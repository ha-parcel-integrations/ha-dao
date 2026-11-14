"""The public barcode payload, normalised to the canonical parcel shape.

No I/O and no Home Assistant objects: the mapping is a pure function of the
payload, so it stays unit-testable without HA. The status vocabulary itself
lives in :mod:`dao.status`, shared with the account source, so one parcel
cannot be reported differently depending on which source saw it.
"""

from __future__ import annotations

from datetime import datetime
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

# Generic list helpers (sorting, the delivered-retention filter, the
# deep-link builder) are shared with the account source rather than
# reimplemented — the same arrangement ha-bpost uses for its two sources.
from ..account.parcels import (  # noqa: F401 - re-exported for the platforms
    apply_delivered_filter,
    parse_iso,
    sort_parcels_by_ts,
    tracking_url,
)
from ..const import HISTORY_MAX_EVENTS, ParcelStatus
from ..status import NEW_ISSUE_URL, map_tracking_code

_LOGGER = logging.getLogger(__name__)

# DAO is a Danish carrier and this route stamps its raw event times in local
# time without an offset, so they are read as Copenhagen time rather than UTC.
CARRIER_TZ = ZoneInfo("Europe/Copenhagen")

_TIME_ONLY_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_DAY_RE = re.compile(r"(\d{1,2})")
# A day-first date and time, the Danish written convention: "25-10-2026
# 08:38:48" and its "." / "/" separators. Only used when the value is not
# ISO, and it warns — for a day below 13 the field order cannot be told
# apart from month-first by looking at the value.
_DAY_FIRST_RE = re.compile(
    r"^(\d{1,2})[-./](\d{1,2})[-./](\d{4})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$"
)

# The page is requested in Danish or English (``lang``), so a localised
# ``date.day`` can arrive in either. A closed 24-name table is not a guess;
# anything outside it yields no timestamp rather than an invented one.
_MONTHS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "maj": 5,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# One-shot warning bookkeeping for this source. Module-level by design: one
# warning per HA session, not one per poll.
_assumed_timestamp_warned = False


def reset_one_shot_warnings() -> None:
    """Forget this module's "already warned" flag. For tests only."""
    global _assumed_timestamp_warned  # noqa: PLW0603 - test hook
    _assumed_timestamp_warned = False


def _warn_assumed_timestamp() -> None:
    """Log once, ever: an event time was reconstructed from two fields.

    ``raw.tidspunkt`` carried only a clock time, so the date had to come from
    the localised ``date`` block beside it. That combination has never been
    confirmed against a documented payload, so it is flagged rather than
    trusted silently.
    """
    global _assumed_timestamp_warned  # noqa: PLW0603 - one-shot warning flag; tests reset it
    if _assumed_timestamp_warned:
        return
    _assumed_timestamp_warned = True
    _LOGGER.warning(
        "DAO's public tracking lookup returned an event time without a date "
        "(tidspunkt), so the date was taken from the event's localised date "
        "field. If a history entry's date looks wrong, open an issue and "
        "paste this line: %s",
        NEW_ISSUE_URL,
    )


def event_timestamp(event: dict[str, Any]) -> str | None:
    """Return one public-route event's time as an ISO 8601 string, or ``None``.

    ``raw.tidspunkt`` is Copenhagen local time, confirmed on ten real parcels
    (2026-11-11) as ``YYYY-MM-DD HH:MM:SS`` — which :func:`datetime.fromisoformat`
    reads directly. The other two branches are fallbacks for shapes DAO has
    not been seen to send, and they say so by warning once. Nothing is ever
    dated from "today": an event we cannot place in time keeps no timestamp
    and is dropped from history, which is strictly better than a wrong date.
    """
    raw = event.get("raw") or {}
    stamp = raw.get("tidspunkt")
    text = str(stamp).strip() if stamp is not None else ""

    if text:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=CARRIER_TZ)
            return parsed.isoformat()

    day_first = _DAY_FIRST_RE.match(text) if text else None
    if day_first is not None:
        day, month, year, hour, minute, second = day_first.groups()
        try:
            stamped = datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second or 0),
                tzinfo=CARRIER_TZ,
            )
        except ValueError:
            return None
        _warn_assumed_timestamp()
        return stamped.isoformat()

    date = event.get("date") or {}
    time_match = _TIME_ONLY_RE.match(text) if text else None
    if time_match is None:
        # No usable clock time of our own; the display block alone carries no
        # unambiguous time either.
        return None

    year = date.get("year")
    day_text = str(date.get("day") or "")
    day_match = _DAY_RE.search(day_text)
    month = next(
        (number for name, number in _MONTHS.items() if name in day_text.lower()[:32]),
        None,
    )
    if not year or day_match is None or month is None:
        return None
    try:
        reconstructed = datetime(
            int(year),
            month,
            int(day_match.group(1)),
            int(time_match.group(1)),
            int(time_match.group(2)),
            int(time_match.group(3) or 0),
            tzinfo=CARRIER_TZ,
        )
    except ValueError:
        return None
    _warn_assumed_timestamp()
    return reconstructed.isoformat()


