"""Coordinator for the DAO parcel tracker integration.

Fetching and event firing only — the parcel mapping lives in :mod:`.parcels`,
shared verbatim with the account-less variant.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DAOApiClient,
    DAOApiError,
    DAOAuthError,
)
from .const import (
    CONF_INCLUDE_HISTORY,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)
from .parcels import apply_delivered_filter, normalize_parcel, sort_parcels_by_ts

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


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    midnight_tomorrow = (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    return midnight_tomorrow


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int:
    """Tier for the account-based model (Section 2.2).

    Unlike the barcode-based model this never returns ``None`` — a single
    account call already returns the full state, so the mid-tier poll is
    also the only way to discover a new shipment.
    """
    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES

    return MID_INTERVAL_MINUTES


def _next_update_interval(now: datetime, tier_minutes: int, entry_id: str) -> timedelta:
    """Turn a tier into the coordinator's next ``update_interval``.

    Clamp the naive next-due time forward to the next anchor whenever it
    would land inside the quiet window — including when ``now`` itself is
    already inside it (an anchor poll computing its own follow-up).
    """
    if _in_quiet_window(now):
        return _next_anchor(now) - now

    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


class DAOCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls the account's parcel list and publishes the canonical lists.

    ``coordinator.data`` is the active (not-yet-delivered) parcels,
    ``self.delivered`` the rest.
    """

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
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
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
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
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
        """Fetch the account's parcels and split into active vs delivered.

        ``aiohttp.ClientError`` and a non-429 ``DAOApiError`` are
        deliberately not caught — ``DataUpdateCoordinator`` turns those into
        ``UpdateFailed`` with backoff on its own. An expired session needs
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
        self._consecutive_429 = 0

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, include_history=include_history) for raw in raws
        ]
        active = [parcel for parcel in normalized if not parcel["delivered"]]
        delivered = [parcel for parcel in normalized if parcel["delivered"]]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set.
        incoming = normalized_active + self.delivered
        self._fire_change_events(incoming)
        self._known_state = {
            parcel["barcode"]: parcel["status"]
            for parcel in incoming
            if parcel.get("barcode")
        }
        self._known_delivery_times = {
            parcel["barcode"]: (parcel.get("planned_from"), parcel.get("planned_to"))
            for parcel in incoming
            if parcel.get("barcode")
        }

        self.last_success_time = datetime.now(timezone.utc)

        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(normalized_active, now)
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new versus already present before HA started.

        The event contract, identical across the suite:

        * every payload is the full normalised parcel plus ``device_id``;
        * the hop **to** ``delivered`` fires only ``_parcel_delivered``, never
          also ``_parcel_status_changed``;
        * a barcode first seen already-delivered fires nothing;
        * ``registered`` only fires for a new, not-yet-delivered barcode;
        * an ETA going ``value → null`` is intentionally silent — the carrier
          just lost the window, which is not worth waking someone up for.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )
