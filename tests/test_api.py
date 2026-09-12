"""Tests for the DAO API client: PKCE, the token exchange/refresh, and the
bearer-authenticated tracking calls."""
import base64
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest

from custom_components.dao.api import (
    DAOApiClient,
    DAOApiError,
    DAOAuthError,
    build_authorization_url,
    new_pkce,
    resolve_lang,
)
from custom_components.dao.const import DAO_CLIENT_ID, DAO_REDIRECT_URI

REFRESH_TOKEN = "rt-0"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _fake_id_token(sub: str) -> str:
    header = _b64url(json.dumps({"alg": "none"}).encode())
    payload = _b64url(json.dumps({"sub": sub}).encode())
    return f"{header}.{payload}.sig"


def _response(status: int, body: object = None, headers: dict | None = None) -> AsyncMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
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


def _session(*, post: list | None = None, get: list | None = None) -> MagicMock:
    """A session whose .post()/.get() hand out one context per call, in order."""
    session = MagicMock()
    if post is not None:
        session.post = MagicMock(side_effect=[_ctx(r) for r in post])
    if get is not None:
        session.get = MagicMock(side_effect=[_ctx(r) for r in get])
    return session


def _client(session: MagicMock, refresh_token: str | None = REFRESH_TOKEN) -> DAOApiClient:
    return DAOApiClient(session, refresh_token)


def _authenticated_client(session: MagicMock) -> DAOApiClient:
    """A client whose access token is already fresh, so async_refresh() is a no-op."""
    from datetime import datetime, timedelta, timezone

    client = _client(session)
    client.access_token = "at-0"
    client._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


TOKEN_BODY = {
    "access_token": "at-1",
    "refresh_token": "rt-1",
    "expires_in": 300,
    "id_token": _fake_id_token("subject-1"),
}


# ---------------------------------------------------------------------------
# PKCE + authorization URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hass_language,expected",
    [
        ("da", "da"),
        ("da-DK", "da"),
        ("en", "en"),
        ("en-US", "en"),
        ("nl", "en"),
        (None, "en"),
        ("", "en"),
    ],
)
def test_resolve_lang(hass_language, expected):
    assert resolve_lang(hass_language) == expected


def test_new_pkce_challenge_matches_verifier():
    verifier, challenge, state = new_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert 43 <= len(verifier) <= 128
    assert state


def test_new_pkce_is_random_each_time():
    first = new_pkce()
    second = new_pkce()
    assert first != second


def test_build_authorization_url_carries_pkce_and_client_params():
    url = build_authorization_url("challenge-1", "state-1")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert query["client_id"] == [DAO_CLIENT_ID]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [DAO_REDIRECT_URI]
    assert query["scope"] == ["openid profile email"]
    assert query["state"] == ["state-1"]
    assert query["code_challenge"] == ["challenge-1"]
    assert query["code_challenge_method"] == ["S256"]


# ---------------------------------------------------------------------------
# async_exchange_code — the token endpoint's auth/outage split matters here
# ---------------------------------------------------------------------------


