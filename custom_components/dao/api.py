"""DAO's public native-client OIDC and read-only tracking API."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .const import (
    DAO_AUTHORIZATION_URL,
    DAO_CLIENT_ID,
    DAO_PICKUP_POINT_URL,
    DAO_REDIRECT_URI,
    DAO_TOKEN_URL,
    DAO_TRACKING_URL,
)


class DAOApiError(Exception):
    """DAO is unavailable or returned an unexpected response.

    ``status_code``/``retry_after`` carry the HTTP layer's own numbers up to
    the coordinator, which needs them for the 429 backoff — an exception
    string alone can't be branched on.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        """Initialize the error with the failing status code, if known."""
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class DAOAuthError(DAOApiError):
    """DAO rejected an authorization or refresh token."""


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` header value in seconds, or ``None``."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def new_pkce() -> tuple[str, str, str]:
    """Create one S256 verifier/challenge/state triple."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge, secrets.token_urlsafe(24)


def resolve_lang(hass_language: str | None) -> str:
    """Map HA's configured language to one of DAO's two supported UI locales.

    Everything user-visible in the tracking payload (``statusText``) is
    server-rendered in whatever ``lang`` is requested. DAO is a Danish
    carrier so Danish gets its own code; every other HA locale falls back to
    English rather than guessing at a locale DAO does not serve.
    """
    if (hass_language or "").split("-")[0].lower() == "da":
        return "da"
    return "en"


def build_authorization_url(challenge: str, state: str) -> str:
    """Return DAO's app login URL; credentials never reach Home Assistant."""
    params = {
        "client_id": DAO_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": DAO_REDIRECT_URI,
        "scope": "openid profile email",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "ui_locales": "da",
        "kc_locale": "da",
    }
    return f"{DAO_AUTHORIZATION_URL}?{urlencode(params)}"


class DAOApiClient:
    """One DAO account's in-memory bearer session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str | None = None,
        *,
        language: str = "en",
    ) -> None:
        """Initialize the client, optionally with an already-issued refresh token."""
        self._session = session
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.account_subject: str | None = None
        self._expires_at: datetime | None = None
        self._language = language
        # pickupPointId -> resolved pickup-point object. Pickup points are
        # near-static and many parcels share one, so this lives for the
        # client's lifetime (same scope as the bearer session) rather than
        # being refetched per parcel per poll.
        self._pickup_point_cache: dict[str, dict[str, Any]] = {}

    async def _token(self, data: dict[str, str]) -> None:
        async with self._session.post(DAO_TOKEN_URL, data=data) as response:
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise DAOApiError("DAO token endpoint returned non-JSON") from err
        if (
            response.status in (400, 401)
            and isinstance(payload, dict)
            and payload.get("error")
            in {"invalid_grant", "invalid_client", "unauthorized_client"}
        ):
            raise DAOAuthError(str(payload.get("error")))
        if response.status != 200 or not isinstance(payload, dict):
            raise DAOApiError(
                f"DAO token endpoint returned HTTP {response.status}",
                status_code=response.status,
            )
        if not isinstance(payload.get("access_token"), str) or not isinstance(
            payload.get("refresh_token"), str
        ):
            raise DAOApiError("DAO token response omitted required token")
        self.access_token, self.refresh_token = (
            payload["access_token"],
            payload["refresh_token"],
        )
        self._expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(payload.get("expires_in", 300))
        )
        # Keycloak's id_token sub is a stable account identifier; it is never logged.
        token = payload.get("id_token", "").split(".")
        if len(token) == 3:
            try:
                self.account_subject = (
                    str(
                        __import__("json")
                        .loads(
                            base64.urlsafe_b64decode(
                                token[1] + "=" * (-len(token[1]) % 4)
                            )
                        )
                        .get("sub")
                        or ""
                    )
                    or None
                )
            except (ValueError, UnicodeDecodeError):
                pass

    async def async_exchange_code(self, code: str, verifier: str) -> None:
        """Exchange a pasted authorization code for the account's tokens."""
        await self._token(
            {
                "grant_type": "authorization_code",
                "client_id": DAO_CLIENT_ID,
                "code": code,
                "redirect_uri": DAO_REDIRECT_URI,
                "code_verifier": verifier,
            }
        )

    async def async_refresh(self, *, force: bool = False) -> None:
        """Refresh the access token if it is missing or near expiry.

        ``force`` bypasses the expiry check — used after a 401 to get a
        genuinely new token rather than trusting a clock that just lied.
        """
        if not self.refresh_token:
            raise DAOAuthError("missing refresh token")
        if (
            not force
            and self.access_token
            and self._expires_at
            and datetime.now(timezone.utc) < self._expires_at - timedelta(seconds=120)
        ):
            return
        await self._token(
            {
                "grant_type": "refresh_token",
                "client_id": DAO_CLIENT_ID,
                "refresh_token": self.refresh_token,
            }
        )

    async def _authenticated_get(
        self, url: str, params: dict[str, str] | None = None
    ) -> Any:
        """GET one bearer-authenticated endpoint, refreshing once on a 401.

        Every read call goes through this so the refresh/retry/error
        plumbing exists in exactly one place, instead of being duplicated
        for the list call, the detail call and the pickup-point lookup.
        """
        await self.async_refresh()
        for attempt in range(2):
            async with self._session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
            ) as response:
                if response.status == 401 and attempt == 0:
                    await self.async_refresh(force=True)
                    continue
                if response.status == 401:
                    raise DAOAuthError("DAO API rejected refreshed token")
                if response.status != 200:
                    raise DAOApiError(
                        f"DAO API returned HTTP {response.status}",
                        status_code=response.status,
                        retry_after=_parse_retry_after(
                            response.headers.get("Retry-After")
                        ),
                    )
                try:
                    return await response.json(content_type=None)
                except ValueError as err:
                    raise DAOApiError("DAO API returned non-JSON") from err
        raise AssertionError(  # pragma: no cover - every branch above returns or raises
            "unreachable"
        )

    async def async_get_parcels(self) -> list[dict[str, Any]]:
        """Return the account's inbound and outbound parcels, combined."""
        combined: list[dict[str, Any]] = []
        for bound in ("IN", "OUT"):
            payload = await self._authenticated_get(
                DAO_TRACKING_URL,
                params={"bound": bound, "archived": "FALSE", "lang": self._language},
            )
            items = (
                payload.get("items", payload.get("data", payload))
                if isinstance(payload, dict)
                else payload
            )
            if not isinstance(items, list):
                raise DAOApiError("DAO tracking response has no list")
            combined.extend(item for item in items if isinstance(item, dict))
        return combined

    async def async_get_parcel_detail(self, tracking_id: str) -> dict[str, Any]:
        """Return one parcel's detail payload, including its ``events[]`` timeline.

        The envelope (whether the object comes back bare or wrapped) is not
        confirmed against a live account, so it is unwrapped the same
        defensive way as the list call rather than assumed.
        """
        payload = await self._authenticated_get(
            f"{DAO_TRACKING_URL}/{tracking_id}",
            params={"lang": self._language},
        )
        detail = (
            payload.get("data", payload.get("item", payload))
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(detail, dict):
            raise DAOApiError("DAO tracking detail response has no object")
        return detail

    async def async_get_pickup_point(self, pickup_point_id: str) -> dict[str, Any]:
        """Return one resolved pickup-point object, cached for this client's lifetime.

        Pickup points are near-static and many parcels can share one, so a
        refresh that sees the same id again must not refetch it.
        """
        cached = self._pickup_point_cache.get(pickup_point_id)
        if cached is not None:
            return cached
        payload = await self._authenticated_get(
            f"{DAO_PICKUP_POINT_URL}/{pickup_point_id}"
        )
        pickup_point = (
            payload.get("data", payload.get("item", payload))
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(pickup_point, dict):
            raise DAOApiError("DAO pickup-point response has no object")
        self._pickup_point_cache[pickup_point_id] = pickup_point
        return pickup_point
