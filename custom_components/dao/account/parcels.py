"""Canonical parcel shape, status mapping and list helpers for DAO.

No I/O and no Home Assistant objects beyond the config entry's options: this
is the carrier-specific status mapping and canonical-shape logic, kept apart
from the coordinator (fetching, caching, events) so it stays trivially
unit-testable without spinning up HA.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
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


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order, and the aggregator and
    cross-carrier dashboards depend on it.

    ``pickup`` is true as soon as DAO names a pickup point for the parcel —
    confirmed on a real parcel still in transit (issue #17) — and stays true
    once the parcel is collected, since the id it was collected from stays on
    the payload.
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
    # `delivery.pickupPointId` is filled while the parcel is still moving and
    # stays after collection (code 51), so it marks the route, not the phase.
    pickup = is_pickup(
        pickup_point_id is not None,
        status,
        (
            map_event_status(
                (event.get("status") or {}).get("statusType"),
                (event.get("status") or {}).get("statusCode"),
                pickup_point_id,
            )
            for event in raw.get("events") or []
            if isinstance(event, dict)
        ),
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
        "contents": None,
        "status": status,
        "raw_status": last_status.get("statusText") or status_code,
        "delivered": delivered,
        "delivered_at": to_iso_timestamp(last_event.get("timestamp"))
        if delivered
        else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": pickup_point_name if pickup else None,
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
