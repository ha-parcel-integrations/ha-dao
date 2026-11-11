"""DAO's keyless public barcode lookup.

No account, no credentials: the same route DAO's own "find din pakke" page
uses. The page hands out the endpoint URL and a request nonce in one
JavaScript config object, which is read once and cached — reading a config
object is not parsing the page for parcel data, the parcel itself comes back
as JSON from the API.

The route polices traffic (see :class:`DAOTrackingBannedError`), so every
caller is expected to keep its request volume at household scale.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp

from ..const import DAO_TRACKING_PAGE_URL

_LOGGER = logging.getLogger(__name__)

# The page embeds `var TRACK_PARCEL_API = {...};` once. Matched narrowly, on
# the object braces only, so a page redesign fails loudly here instead of
# silently matching something else.
_CONFIG_RE = re.compile(r"TRACK_PARCEL_API\s*=\s*(\{.*?\})\s*;", re.DOTALL)

# The error code the endpoint returns when its bot check has blocked the
# caller. DAO's own page shows no retry button for it, unlike a plain 429 —
# it is a ban, not a throttle.
BOT_BANNED_CODE = "dao_bot_check_banned"


class DAOTrackingApiError(Exception):
    """The public tracking route is unavailable or answered unexpectedly.

    ``status_code``/``retry_after`` carry the HTTP layer's own numbers up to
    the coordinator, which needs them for the 429 backoff; ``code`` carries
    the error code out of the response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
        code: str | None = None,
    ) -> None:
        """Initialize the error with whatever the response told us."""
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.code = code


class DAOTrackingBannedError(DAOTrackingApiError):
    """DAO's bot check has rejected us (``403``, or ``429`` + the ban code).

    Distinct from a plain rate limit on purpose: this is not a per-parcel
    problem and must not be retried on the normal polling cadence.
    """


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` header value in seconds, or ``None``."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def extract_page_config(html: str) -> dict[str, Any]:
    """Return the tracking page's embedded API config object.

    Raises :class:`DAOTrackingApiError` when the object is missing or no
    longer valid JSON — that is a changed page, which is an API failure and
    explicitly *not* a "parcel not found".
    """
    match = _CONFIG_RE.search(html or "")
    if match is None:
        raise DAOTrackingApiError("DAO tracking page carries no API config")
    try:
        config = json.loads(match.group(1))
    except ValueError as err:
        raise DAOTrackingApiError("DAO tracking page config is not JSON") from err
    if not isinstance(config, dict) or not config.get("endpoint"):
        raise DAOTrackingApiError("DAO tracking page config names no endpoint")
    return config


class DAOTrackingApiClient:
    """Keyless barcode lookups against DAO's public tracking route."""

    def __init__(
        self, session: aiohttp.ClientSession, *, language: str = "en"
    ) -> None:
        """Initialize the client; nothing is fetched until the first lookup."""
        self._session = session
        self._language = language
        self._config: dict[str, Any] | None = None

    @property
    def language(self) -> str:
        """The ``lang`` parameter this client sends, in the page's own casing."""
        return self._language.upper()

    async def _discover(self, *, force: bool = False) -> dict[str, Any]:
        """Fetch and cache the page's endpoint + nonce.

        ``force`` re-reads the page after a rejection, since a stale nonce is
        the one failure a fresh read can fix on its own.
        """
        if self._config is not None and not force:
            return self._config
        try:
            async with self._session.get(DAO_TRACKING_PAGE_URL) as response:
                if response.status != 200:
                    raise DAOTrackingApiError(
                        f"DAO tracking page returned HTTP {response.status}",
                        status_code=response.status,
                    )
                html = await response.text()
        except aiohttp.ClientError as err:
            raise DAOTrackingApiError(f"DAO tracking page unreachable: {err}") from err
        self._config = extract_page_config(html)
        return self._config

    def _headers(self, config: dict[str, Any]) -> dict[str, str]:
        """Headers for one lookup.

        The nonce is sent although the endpoint does not currently enforce it
        — it costs nothing and enforcement can be switched back on. The
        Turnstile header the browser sends is deliberately never faked.
        """
        headers = {
            "Accept": "application/json",
            "Referer": DAO_TRACKING_PAGE_URL,
        }
        nonce = config.get("nonce")
        if nonce:
            headers["X-WP-Nonce"] = str(nonce)
        return headers

    async def async_get_parcel(self, barcode: str) -> dict[str, Any] | None:
        """Look one barcode up.

        Returns the parcel object, or ``None`` when DAO does not know the
        code — ``{"found": false}`` with HTTP 200 is the route's normal
        unknown/not-yet-registered answer, never an error. Retries once
        against a freshly read page when the request is rejected outright,
        then gives up.
        """
        for attempt in range(2):
            config = await self._discover(force=attempt == 1)
            try:
                async with self._session.get(
                    str(config["endpoint"]),
                    params={"barcode": barcode, "lang": self.language},
                    headers=self._headers(config),
                ) as response:
                    payload: Any = None
                    try:
                        payload = await response.json(content_type=None)
                    except ValueError:
                        payload = None

                    code = payload.get("code") if isinstance(payload, dict) else None
                    if response.status == 403 or (
                        response.status == 429 and code == BOT_BANNED_CODE
                    ):
                        if response.status == 403 and attempt == 0:
                            # A rejected request can simply be a stale nonce;
                            # one fresh page read is the cheap fix. A second
                            # rejection is treated as the ban it looks like.
                            self._config = None
                            continue
                        raise DAOTrackingBannedError(
                            f"DAO rejected the tracking request (HTTP "
                            f"{response.status}, code {code})",
                            status_code=response.status,
                            code=code,
                        )
                    if response.status == 429:
                        raise DAOTrackingApiError(
                            "DAO rate-limited the tracking request",
                            status_code=429,
                            retry_after=_parse_retry_after(
                                response.headers.get("Retry-After")
                            ),
                            code=code,
                        )
                    if response.status != 200:
                        raise DAOTrackingApiError(
                            f"DAO tracking lookup returned HTTP {response.status}",
                            status_code=response.status,
                            code=code,
                        )
                    if not isinstance(payload, dict):
                        raise DAOTrackingApiError(
                            "DAO tracking lookup returned non-JSON"
                        )
            except aiohttp.ClientError as err:
                raise DAOTrackingApiError(f"DAO tracking lookup failed: {err}") from err

            if payload.get("found") is not True:
                return None
            parcel = payload.get("parcel")
            if not isinstance(parcel, dict):
                raise DAOTrackingApiError(
                    "DAO reported a found parcel without a parcel object"
                )
            return parcel

        raise AssertionError(  # pragma: no cover - the loop returns or raises
            "unreachable"
        )