async def test_exchange_code_stores_tokens_and_account_subject():
    session = _session(post=[_response(200, TOKEN_BODY)])
    client = _client(session, refresh_token=None)

    await client.async_exchange_code("code-1", "verifier-1")

    assert client.access_token == "at-1"
    assert client.refresh_token == "rt-1"
    assert client.account_subject == "subject-1"
    sent = session.post.call_args[1]["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "code-1"
    assert sent["code_verifier"] == "verifier-1"
    assert sent["redirect_uri"] == DAO_REDIRECT_URI


async def test_exchange_code_raises_auth_error_on_invalid_grant():
    session = _session(post=[_response(400, {"error": "invalid_grant"})])
    client = _client(session, refresh_token=None)

    with pytest.raises(DAOAuthError):
        await client.async_exchange_code("bogus", "verifier-1")


async def test_exchange_code_raises_api_error_on_outage():
    """A 5xx must not look like a rejected code, or the user is told to retry
    a code that is already single-use and burned."""
    session = _session(post=[_response(500, {})])
    client = _client(session, refresh_token=None)

    with pytest.raises(DAOApiError) as err:
        await client.async_exchange_code("code-1", "verifier-1")
    assert not isinstance(err.value, DAOAuthError)


async def test_exchange_code_raises_on_unparseable_body():
    session = _session(post=[_response(200, "not json")])
    client = _client(session, refresh_token=None)

    with pytest.raises(DAOApiError):
        await client.async_exchange_code("code-1", "verifier-1")


async def test_exchange_code_raises_when_tokens_missing():
    session = _session(post=[_response(200, {"access_token": "at-1"})])
    client = _client(session, refresh_token=None)

    with pytest.raises(DAOApiError):
        await client.async_exchange_code("code-1", "verifier-1")


async def test_exchange_code_tolerates_a_malformed_id_token():
    """An unreadable id_token must not fail the whole exchange — only the
    account identity is lost, and the caller falls back to the refresh token."""
    body = dict(TOKEN_BODY, id_token="not-a-jwt")
    session = _session(post=[_response(200, body)])
    client = _client(session, refresh_token=None)

    await client.async_exchange_code("code-1", "verifier-1")

    assert client.access_token == "at-1"
    assert client.account_subject is None


async def test_exchange_code_propagates_network_error():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = _client(session, refresh_token=None)

    with pytest.raises(aiohttp.ClientError):
        await client.async_exchange_code("code-1", "verifier-1")


# ---------------------------------------------------------------------------
# async_refresh
# ---------------------------------------------------------------------------


async def test_refresh_raises_without_a_refresh_token():
    client = _client(MagicMock(), refresh_token=None)
    with pytest.raises(DAOAuthError):
        await client.async_refresh()


async def test_refresh_skips_the_network_call_when_token_is_fresh():
    session = _session(post=[])
    client = _authenticated_client(session)

    await client.async_refresh()

    session.post.assert_not_called()


async def test_refresh_calls_token_endpoint_when_no_access_token_yet():
    session = _session(post=[_response(200, TOKEN_BODY)])
    client = _client(session)

    await client.async_refresh()

    assert client.access_token == "at-1"
    sent = session.post.call_args[1]["data"]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == REFRESH_TOKEN


async def test_refresh_force_bypasses_the_freshness_check():
    session = _session(post=[_response(200, TOKEN_BODY)])
    client = _authenticated_client(session)

    await client.async_refresh(force=True)

    session.post.assert_called_once()
    assert client.access_token == "at-1"


async def test_refresh_raises_auth_error_on_invalid_grant():
    session = _session(post=[_response(400, {"error": "invalid_grant"})])
    client = _client(session)

    with pytest.raises(DAOAuthError):
        await client.async_refresh()


async def test_refresh_raises_api_error_on_outage():
    session = _session(post=[_response(502, {})])
    client = _client(session)

    with pytest.raises(DAOApiError) as err:
        await client.async_refresh()
    assert not isinstance(err.value, DAOAuthError)


# ---------------------------------------------------------------------------
# async_get_parcels
# ---------------------------------------------------------------------------


async def test_get_parcels_combines_in_and_out_bound():
    session = _session(get=[_response(200, {"items": [{"a": 1}]}), _response(200, {"items": [{"b": 2}]})])
    client = _authenticated_client(session)

    parcels = await client.async_get_parcels()

    assert parcels == [{"a": 1}, {"b": 2}]
    assert session.get.call_count == 2
    bounds = {call.kwargs["params"]["bound"] for call in session.get.call_args_list}
    assert bounds == {"IN", "OUT"}
    langs = {call.kwargs["params"]["lang"] for call in session.get.call_args_list}
    assert langs == {"en"}


async def test_get_parcels_sends_the_configured_language():
    from datetime import datetime, timedelta, timezone

    session = _session(get=[_response(200, {"items": []}), _response(200, {"items": []})])
    client = DAOApiClient(session, REFRESH_TOKEN, language="da")
    client.access_token = "at-0"
    client._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    await client.async_get_parcels()

    langs = {call.kwargs["params"]["lang"] for call in session.get.call_args_list}
    assert langs == {"da"}


async def test_get_parcels_refreshes_before_calling_the_api():
    session = MagicMock()
    session.post = MagicMock(return_value=_ctx(_response(200, TOKEN_BODY)))
    session.get = MagicMock(
        side_effect=[
            _ctx(_response(200, {"items": []})),
            _ctx(_response(200, {"items": []})),
        ]
    )
    client = _client(session)

    await client.async_get_parcels()

    session.post.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [
        {"items": [{"trackingId": "A"}]},
        {"data": [{"trackingId": "A"}]},
        [{"trackingId": "A"}],
    ],
)
async def test_get_parcels_accepts_every_known_envelope_shape(body):
    session = _session(get=[_response(200, body), _response(200, {"items": []})])
    client = _authenticated_client(session)

    parcels = await client.async_get_parcels()

    assert parcels == [{"trackingId": "A"}]