def event_code(event: dict[str, Any]) -> Any:
    """Return an event's DAO code (``raw.haendelse``), or ``None``."""
    return (event.get("raw") or {}).get("haendelse")


def event_description(event: dict[str, Any]) -> str | None:
    """Return an event's own text, preferring the rendered description."""
    return event.get("description") or (event.get("raw") or {}).get("beskrivelse")


def newest_event(parcel: dict[str, Any]) -> dict[str, Any] | None:
    """Return the most recent event.

    This route returns ``events`` newest first, so the head is the newest —
    but the head is only trusted when it carries a code, so a malformed
    leading entry cannot decide a parcel's status.
    """
    for event in parcel.get("events") or []:
        if isinstance(event, dict) and event_code(event) is not None:
            return event
    return None


def build_history(
    parcel: dict[str, Any], *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the public route's ``events[]``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across the
    suite, and top-level (not under ``raw``) so it survives the aggregator's
    ``strip_raw()``. Sorted oldest → newest (the payload is newest first) and
    capped to the most recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    for event in parcel.get("events") or []:
        if not isinstance(event, dict):
            continue
        timestamp = event_timestamp(event)
        if not timestamp:
            continue
        code = event_code(event)
        parsed = parse_iso(timestamp)
        if parsed is None:  # pragma: no cover - event_timestamp emits ISO only
            continue
        parseable.append(
            (
                parsed,
                {
                    "timestamp": timestamp,
                    "status": map_tracking_code(code, event_description(event))
                    if code is not None
                    else None,
                    "raw_status": event_description(event) or code,
                },
            )
        )
    parseable.sort(key=lambda item: item[0])
    return [entry for _, entry in parseable][-max_events:]


def normalize_tracking_parcel(
    code: str, parcel: dict[str, Any] | None, *, include_history: bool = False
) -> dict:
    """Return a canonical parcel dict for one tracked code.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order, and the aggregator and
    cross-carrier dashboards depend on it.

    ``parcel`` is ``None`` when DAO does not know the code yet — a freshly
    added or not-yet-registered number. That still yields a full parcel dict
    (status ``unknown``, everything optional ``None``) so the user gets a
    sensor for the code they just added instead of nothing at all.

    ``planned_from``/``planned_to`` stay ``None``: this route carries no
    delivery window either (``const.py``'s ``CAPABILITIES_BY_VARIANT``).
    ``weight``/``dimensions`` stay ``None``: both are confirmed absent from
    every DAO tracking payload.
    """
    parcel = parcel or {}
    latest = newest_event(parcel)
    status = (
        map_tracking_code(event_code(latest), event_description(latest))
        if latest
        else ParcelStatus.UNKNOWN
    )
    delivered = status is ParcelStatus.DELIVERED

    pickup_point = parcel.get("pickup") or {}
    recipient = parcel.get("recipient") or {}
    latest_event = parcel.get("latest_event") or {}

    return {
        "carrier": "DAO",
        "barcode": parcel.get("barcode") or code,
        "sender": parcel.get("sender_masked") or None,
        "receiver": recipient.get("name") or None,
        "status": status,
        "raw_status": latest_event.get("description")
        or (event_description(latest) if latest else None),
        "delivered": delivered,
        "delivered_at": (event_timestamp(latest) if latest else None)
        if delivered
        else None,
        "planned_from": None,
        "planned_to": None,
        # The public route names the daoSHOP as soon as the parcel is routed
        # to one — which is what the contract's "bound for a pickup point"
        # actually means. The account source reads its own
        # `delivery.pickupPointId` for the same phase.
        "pickup": bool(pickup_point) or status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": pickup_point.get("name") or None,
        "url": tracking_url(parcel.get("barcode") or code),
        "weight": None,
        "dimensions": None,
        "history": build_history(parcel) if include_history else None,
        "raw": parcel,
    }


def parcel_key(code: str) -> str:
    """Return the dedup key for a tracked code.

    DAO publishes no barcode format, so the key is the trimmed code itself —
    nothing is upper-cased or stripped, since a guessed transform could turn
    a working code into one the endpoint rejects.
    """
    return (code or "").strip()
