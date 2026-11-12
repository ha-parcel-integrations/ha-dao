"""Coordinator for the keyless barcode source.

The suite's **account-less** polling model, which this repo did not have
before: each tracked code is one lookup, and polling stops entirely while
nothing is tracked or every tracked code is delivered. The options flow's
update listener is the resume path.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..const import (
    BAN_BACKOFF_CAP_SECONDS,
    BAN_BACKOFF_START_SECONDS,
    CONF_BARCODE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    RATE_LIMIT_BACKOFF_CAP_SECONDS,
    RATE_LIMIT_BACKOFF_START_SECONDS,
    TRACKING_FETCH_GAP_SECONDS,
    ParcelStatus,
)
from ..events import (
    fire_incoming_change_events,
    snapshot_delivery_times,
    snapshot_states,
)
from ..polling import next_update_interval, tracked_tier_minutes
from .api import DAOTrackingApiClient, DAOTrackingApiError, DAOTrackingBannedError
from .parcels import (
    apply_delivered_filter,
    normalize_tracking_parcel,
    parcel_key,
    sort_parcels_by_ts,
)

_LOGGER = logging.getLogger(__name__)


class DAOTrackingCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls each tracked code and publishes the canonical parcel lists.

    Exposes the same surface as the account coordinator — ``data``,
    ``delivered``, ``outgoing``, ``delivered_outgoing``, ``last_success_time``
    and ``current_tier_minutes`` — so ``sensor.py``, ``calendar.py`` and
    ``button.py`` work against either source untouched. ``outgoing`` and
    ``delivered_outgoing`` stay empty for good: the public route has no
    sender view at all.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: DAOTrackingApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            # Recomputed at the end of every refresh; the mid cadence is the
            # safe start for a route that polices traffic.
            update_interval=timedelta(minutes=MID_INTERVAL_MINUTES),
        )
        self._client = client
        self.delivered: list[dict] = []
        # Always empty: the public route is recipient-side only. Kept so the
        # shared platforms need no source check.
        self.outgoing: list[dict] = []
        self.delivered_outgoing: list[dict] = []
        self._consecutive_429 = 0
        # Consecutive bot-check rejections, for the much longer ban backoff.
        self._consecutive_bans = 0
        # Set while one lookup in this poll was rate-limited, so the rest of
        # the poll stops asking and the next one is pushed out.
        self._rate_limited = False
        self._current_tier_minutes: int | None = None
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: dict[str, tuple[str | None, str | None]] | None = (
            None
        )
        self._cached_device_id: str | None = None
        # code -> last raw payload, so a delivered parcel keeps its data
        # without being refetched, and a failed lookup falls back to what we
        # already had rather than blanking the sensor.
        self._raw_cache: dict[str, dict[str, Any] | None] = {}
        # Codes whose parcel is delivered: terminal, never fetched again.
        self._delivered_codes: set[str] = set()
        self.last_success_time: datetime | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY)
        )

    def _tracked(self) -> list[str]:
        """Return the tracked codes from the entry's options."""
        return [
            item[CONF_BARCODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_BARCODE)
        ]

    async def _async_fetch(self, code: str) -> dict[str, Any] | None:
        """Look one code up, degrading to the cached payload on failure.

        A per-code failure is a per-code problem: one unreachable or
        malformed lookup must not fail the whole poll. A bot-check rejection
        is the exception — it will hit every other code too, so it is left to
        propagate and fail the refresh as a whole.
        """
        if self._rate_limited:
            # Another code in this same poll was already turned away. Asking
            # again is what keeps a short-window limiter closed, so the rest
            # of the poll is served from the cache.
            return self._raw_cache.get(parcel_key(code))
        try:
            return await self._client.async_get_parcel(code)
        except DAOTrackingBannedError:
            raise
        except (DAOTrackingApiError, aiohttp.ClientError) as err:
            if isinstance(err, DAOTrackingApiError) and err.status_code == 429:
                self._rate_limited = True
                _LOGGER.debug("DAO rate-limited the lookup for %s", code)
            else:
                _LOGGER.debug("DAO tracking lookup failed for %s: %s", code, err)
            return self._raw_cache.get(parcel_key(code))

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self._async_poll()
        except Exception:
            # The base class keeps the last interval after a failure, which
            # after an overnight poll is the whole quiet window.
            now = dt_util.now()
            tier = tracked_tier_minutes(self.data or [], now)
            self.update_interval = next_update_interval(
                now,
                tier if tier is not None else MID_INTERVAL_MINUTES,
                self.config_entry.entry_id,
            )
            raise

    async def _async_poll(self) -> list[dict]:
        """Fetch every tracked code and split into active vs delivered."""
        tracked = self._tracked()
        tracked_keys = {parcel_key(code) for code in tracked}

        # Keep the caches bounded to what the user still follows.
        self._raw_cache = {
            key: raw for key, raw in self._raw_cache.items() if key in tracked_keys
        }
        self._delivered_codes &= tracked_keys

        # A delivered parcel's payload can never change again, so it is
        # dropped from the fetch — not from the options list, which stays
        # untouched until the user removes the code by hand.
        to_fetch = [
            code for code in tracked if parcel_key(code) not in self._delivered_codes
        ]

        self._rate_limited = False
        fetched: list[tuple[str, dict[str, Any] | None]] = []
        try:
            for index, code in enumerate(to_fetch):
                if index:
                    # Trickle rather than burst: the limiter counts requests
                    # per short window, not per parcel.
                    await asyncio.sleep(TRACKING_FETCH_GAP_SECONDS)
                fetched.append((code, await self._async_fetch(code)))
        except DAOTrackingBannedError as err:
            self._consecutive_bans += 1
            retry_after = min(
                BAN_BACKOFF_START_SECONDS * 2 ** (self._consecutive_bans - 1),
                BAN_BACKOFF_CAP_SECONDS,
            )
            raise UpdateFailed(
                "DAO's bot check rejected the tracking lookup", retry_after=retry_after
            ) from err
        self._consecutive_bans = 0

        for code, raw in fetched:
            self._raw_cache[parcel_key(code)] = raw

        rate_limit_backoff: int | None = None
        if self._rate_limited:
            self._consecutive_429 += 1
            rate_limit_backoff = min(
                RATE_LIMIT_BACKOFF_START_SECONDS * 2 ** (self._consecutive_429 - 1),
                RATE_LIMIT_BACKOFF_CAP_SECONDS,
            )
            if not any(raw is not None for raw in self._raw_cache.values()):
                # Nothing to show at all — fail the refresh so Home Assistant
                # retries with backoff instead of publishing empty parcels.
                raise UpdateFailed(
                    "DAO rate-limited the tracking lookup (429)",
                    retry_after=rate_limit_backoff,
                )
            _LOGGER.warning(
                "DAO rate-limited this update; showing the parcels from the "
                "previous one and waiting %s minutes before asking again",
                rate_limit_backoff // 60,
            )
        else:
            self._consecutive_429 = 0

        include_history = self._include_history
        normalized = [
            normalize_tracking_parcel(
                code,
                self._raw_cache.get(parcel_key(code)),
                include_history=include_history,
            )
            for code in tracked
        ]

        self._delivered_codes = {
            parcel_key(parcel["barcode"])
            for parcel in normalized
            if parcel["delivered"] and parcel.get("barcode")
        }

        active = [parcel for parcel in normalized if not parcel["delivered"]]
        delivered = [parcel for parcel in normalized if parcel["delivered"]]
        delivered_sorted = sort_parcels_by_ts(
            delivered, "delivered_at", descending=True
        )
        self.delivered = apply_delivered_filter(delivered_sorted, self.config_entry)
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Events and remembered state cover every parcel; the retention
        # option only limits what the delivered sensor shows.
        seen = normalized_active + delivered_sorted
        fire_incoming_change_events(
            self.hass,
            seen,
            self._known_state,
            self._known_delivery_times,
            self._device_id(),
        )
        self._known_state = snapshot_states(seen)
        self._known_delivery_times = snapshot_delivery_times(seen)

        if rate_limit_backoff is None:
            self.last_success_time = datetime.now(UTC)

        now = dt_util.now()
        self._current_tier_minutes = tracked_tier_minutes(normalized_active, now)
        if rate_limit_backoff is not None:
            self.update_interval = timedelta(seconds=rate_limit_backoff)
        else:
            self.update_interval = next_update_interval(
                now, self._current_tier_minutes, self.config_entry.entry_id
            )
        return normalized_active
