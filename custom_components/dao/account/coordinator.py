"""Coordinator for the DAO parcel tracker integration.

Fetching, direction split and event firing. ``api.async_get_parcels()``
already combines both ``bound`` values into one list (Section on API
mechanics), so this coordinator splits that one fetched list into
incoming/outgoing itself rather than needing two coordinators — same
approach as ``ha-posten-bring``'s ``direction`` field.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_TOKEN,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    ParcelStatus,
)
from ..events import (
    fire_incoming_change_events,
    fire_outgoing_change_events,
    snapshot_delivery_times,
    snapshot_states,
)
from ..polling import (  # noqa: F401 - the aliases are re-exported, see below
    account_tier_minutes as _hottest_tier_minutes,
    in_quiet_window as _in_quiet_window,
    next_anchor as _next_anchor,
    next_update_interval as _next_update_interval,
    stagger_minutes as _stagger_minutes,
)
from .client import DAOApiClient, DAOApiError, DAOAuthError
from .parcels import (
    BOUND_OUTGOING,
    apply_delivered_filter,
    normalize_parcel,
    parcel_direction,
    sort_parcels_by_ts,
)

# The pre-split private names (``_hottest_tier_minutes`` and friends) are
# aliased above rather than redefined: the algorithm itself now lives in
# ``polling.py``, shared with the tracking source, and the aliases keep the
# existing import path working.

_LOGGER = logging.getLogger(__name__)

# Base for the 429 backoff when the carrier's response carries no
# ``Retry-After`` of its own: ``BACKOFF_BASE_SECONDS * 2**consecutive_429``,
# capped at ``BACKOFF_CAP_SECONDS``.
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600

# Bounds how many detail/pickup-point calls run concurrently during one
# refresh's enrichment step — a large inbox must not fire dozens of requests
# at once just because asyncio.gather lets it.
ENRICH_CONCURRENCY = 3


class DAOCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls the account's parcel list and publishes incoming/outgoing lists.

    ``coordinator.data`` is active incoming parcels, ``self.delivered`` the
    delivered incoming ones. ``self.outgoing``/``self.delivered_outgoing``
    are the ``bound: "OUT"`` counterparts.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: DAOApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passing config_entry makes self.config_entry available on the
            # base class, which every helper below relies on.
            config_entry=entry,
            name=DOMAIN,
            # Recomputed at the end of every refresh (Section 2.2's tiering) —
            # start with the hot cadence so the very first poll, right after
            # setup, happens promptly regardless of what it finds.
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self._client = client
        self.delivered: list[dict] = []
        self.outgoing: list[dict] = []
        self.delivered_outgoing: list[dict] = []
        # Consecutive 429 responses, for the exponential backoff in Section 3.
        # Reset to 0 on any success.
        self._consecutive_429 = 0
        # Last tier computed by _hottest_tier_minutes — surfaced in
        # diagnostics.
        self._current_tier_minutes: int | None = None
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started — otherwise every
        # restart would flood users with "registered" notifications.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: dict[str, tuple[str | None, str | None]] | None = (
            None
        )
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this account's device.
        self._cached_device_id: str | None = None
        # trackingId -> {"events": [...], "_status_code": raw statusCode}.
        # The detail call is an extra request per parcel, made only when the
        # history option is on, and only replayed when a parcel's raw status
        # code changes — history only grows on a status change, so a parcel
        # whose statusCode is unchanged since the last poll (a terminal,
        # delivered one included) is skipped rather than refetched. Lives
        # for the coordinator's lifetime (resets on HA restart); the
        # pickup-point cache lives on the client instead, since it is keyed
        # by pickup-point id rather than by parcel and outlives any single
        # parcel's tracked lifetime.
        self._history_cache: dict[str, dict[str, Any]] = {}
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    def _persist_refresh_token(self) -> None:
        """Store the client's refresh token if a refresh rotated it."""
        token = self._client.refresh_token
        if not token or token == self.config_entry.data.get(CONF_REFRESH_TOKEN):
            return
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_REFRESH_TOKEN: token},
        )

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

    async def _enrich_parcel(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Merge one parcel's ``events[]`` and resolved pickup point into ``raw``.

        Best-effort: a failure here degrades to no history / no pickup point
        for this one parcel rather than failing the whole poll — one bad
        parcel must not take the rest down (see CLAUDE.md). ``DAOAuthError``
        is the one exception let through: a session that just died will fail
        identically for every other parcel too, so it is left to propagate
        and become the same reauth the main list call would have triggered.
        """
        tracking_id = raw.get("trackingId")
        delivery = raw.get("delivery") or {}
        pickup_point_id = delivery.get("pickupPointId")
        status_code = ((raw.get("lastEvent") or {}).get("status") or {}).get(
            "statusCode"
        )

        if self._include_history and tracking_id:
            cached = self._history_cache.get(tracking_id)
            if cached is not None and cached.get("_status_code") == status_code:
                raw = {**raw, "events": cached.get("events")}
            else:
                try:
                    detail = await self._client.async_get_parcel_detail(tracking_id)
                except DAOAuthError:
                    raise
                except (DAOApiError, aiohttp.ClientError) as err:
                    _LOGGER.debug(
                        "DAO detail fetch failed for %s: %s", tracking_id, err
                    )
                else:
                    events = detail.get("events")
                    self._history_cache[tracking_id] = {
                        "_status_code": status_code,
                        "events": events,
                    }
                    raw = {**raw, "events": events}

        if pickup_point_id:
            try:
                pickup_point = await self._client.async_get_pickup_point(
                    str(pickup_point_id)
                )
            except DAOAuthError:
                raise
            except (DAOApiError, aiohttp.ClientError) as err:
                _LOGGER.debug(
                    "DAO pickup-point fetch failed for %s: %s", pickup_point_id, err
                )
            else:
                raw = {**raw, "pickupPoint": pickup_point}

        return raw

    async def _enrich_parcels(self, raws: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enrich every raw parcel concurrently, bounded by ``ENRICH_CONCURRENCY``."""
        semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY)

        async def _bounded(raw: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await self._enrich_parcel(raw)

        return await asyncio.gather(*(_bounded(raw) for raw in raws))

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self._async_poll()
        except Exception:
            # The base class keeps the last interval after a failure, which
            # after an overnight poll is the whole quiet window.
            now = dt_util.now()
            self.update_interval = _next_update_interval(
                now,
                _hottest_tier_minutes((self.data or []) + self.outgoing, now),
                self.config_entry.entry_id,
            )
            raise

    async def _async_poll(self) -> list[dict]:
        """Fetch the account's parcels and split into active vs delivered.

        ``aiohttp.ClientError`` and a non-429 ``DAOApiError`` are
        deliberately not caught — ``DataUpdateCoordinator`` turns those into
        a failed update and the wrapper above reschedules it. An expired session needs
        special handling, because retrying it forever would never recover; a
        429 needs its own handling too, for the Section 3 backoff.
        """
        try:
            raws = await self._client.async_get_parcels()
            raws = await self._enrich_parcels(raws)
        except DAOAuthError as err:
            raise ConfigEntryAuthFailed("DAO session expired") from err
        except DAOApiError as err:
            if err.status_code != 429:
                raise
            self._consecutive_429 += 1
            retry_after = err.retry_after or min(
                BACKOFF_BASE_SECONDS * 2**self._consecutive_429, BACKOFF_CAP_SECONDS
            )
            raise UpdateFailed(
                "DAO rate-limited (429)", retry_after=retry_after
            ) from err
        finally:
            # A refresh inside a poll that later fails still rotated the
            # token; losing it would leave a restart holding a dead one.
            self._persist_refresh_token()
        self._consecutive_429 = 0

        include_history = self._include_history
        incoming_raw = [r for r in raws if parcel_direction(r) != BOUND_OUTGOING]
        outgoing_raw = [r for r in raws if parcel_direction(r) == BOUND_OUTGOING]

        normalized = [
            normalize_parcel(raw, include_history=include_history)
            for raw in incoming_raw
        ]
        active = [parcel for parcel in normalized if not parcel["delivered"]]
        delivered = [parcel for parcel in normalized if parcel["delivered"]]

        delivered_sorted = sort_parcels_by_ts(
            delivered, "delivered_at", descending=True
        )
        self.delivered = apply_delivered_filter(delivered_sorted, self.config_entry)
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Events and remembered state cover every parcel; the retention option
        # only limits what the delivered sensor shows.
        incoming = normalized_active + delivered_sorted
        self._fire_change_events(incoming)
        self._known_state = snapshot_states(incoming)
        self._known_delivery_times = snapshot_delivery_times(incoming)

        normalized_outgoing = [
            normalize_parcel(raw, include_history=include_history)
            for raw in outgoing_raw
        ]
        outgoing_active = [p for p in normalized_outgoing if not p["delivered"]]
        outgoing_delivered = [p for p in normalized_outgoing if p["delivered"]]
        delivered_outgoing_sorted = sort_parcels_by_ts(
            outgoing_delivered, "delivered_at", descending=True
        )
        self.delivered_outgoing = apply_delivered_filter(
            delivered_outgoing_sorted, self.config_entry
        )
        self.outgoing = sort_parcels_by_ts(outgoing_active, "planned_from")
        outgoing_all = self.outgoing + delivered_outgoing_sorted
        self._fire_outgoing_change_events(outgoing_all)
        self._known_outgoing_state = snapshot_states(outgoing_all)

        self.last_success_time = datetime.now(UTC)

        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(
            normalized_active + outgoing_active, now
        )
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire the incoming event set for this poll. Contract: ``events.py``."""
        fire_incoming_change_events(
            self.hass,
            parcels,
            self._known_state,
            self._known_delivery_times,
            self._device_id(),
        )

    def _fire_outgoing_change_events(self, parcels: list[dict]) -> None:
        """Fire the outgoing (sender-side) event subset. Contract: ``events.py``."""
        fire_outgoing_change_events(
            self.hass, parcels, self._known_outgoing_state, self._device_id()
        )
