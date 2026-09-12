"""Manual browser-paste OAuth PKCE flow for DAO."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DAOApiClient,
    DAOApiError,
    DAOAuthError,
    build_authorization_url,
    new_pkce,
)
from .const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_TOKEN,
    DAO_REDIRECT_URL_DOCS_URL,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
)

_SCHEMA = vol.Schema({vol.Required("redirect_url"): str})


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
    def async_get_options_flow(config_entry):
        """Return this integration's options flow."""
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
        """Generate the one-time browser URL before asking for a callback."""
        return await self.async_step_dao()

    async def async_step_dao(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the DAO URL and exchange its pasted callback, like DHL DE."""
        self._start()
        errors: dict[str, str] = {}
        if user_input is not None:
            client, error = await self._exchange(user_input["redirect_url"])
            if error:
                errors["base"] = error
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
        """Re-run the same three steps; :meth:`async_step_dao` guards the account."""
        return await self.async_step_user()


class DAOOptionsFlow(OptionsFlow):
    """Match the suite's delivered-retention and history options."""

    async def async_step_init(self, user_input=None):
        """Show and save the delivered-retention and history options."""
        if user_input is not None:
            delivered, history = user_input["delivered"], user_input["history"]
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
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
