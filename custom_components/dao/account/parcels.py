"""Canonical parcel shape, status mapping and list helpers for DAO.

No I/O and no Home Assistant objects beyond the config entry's options: this
is the carrier-specific status mapping and canonical-shape logic, kept apart
from the coordinator (fetching, caching, events) so it stays trivially
unit-testable without spinning up HA.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry

from ..const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)
from ..status import (  # noqa: F401 - re-exported for the pre-split import path
    _CONFIRMED_CODE_OVERRIDES,
    _CONFIRMED_STATUS_CODES,
    _STATUS_MAP,
    _STATUS_OK_CONFIRMED_CODES,
    BOUND_INCOMING,
    BOUND_OUTGOING,
    NEW_ISSUE_URL,
    TRACKING_CODE_MAP,
    _assumed_ok_split_warned,
    _unmapped_bounds_logged,
    _unmapped_status_codes_logged,
    _unmapped_statuses_logged,
    _unverified_history_warned,
    _warn_assumed_ok_split,
    _warn_unmapped_bound,
    _warn_unmapped_status,
    _warn_unmapped_status_code,
    _warn_unverified_history,
    map_event_status,
    map_parcel_status,
    map_tracking_code,
    reset_one_shot_warnings,
)


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
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
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
            return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
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

    ``pickup`` is true as soon as DAO names a pickup point for the parcel —
    confirmed on a real parcel still in transit (issue #17) — and not only
    once it has arrived; a delivered parcel is excluded, because the id it
    was collected from stays on the payload.
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
    # "Bound for a pickup point", not "waiting at one": DAO fills
    # `delivery.pickupPointId` while the parcel is still moving, so the
    # status alone would under-report it until arrival (issue #17). A
    # delivered parcel keeps the id it was collected from, which is neither
    # phase, so it is excluded.
    is_pickup = status is ParcelStatus.AT_PICKUP_POINT or (
        pickup_point_id is not None and not delivered
    )

    if status_code is not None:
        _warn_unmapped_status_code(status_code)

    pickup_point = raw.get("pickupPoint") or {}
    pickup_point_name = pickup_point.get("name") or None

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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