async def test_get_parcels_skips_non_dict_entries():
    session = _session(
        get=[_response(200, {"items": [{"trackingId": "A"}, "junk"]}), _response(200, {"items": []})]
    )
    client = _authenticated_client(session)

    assert await client.async_get_parcels() == [{"trackingId": "A"}]


async def test_get_parcels_retries_once_on_401_then_succeeds():
    session = MagicMock()
    session.post = MagicMock(return_value=_ctx(_response(200, TOKEN_BODY)))
    session.get = MagicMock(
        side_effect=[
            _ctx(_response(401, {})),
            _ctx(_response(200, {"items": []})),
            _ctx(_response(200, {"items": []})),
        ]
    )
    client = _authenticated_client(session)

    await client.async_get_parcels()

    session.post.assert_called_once()  # the forced refresh


async def test_get_parcels_raises_auth_error_on_second_401():
    session = MagicMock()
    session.post = MagicMock(return_value=_ctx(_response(200, TOKEN_BODY)))
    session.get = MagicMock(
        side_effect=[_ctx(_response(401, {})), _ctx(_response(401, {}))]
    )
    client = _authenticated_client(session)

    with pytest.raises(DAOAuthError):
        await client.async_get_parcels()


async def test_get_parcels_raises_on_error_status_with_status_code():
    session = _session(get=[_response(503, {})])
    client = _authenticated_client(session)

    with pytest.raises(DAOApiError) as err:
        await client.async_get_parcels()
    assert err.value.status_code == 503


async def test_get_parcels_429_carries_retry_after_from_header():
    session = _session(get=[_response(429, {}, headers={"Retry-After": "30"})])
    client = _authenticated_client(session)

    with pytest.raises(DAOApiError) as err:
        await client.async_get_parcels()
    assert err.value.status_code == 429
    assert err.value.retry_after == 30


async def test_get_parcels_429_without_retry_after_header_leaves_it_none():
    session = _session(get=[_response(429, {})])
    client = _authenticated_client(session)

    with pytest.raises(DAOApiError) as err:
        await client.async_get_parcels()
    assert err.value.retry_after is None


async def test_get_parcels_raises_on_unparseable_body():
    session = _session(get=[_response(200, "not json")])
    client = _authenticated_client(session)

    with pytest.raises(DAOApiError):
        await client.async_get_parcels()


async def test_get_parcels_raises_without_a_list():
    session = _session(get=[_response(200, {"items": "nope"})])
    client = _authenticated_client(session)

    with pytest.raises(DAOApiError):
        await client.async_get_parcels()


# ---------------------------------------------------------------------------
# async_get_parcel_detail
# ---------------------------------------------------------------------------


