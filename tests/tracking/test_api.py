"""Tests for the keyless public barcode client: discovery, not-found, bans."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dao.tracking.api import (
    BOT_BANNED_CODE,
    DAOTrackingApiClient,
    DAOTrackingApiError,
    DAOTrackingBannedError,
    extract_page_config,
)

from .payloads import CODE, ENDPOINT, NONCE, NOT_FOUND, PAGE_HTML, found


def _response(
    status: int, body: object = None, headers: dict | None = None, text: str = ""
) -> AsyncMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    response.text = AsyncMock(return_value=text)
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    return response


def _ctx(response: AsyncMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(responses: list) -> MagicMock:
    """A session handing out one context per ``get()`` call, in order."""
    session = MagicMock()
    contexts = [item if isinstance(item, Exception) else _ctx(item) for item in responses]

    def _get(*args, **kwargs):
        nonlocal contexts
        item = contexts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    session.get = MagicMock(side_effect=_get)
    return session


# ---------------------------------------------------------------------------
# page discovery
# ---------------------------------------------------------------------------


def test_extract_page_config_reads_endpoint_and_nonce():
    config = extract_page_config(PAGE_HTML)
    assert config["endpoint"] == ENDPOINT
    assert config["nonce"] == NONCE


@pytest.mark.parametrize(
    "html",
    [
        "<html>no config here</html>",
        "<script>TRACK_PARCEL_API = {not json};</script>",
        '<script>TRACK_PARCEL_API = {"nonce":"x"};</script>',
    ],
)
def test_extract_page_config_rejects_a_changed_page(html):
    """A changed page is an API failure, explicitly not a 'parcel not found'."""
    with pytest.raises(DAOTrackingApiError):
        extract_page_config(html)


async def test_page_is_read_once_and_cached():
    session = _session(
        [
            _response(200, text=PAGE_HTML),
            _response(200, found()),
            _response(200, NOT_FOUND),
        ]
    )
    client = DAOTrackingApiClient(session)

    assert await client.async_get_parcel(CODE) is not None
    assert await client.async_get_parcel(CODE) is None
    # One page read, two lookups.
    assert session.get.call_count == 3


async def test_an_unreachable_page_is_an_api_error():
    session = _session([aiohttp.ClientError("boom")])
    with pytest.raises(DAOTrackingApiError):
        await DAOTrackingApiClient(session).async_get_parcel(CODE)


async def test_a_failing_page_carries_its_status_code():
    session = _session([_response(503, text="")])
    with pytest.raises(DAOTrackingApiError) as err:
        await DAOTrackingApiClient(session).async_get_parcel(CODE)
    assert err.value.status_code == 503


# ---------------------------------------------------------------------------
# the lookup itself
# ---------------------------------------------------------------------------


async def test_found_returns_the_parcel_object():
    session = _session([_response(200, text=PAGE_HTML), _response(200, found())])
    parcel = await DAOTrackingApiClient(session).async_get_parcel(CODE)
    assert parcel is not None
    assert parcel["barcode"] == CODE


async def test_not_found_is_none_not_an_error():
    """``{"found": false}`` is the route's normal unknown-code answer."""
    session = _session([_response(200, text=PAGE_HTML), _response(200, NOT_FOUND)])
    assert await DAOTrackingApiClient(session).async_get_parcel(CODE) is None


async def test_a_found_parcel_without_an_object_is_an_error():
    session = _session(
        [_response(200, text=PAGE_HTML), _response(200, {"found": True})]
    )
    with pytest.raises(DAOTrackingApiError):
        await DAOTrackingApiClient(session).async_get_parcel(CODE)


async def test_non_json_is_an_error():
    session = _session([_response(200, text=PAGE_HTML), _response(200, "<html>")])
    with pytest.raises(DAOTrackingApiError):
        await DAOTrackingApiClient(session).async_get_parcel(CODE)


async def test_the_nonce_and_referer_are_sent():
    session = _session([_response(200, text=PAGE_HTML), _response(200, NOT_FOUND)])
    client = DAOTrackingApiClient(session, language="da")
    await client.async_get_parcel(CODE)

    _, kwargs = session.get.call_args
    assert kwargs["headers"]["X-WP-Nonce"] == NONCE
    assert "find-din-pakke" in kwargs["headers"]["Referer"]
    assert kwargs["params"] == {"barcode": CODE, "lang": "DA"}


async def test_no_turnstile_header_is_ever_sent():
    """The browser's challenge token is never faked, solved or guessed."""
    session = _session([_response(200, text=PAGE_HTML), _response(200, NOT_FOUND)])
    await DAOTrackingApiClient(session).async_get_parcel(CODE)
    _, kwargs = session.get.call_args
    assert not any(key.lower().startswith("x-turnstile") for key in kwargs["headers"])


# ---------------------------------------------------------------------------
# rejection handling
# ---------------------------------------------------------------------------


async def test_a_403_rereads_the_page_once_then_gives_up():
    """A rejected request can be a stale nonce; one fresh read is the fix."""
    session = _session(
        [
            _response(200, text=PAGE_HTML),
            _response(403, {"code": "rest_cookie_invalid_nonce"}),
            _response(200, text=PAGE_HTML),
            _response(200, found()),
        ]
    )
    client = DAOTrackingApiClient(session)
    assert await client.async_get_parcel(CODE) is not None
    assert session.get.call_count == 4


async def test_a_second_403_is_treated_as_a_ban():
    session = _session(
        [
            _response(200, text=PAGE_HTML),
            _response(403, {"code": "forbidden"}),
            _response(200, text=PAGE_HTML),
            _response(403, {"code": "forbidden"}),
        ]
    )
    with pytest.raises(DAOTrackingBannedError):
        await DAOTrackingApiClient(session).async_get_parcel(CODE)


async def test_the_bot_check_ban_code_is_a_ban_not_a_throttle():
    session = _session(
        [
            _response(200, text=PAGE_HTML),
            _response(429, {"code": BOT_BANNED_CODE}),
        ]
    )
    with pytest.raises(DAOTrackingBannedError) as err:
        await DAOTrackingApiClient(session).async_get_parcel(CODE)
    assert err.value.code == BOT_BANNED_CODE


async def test_a_plain_429_carries_retry_after():
    session = _session(
        [
            _response(200, text=PAGE_HTML),
            _response(429, {"message": "slow down"}, headers={"Retry-After": "90"}),
        ]
    )
    with pytest.raises(DAOTrackingApiError) as err:
        await DAOTrackingApiClient(session).async_get_parcel(CODE)
    assert not isinstance(err.value, DAOTrackingBannedError)
    assert err.value.retry_after == 90


async def test_an_unexpected_status_is_an_api_error():
    session = _session([_response(200, text=PAGE_HTML), _response(500, None)])
    with pytest.raises(DAOTrackingApiError) as err:
        await DAOTrackingApiClient(session).async_get_parcel(CODE)
    assert err.value.status_code == 500


async def test_a_network_error_mid_lookup_is_an_api_error():
    session = _session([_response(200, text=PAGE_HTML), aiohttp.ClientError("reset")])
    with pytest.raises(DAOTrackingApiError):
        await DAOTrackingApiClient(session).async_get_parcel(CODE)
