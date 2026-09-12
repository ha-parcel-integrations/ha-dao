"""Tests for the DAO config and options flow: the manual authorization-code
paste, state verification, reauth's account-mismatch guard, and options."""
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dao.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_TOKEN,
    DAO_REDIRECT_URL_DOCS_URL,
    DOMAIN,
)

FIXED_VERIFIER = "verifier-0"
FIXED_CHALLENGE = "challenge-0"
FIXED_STATE = "state-0"

PKCE = "custom_components.dao.config_flow.new_pkce"
SESSION = "custom_components.dao.config_flow.async_get_clientsession"


def _patch_pkce():
    return patch(PKCE, return_value=(FIXED_VERIFIER, FIXED_CHALLENGE, FIXED_STATE))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _fake_id_token(sub: str) -> str:
    header = _b64url(json.dumps({"alg": "none"}).encode())
    payload = _b64url(json.dumps({"sub": sub}).encode())
    return f"{header}.{payload}.sig"


def _redirect_url(code: str = "auth-code-1", state: str = FIXED_STATE) -> str:
    return f"daoapp://auth-callback?code={code}&state={state}"


def _token_session(status: int, body: object) -> MagicMock:
    response = AsyncMock()
    response.status = status
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


def _token_body(sub: str = "subject-1", refresh_token: str = "rt-1") -> dict:
    return {
        "access_token": "at-1",
        "refresh_token": refresh_token,
        "expires_in": 300,
        "id_token": _fake_id_token(sub),
    }


def _entry(subject: str = "subject-1", refresh_token: str = "rt-0") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DAO",
        unique_id=subject,
        data={CONF_REFRESH_TOKEN: refresh_token, CONF_ACCOUNT_SUBJECT: subject},
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
        },
    )


# ---------------------------------------------------------------------------
# user step — shows the authorize URL, then exchanges the pasted redirect
# ---------------------------------------------------------------------------


async def test_user_flow_shows_authorize_url_and_docs_link(hass):
    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["step_id"] == "dao"
    placeholders = result["description_placeholders"]
    assert FIXED_STATE in placeholders["authorize_url"]
    assert placeholders["docs_url"] == DAO_REDIRECT_URL_DOCS_URL


async def test_user_flow_creates_entry_from_pasted_redirect_url(hass):
    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    with patch(SESSION, return_value=_token_session(200, _token_body())):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _redirect_url()}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_TOKEN] == "rt-1"
    assert result["data"][CONF_ACCOUNT_SUBJECT] == "subject-1"


async def test_user_flow_accepts_the_full_url_pasted_with_surrounding_text(hass):
    """Browsers sometimes hand back the URL with a trailing fragment or
    whitespace; only the query matters."""
    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    with patch(SESSION, return_value=_token_session(200, _token_body())):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": f"  {_redirect_url()}  "}
        )

    assert result["type"] == "create_entry"


async def test_user_flow_rejects_a_mismatched_state(hass):
    """The user pastes a URL by hand and can paste the wrong one — state is a
    real check, not ceremony."""
    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"redirect_url": _redirect_url(state="wrong-state")}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_redirect"}


async def test_user_flow_rejects_a_bare_code(hass):
    """A bare code carries no state to verify against — accepting it would
    reopen the login-CSRF hole `state` exists to close, so it is rejected up
    front with a clear message rather than silently skipping the check."""
    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"redirect_url": "auth-code-1"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "paste_full_url"}


async def test_user_flow_rejects_a_url_without_a_code(hass):
    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"redirect_url": f"daoapp://auth-callback?state={FIXED_STATE}"}
    )

    assert result["errors"] == {"base": "invalid_redirect"}


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"error": "invalid_grant"}, "invalid_auth"),
        ({}, "cannot_connect"),
    ],
)
async def test_user_flow_surfaces_token_endpoint_errors(hass, body, expected):
    """A rejected code and an outage must not look the same to the user."""
    status = 400 if expected == "invalid_auth" else 500
    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    with patch(SESSION, return_value=_token_session(status, body)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _redirect_url()}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": expected}


async def test_user_flow_surfaces_a_network_error_as_cannot_connect(hass):
    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with patch(SESSION, return_value=session):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _redirect_url()}
        )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_aborts_on_duplicate_account(hass):
    _entry(subject="subject-1").add_to_hass(hass)

    with _patch_pkce():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    with patch(SESSION, return_value=_token_session(200, _token_body(sub="subject-1"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": _redirect_url()}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------


async def test_reauth_updates_the_refresh_token(hass):
    entry = _entry(subject="subject-1", refresh_token="rt-0")
    entry.add_to_hass(hass)

    with _patch_pkce():
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "dao"

        with patch(
            SESSION,
            return_value=_token_session(
                200, _token_body(sub="subject-1", refresh_token="rt-new")
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"redirect_url": _redirect_url()}
            )
            await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "rt-new"


async def test_reauth_rejects_a_different_account(hass):
    """Signing in with another DAO account during reauth must not silently
    rebind an existing entry's sensors to someone else's parcels."""
    entry = _entry(subject="subject-1", refresh_token="rt-0")
    entry.add_to_hass(hass)

    with _patch_pkce():
        result = await entry.start_reauth_flow(hass)
        with patch(
            SESSION, return_value=_token_session(200, _token_body(sub="someone-else"))
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"redirect_url": _redirect_url()}
            )

    assert result["type"] == "abort"
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_REFRESH_TOKEN] == "rt-0"


async def test_reauth_surfaces_invalid_credentials(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with _patch_pkce():
        result = await entry.start_reauth_flow(hass)
        with patch(
            SESSION, return_value=_token_session(400, {"error": "invalid_grant"})
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"redirect_url": _redirect_url()}
            )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_surfaces_connection_errors(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with _patch_pkce():
        result = await entry.start_reauth_flow(hass)
        with patch(SESSION, return_value=_token_session(500, {})):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"redirect_url": _redirect_url()}
            )

    assert result["errors"] == {"base": "cannot_connect"}


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


async def test_options_flow_saves_and_reloads(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "delivered": {
                    CONF_DELIVERED_FILTER_TYPE: "parcels",
                    CONF_DELIVERED_FILTER_AMOUNT: 5,
                },
                "history": {CONF_INCLUDE_HISTORY: True},
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_DELIVERED_FILTER_TYPE: "parcels",
        CONF_DELIVERED_FILTER_AMOUNT: 5,
        CONF_INCLUDE_HISTORY: True,
    }
    # A changed setting only takes effect on reload, so the flow schedules one
    # itself rather than registering an update listener (which is deprecated in
    # combination with reloading).
    schedule_reload.assert_called_once_with(entry.entry_id)