async def test_get_parcel_detail_returns_events():
    body = {"trackingId": "A", "events": [{"timestamp": "2026-04-29T13:12:42Z"}]}
    session = _session(get=[_response(200, body)])
    client = _authenticated_client(session)

    detail = await client.async_get_parcel_detail("A")

    assert detail["events"] == body["events"]
    url = session.get.call_args[0][0]
    assert url.endswith("/tracking/A")
    assert session.get.call_args.kwargs["params"]["lang"] == "en"


@pytest.mark.parametrize(
    "body",
    [
        {"trackingId": "A", "events": []},
        {"data": {"trackingId": "A", "events": []}},
        {"item": {"trackingId": "A", "events": []}},
    ],
)
async def test_get_parcel_detail_accepts_every_known_envelope_shape(body):
    session = _session(get=[_response(200, body)])
    client = _authenticated_client(session)

    detail = await client.async_get_parcel_detail("A")

    assert detail["trackingId"] == "A"


async def test_get_parcel_detail_raises_without_an_object():
    session = _session(get=[_response(200, {"data": "nope"})])
    client = _authenticated_client(session)

    with pytest.raises(DAOApiError):
        await client.async_get_parcel_detail("A")


async def test_get_parcel_detail_retries_once_on_401_then_succeeds():
    session = MagicMock()
    session.post = MagicMock(return_value=_ctx(_response(200, TOKEN_BODY)))
    session.get = MagicMock(
        side_effect=[
            _ctx(_response(401, {})),
            _ctx(_response(200, {"trackingId": "A"})),
        ]
    )
    client = _authenticated_client(session)

    await client.async_get_parcel_detail("A")

    session.post.assert_called_once()


async def test_get_parcel_detail_propagates_client_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = _authenticated_client(session)

    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel_detail("A")


# ---------------------------------------------------------------------------
# async_get_pickup_point
# ---------------------------------------------------------------------------


async def test_get_pickup_point_returns_the_resolved_object():
    body = {"id": "PP1", "name": "Example Point"}
    session = _session(get=[_response(200, body)])
    client = _authenticated_client(session)

    pickup_point = await client.async_get_pickup_point("PP1")

    assert pickup_point == body
    url = session.get.call_args[0][0]
    assert url.endswith("/pickup-points/PP1")


async def test_get_pickup_point_is_cached_and_never_refetched():
    """Pickup points are near-static and shared across parcels — a second
    lookup for the same id must not hit the network again."""
    body = {"id": "PP1", "name": "Example Point"}
    session = _session(get=[_response(200, body)])
    client = _authenticated_client(session)

    first = await client.async_get_pickup_point("PP1")
    second = await client.async_get_pickup_point("PP1")

    assert first == second == body
    assert session.get.call_count == 1


async def test_get_pickup_point_caches_per_id():
    session = _session(
        get=[
            _response(200, {"id": "PP1", "name": "One"}),
            _response(200, {"id": "PP2", "name": "Two"}),
        ]
    )
    client = _authenticated_client(session)

    first = await client.async_get_pickup_point("PP1")
    second = await client.async_get_pickup_point("PP2")

    assert first["name"] == "One"
    assert second["name"] == "Two"
    assert session.get.call_count == 2


async def test_get_pickup_point_raises_without_an_object():
    session = _session(get=[_response(200, {"data": "nope"})])
    client = _authenticated_client(session)

    with pytest.raises(DAOApiError):
        await client.async_get_pickup_point("PP1")


async def test_get_pickup_point_failure_is_not_cached():
    """A failed lookup must not poison the cache — the next poll should
    retry rather than being stuck with nothing forever."""
    session = _session(get=[_response(503, {}), _response(200, {"id": "PP1"})])
    client = _authenticated_client(session)

    with pytest.raises(DAOApiError):
        await client.async_get_pickup_point("PP1")

    pickup_point = await client.async_get_pickup_point("PP1")
    assert pickup_point == {"id": "PP1"}
    assert session.get.call_count == 2
