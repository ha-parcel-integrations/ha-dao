"""Manual browser-paste OAuth PKCE flow for DAO."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .account.client import (
    DAOApiClient,
    DAOApiError,
    DAOAuthError,
    build_authorization_url,
    new_pkce,
)
from .const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_BARCODE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DAO_REDIRECT_URL_DOCS_URL,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)

_SCHEMA = vol.Schema({vol.Required("redirect_url"): str})


def normalize_code(value: str) -> str:
    """Return a tracking code trimmed, nothing else.

    DAO publishes no barcode format or checksum, so nothing is upper-cased
    or stripped: a guessed transform could turn a working code into one the
    lookup rejects.
    """
    return (value or "").strip()


def clean_codes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate a submitted code list."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


class DAOConfigFlow(ConfigFlow, domain=DOMAIN):
    """Manual browser-paste OAuth PKCE flow, and its reauth counterpart."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow with no PKCE material generated yet."""
        self._verifier: str | None = None
        self._state: str | None = None
        self._url: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Return the options flow for this entry's source.

        The two sources use the suite's two different options models and
        they may not be mixed: the account entry reloads on save
        (``OptionsFlowWithReload``, no update listener), the tracking entry
        applies changes live through its update listener instead.
        """
        if config_entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_TRACKING:
            return DAOTrackingOptionsFlow()
        return DAOOptionsFlow()

    def _start(self) -> None:
        if not self._url:
            self._verifier, challenge, self._state = new_pkce()
            self._url = build_authorization_url(challenge, self._state)

    async def _exchange(self, value: str) -> tuple[DAOApiClient | None, str | None]:
        query = parse_qs(urlparse(value.strip()).query)
        if not query:
            # A bare code carries no state to verify against — accepting it
            # anyway would reopen exactly the login-CSRF hole `state` exists
            # to close (an attacker's own authorization code, pasted by the
            # victim, would silently link the attacker's DAO account to this
            # entry). Reject and ask for the full URL instead of weakening
            # the check for this one path.
            return None, "paste_full_url"
        code = query.get("code", [None])[0]
        if not code or query.get("state", [None])[0] != self._state:
            return None, "invalid_redirect"
        client = DAOApiClient(async_get_clientsession(self.hass))
        try:
            await client.async_exchange_code(code, self._verifier or "")
        except DAOAuthError:
            return None, "invalid_auth"
        except (DAOApiError, aiohttp.ClientError):
            return None, "cannot_connect"
        return client, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which DAO source to set up: a sign-in, or just tracking codes."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the keyless tracking-code hub. Asks nothing and calls nothing.

        One hub per install: DAO's public lookup needs no account and no
        postcode, so there is nothing to key a second hub on — and keeping it
        a singleton is what lets ``dao.track_parcel`` take a bare code with
        no "which hub" argument.
        """
        await self.async_set_unique_id(SOURCE_TRACKING)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="DAO (tracking codes)",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={
                CONF_PARCELS: [],
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up the signed-in account source."""
        return await self.async_step_dao(user_input)

    async def async_step_dao(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the DAO URL and exchange its pasted callback, like DHL DE."""
        self._start()
        errors: dict[str, str] = {}
        if user_input is not None:
            client, error = await self._exchange(user_input["redirect_url"])
            if client is None or client.refresh_token is None:
                errors["base"] = error or "cannot_connect"
            else:
                await self.async_set_unique_id(
                    client.account_subject or f"dao:{client.refresh_token[-12:]}"
                )
                if self.source == "reauth":
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={
                            CONF_REFRESH_TOKEN: client.refresh_token,
                            CONF_ACCOUNT_SUBJECT: client.account_subject or "unknown",
                        },
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="DAO",
                    data={
                        CONF_SOURCE: SOURCE_ACCOUNT,
                        CONF_REFRESH_TOKEN: client.refresh_token,
                        CONF_ACCOUNT_SUBJECT: client.account_subject or "unknown",
                    },
                )
        return self.async_show_form(
            step_id="dao",
            data_schema=_SCHEMA,
            errors=errors,
            description_placeholders={
                "authorize_url": self._url or "",
                "docs_url": DAO_REDIRECT_URL_DOCS_URL,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Re-run the same three steps; :meth:`async_step_dao` guards the account.

        Straight to the account step, not the source menu: only an account
        entry can ever need reauthenticating.
        """
        return await self.async_step_dao()


class DAOOptionsFlow(OptionsFlowWithReload):
    """Match the suite's delivered-retention and history options."""

    async def async_step_init(self, user_input=None):
        """Show and save the delivered-retention and history options."""
        if user_input is not None:
            delivered, history = user_input["delivered"], user_input["history"]
            # OptionsFlowWithReload reloads once the new options are saved; a
            # reload scheduled here would start before that, on the old ones.
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required("delivered"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_DELIVERED_FILTER_TYPE,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    DEFAULT_DELIVERED_FILTER_TYPE,
                                ),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=["days", "parcels"],
                                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                                    mode=selector.SelectSelectorMode.LIST,
                                )
                            ),
                            vol.Required(
                                CONF_DELIVERED_FILTER_AMOUNT,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_AMOUNT,
                                    DEFAULT_DELIVERED_FILTER_AMOUNT,
                                ),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=1,
                                    max=365,
                                    step=1,
                                    mode=selector.NumberSelectorMode.BOX,
                                )
                            ),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_INCLUDE_HISTORY,
                                default=current.get(
                                    CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                                ),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class DAOTrackingOptionsFlow(OptionsFlow):
    """Manage tracked codes separately from integration settings.

    The suite's code-based shape: a menu of exactly ``Parcels`` and
    ``Settings``. ``Parcels`` edits the whole code list at once; ``Settings``
    holds delivered-parcel retention and history. A plain ``OptionsFlow``,
    not ``OptionsFlowWithReload`` — changes apply live through the update
    listener in ``__init__.py``, which is also what resumes polling after it
    has suspended entirely. No live validation on add (format only): a code
    is confirmed real by the next poll.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        return self.async_show_menu(
            step_id="init", menu_options=["parcels", "settings"]
        )

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = clean_codes(user_input.get("tracking_codes"))
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_PARCELS: [{CONF_BARCODE: code} for code in codes],
                },
            )
        current = [
            item[CONF_BARCODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_BARCODE)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DELIVERED_FILTER_TYPE,
                    default=current.get(
                        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                    ),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["days", "parcels"],
                        translation_key=CONF_DELIVERED_FILTER_TYPE,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_DELIVERED_FILTER_AMOUNT,
                    default=current.get(
                        CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_INCLUDE_HISTORY,
                    default=current.get(
                        CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                    ),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)
