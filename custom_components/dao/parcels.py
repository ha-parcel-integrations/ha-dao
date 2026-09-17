"""Canonical parcel shape, status mapping and list helpers for DAO.

No I/O and no Home Assistant objects beyond the config entry's options: this
is the carrier-specific status mapping and canonical-shape logic, kept apart
from the coordinator (fetching, caching, events) so it stays trivially
unit-testable without spinning up HA.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet, or an ASSUMED mapping that
# turns out to be wrong. Points at the pre-filled issue template rather than
# a blank form, so a user following this link from their log lands somewhere
# that already asks the right questions.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-dao/issues/new"
    "?template=unrecognised_status.yml"
)

# The closed, confirmed six-value `statusType` enum. `STATUS_OK` is handled
# separately in `_resolve_status_type` because it splits on
# `delivery.pickupPointId` rather than mapping to a single ParcelStatus.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "STATUS_PROCESSING": ParcelStatus.REGISTERED,
    "STATUS_IN_TRANSIT": ParcelStatus.IN_TRANSIT,
    "STATUS_RETURN": ParcelStatus.RETURNING,
    "STATUS_ALERT": ParcelStatus.PROBLEM,
    "STATUS_INFO": ParcelStatus.UNKNOWN,
}

# `statusCode` values under `STATUS_OK` that a real parcel has confirmed,
# overriding the ASSUMED `delivery.pickupPointId` split below. 51 ("Pakken er
# udleveret" — handed over) was reported on issue #7 for a parcel that *did*
# carry a `pickupPointId` (it was collected from that point) and still reads
# as delivered, not "awaiting pickup" — the presence of a pickup point alone
# is not a reliable signal for "still waiting there".
_STATUS_OK_CONFIRMED_CODES: dict[int, ParcelStatus] = {
    51: ParcelStatus.DELIVERED,
}

# Directions seen on the wire. "IN" is the default for anything unrecognised
# rather than silently dropping a parcel.
BOUND_INCOMING = "IN"
BOUND_OUTGOING = "OUT"

# One-shot warning bookkeeping, mirrored across the suite: log once per HA
# session (per distinct value), not on every poll.
_unmapped_statuses_logged: set[str] = set()
_unmapped_status_codes_logged: set[str] = set()
_unmapped_bounds_logged: set[str] = set()
# Whether the ASSUMED STATUS_OK split has already been flagged this session.
# A bool, not a set: the thing being flagged is the mapping mechanism itself,
# not a particular value, so it only needs to fire once, ever.
_assumed_ok_split_warned = False
# Whether the detail-call (events[]) / pickup-point shapes have already been
# flagged, ever. Both were settled by reading the app's own decompiled code,
# never by a live call — same standing as the STATUS_OK split above, so they
# get the same one-shot treatment the moment either actually produces data.
_unverified_history_warned = False
_unverified_pickup_point_warned = False


def _warn_unmapped_status(status_type: str) -> None:
    """Log an unmapped statusType once. Defensive: the enum is closed at six."""
    if status_type in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(status_type)
    _LOGGER.warning(
        "Unrecognised DAO statusType — help us map it. Open an issue and "
        "paste this line: %s\n  statusType=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        status_type,
    )


def _warn_unmapped_status_code(status_code: Any) -> None:
    """Log a DAO statusCode the first time it is seen.

    There is no known table for this field at all beyond the confirmed
    entries in `_STATUS_OK_CONFIRMED_CODES`, so every distinct value is
    reported the same way an unmapped statusType would be — it is never used
    to derive `status` on its own (only as a lookup key against that small
    confirmed table).
    """
    key = str(status_code)
    if key in _unmapped_status_codes_logged:
        return
    _unmapped_status_codes_logged.add(key)
    _LOGGER.warning(
        "DAO reported statusCode=%s, which has no known mapping yet. Open an "
        "issue and paste this line so the code table can be built: %s",
        status_code,
        NEW_ISSUE_URL,
    )


def _warn_unmapped_bound(bound: str) -> None:
    """Log an unrecognised DAO `bound` value once. Defensive: only IN/OUT are confirmed."""
    if bound in _unmapped_bounds_logged:
        return
    _unmapped_bounds_logged.add(bound)
    _LOGGER.warning(
        "Unrecognised DAO bound=%s — treated as incoming. Open an issue and "
        "paste this line: %s",
        bound,
        NEW_ISSUE_URL,
    )


def _warn_assumed_ok_split(pickup_point_id: Any) -> None:
    """Log once, ever: the STATUS_OK → delivered/at_pickup_point split is a guess.

    Only reached for a `statusCode` outside `_STATUS_OK_CONFIRMED_CODES` — a
    confirmed code (currently just 51, "handed over") never falls through to
    this assumption. Fires the first time either branch is exercised, not
    only the pickup-point one, so a "delivered" mis-mapping gets reported
    just as readily.
    """
    global _assumed_ok_split_warned
    if _assumed_ok_split_warned:
        return
    _assumed_ok_split_warned = True
    resolved = ParcelStatus.AT_PICKUP_POINT if pickup_point_id else ParcelStatus.DELIVERED
    _LOGGER.warning(
        "DAO reported STATUS_OK, mapped to '%s' based on an unverified "
        "assumption about delivery.pickupPointId (%s). Open an issue and "
        "paste this line so the split can be confirmed: %s",
        resolved,
        "set" if pickup_point_id else "unset",
        NEW_ISSUE_URL,
    )


def _warn_unverified_history() -> None:
    """Log once, ever: the ``events[]`` shape has never been seen from a live call.

    Its field names come from reading the app's own decompiled code, not
    from an authenticated request. Fires the first time a parcel actually
    carries events to build history from, so it lands the moment the claim
    starts mattering rather than at import time.
    """
    global _unverified_history_warned
    if _unverified_history_warned:
        return
    _unverified_history_warned = True
    _LOGGER.warning(
        "DAO returned parcel history built from an events[] shape that was "
        "only confirmed by reading the app's own code, never by a live "
        "call. If an entry looks wrong, open an issue and paste this line: "
        "%s",
        NEW_ISSUE_URL,
    )


def _warn_unverified_pickup_point() -> None:
    """Log once, ever: the resolved pickup-point shape has never been seen live.

    Same standing as :func:`_warn_unverified_history` — the field names are
    confirmed structurally from the app's own code, not from a real
    ``pickup-points/{id}`` response.
    """
    global _unverified_pickup_point_warned
    if _unverified_pickup_point_warned:
        return
    _unverified_pickup_point_warned = True
    _LOGGER.warning(
        "DAO resolved a pickup point from a shape that was only confirmed "
        "by reading the app's own code, never by a live call. If the name "
        "or address looks wrong, open an issue and paste this line: %s",
        NEW_ISSUE_URL,
    )


def _resolve_status_type(
    status_type: str | None, status_code: Any, pickup_point_id: Any
) -> ParcelStatus | None:
    """Map one statusType, or ``None`` when unrecognised.

    Callers decide the fallback and whether that fallback deserves a warning
    — `map_parcel_status` always warns on `None`, `map_event_status` only
    when a status type was actually present.
    """
    if not status_type:
        return None
    if status_type == "STATUS_OK":
        try:
            confirmed = _STATUS_OK_CONFIRMED_CODES.get(int(status_code))
        except (TypeError, ValueError):
            confirmed = None
        if confirmed is not None:
            return confirmed
        _warn_assumed_ok_split(pickup_point_id)
        return (
            ParcelStatus.AT_PICKUP_POINT if pickup_point_id else ParcelStatus.DELIVERED
        )
    return _STATUS_MAP.get(status_type)


def map_parcel_status(
    status_type: str | None, status_code: Any = None, pickup_point_id: Any = None
) -> ParcelStatus:
    """Map a DAO ``statusType``/``statusCode`` (+ pickup-point presence) to a canonical status.

    A missing status reports ``unknown`` silently; an unrecognised one
    reports ``unknown`` with a one-shot warning.
    """
    resolved = _resolve_status_type(status_type, status_code, pickup_point_id)
    if resolved is not None:
        return resolved
    if status_type:
        _warn_unmapped_status(status_type)
    return ParcelStatus.UNKNOWN


def map_event_status(
    status_type: str | None, status_code: Any = None, pickup_point_id: Any = None
) -> ParcelStatus | None:
    """Map a history entry's statusType to a canonical status, or ``None``.

    Unmapped values keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to
    unknown") and warn once, reusing the parcel-status one-shot set.
    `pickup_point_id` is the *parcel's* current value — individual events
    carry no pickup-point field of their own, so the STATUS_OK split applies
    the same assumption uniformly across a parcel's whole history.
    """
    resolved = _resolve_status_type(status_type, status_code, pickup_point_id)
    if resolved is None and status_type:
        _warn_unmapped_status(status_type)
    return resolved


def parcel_direction(raw: dict) -> str:
    """Return the normalised direction (``IN``/``OUT``) for a raw parcel.

    An unrecognised ``bound`` defaults to incoming with a one-shot warning
    rather than being dropped.
    """
    bound = str(raw.get("bound") or "").strip().upper()
    if bound in (BOUND_INCOMING, BOUND_OUTGOING):
        return bound
    if bound:
        _warn_unmapped_bound(bound)
    return BOUND_INCOMING


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def build_history(
    events: list | None,
    pickup_point_id: Any = None,
    *,
    max_events: int = HISTORY_MAX_EVENTS,
) -> list[dict]:
    """Build the canonical ``history`` list from DAO's detail-call ``events[]``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the event's own
    ``statusText``, falling back to the bare ``statusCode`` only when no
    text is present. Sorted oldest → newest and capped to the most recent
    ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for raw_event in events or []:
        if not isinstance(raw_event, dict):
            continue
        timestamp = to_iso_timestamp(raw_event.get("timestamp"))
        if not timestamp:
            continue
        status = raw_event.get("status") or {}
        status_code = status.get("statusCode")
        if status_code is not None:
            _warn_unmapped_status_code(status_code)
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(
                status.get("statusType"), status_code, pickup_point_id
            ),
            "raw_status": status.get("statusText") or status_code,
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    trimmed = ordered[-max_events:]
    if trimmed:
        _warn_unverified_history()
    return trimmed


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order, and the aggregator and
    cross-carrier dashboards depend on it.

    ``pickup_point`` reads the resolved pickup-point object under
    ``raw["pickupPoint"]`` (fetched separately, per
    ``delivery.pickupPointId``, and merged in before this function ever sees
    the parcel) rather than doing any I/O itself — this function stays pure.
    ``planned_from``/``planned_to`` stay ``None``: no field in the consumed
    shape is confirmed to be a delivery window (see const.py's
    ``CAPABILITIES`` comment). ``weight``/``dimensions`` stay ``None``: both
    are confirmed absent from every tracking payload. ``raw_status`` is
    ``lastEvent.status.statusText`` — the carrier's own human-readable text —
    falling back to the bare ``statusCode`` only when no text is present,
    matching the rest of the suite (`raw_status` is the carrier's own text,
    never a bare code when text is available).
    """
    tracking_id = raw.get("trackingId")
    sender = raw.get("sender") or {}
    receiver = raw.get("receiver") or {}
    last_event = raw.get("lastEvent") or {}
    last_status = last_event.get("status") or {}
    status_type = last_status.get("statusType")
    status_code = last_status.get("statusCode")
    delivery = raw.get("delivery") or {}
    pickup_point_id = delivery.get("pickupPointId")

    status = map_parcel_status(status_type, status_code, pickup_point_id)
    delivered = status is ParcelStatus.DELIVERED
    is_pickup = status is ParcelStatus.AT_PICKUP_POINT

    if status_code is not None:
        _warn_unmapped_status_code(status_code)

    pickup_point = raw.get("pickupPoint") or {}
    pickup_point_name = pickup_point.get("name") or None
    if pickup_point_name:
        _warn_unverified_pickup_point()

    return {
        "carrier": "DAO",
        "barcode": tracking_id,
        "sender": sender.get("name") or None,
        "receiver": receiver.get("name") or None,
        "status": status,
        "raw_status": last_status.get("statusText") or status_code,
        "delivered": delivered,
        "delivered_at": to_iso_timestamp(last_event.get("timestamp"))
        if delivered
        else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": is_pickup,
        "pickup_point": pickup_point_name,
        "url": tracking_url(tracking_id),
        "weight": None,
        "dimensions": None,
        "history": build_history(raw.get("events"), pickup_point_id)
        if include_history
        else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
