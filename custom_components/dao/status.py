"""DAO's status vocabulary, shared by both sources.

The account route reports ``lastEvent.status.statusCode`` and the public
barcode route reports ``events[].raw.haendelse`` — one code space, confirmed
on a real parcel whose timeline appeared on both. The tables and the one-shot
warning bookkeeping therefore live here, one level up from either source, so
neither can drift from the other on what a code means.

No I/O and no Home Assistant objects: this stays trivially unit-testable.
"""

from __future__ import annotations

import logging
from typing import Any

from .const import ParcelStatus

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
    32: ParcelStatus.AT_PICKUP_POINT,
    51: ParcelStatus.DELIVERED,
}

# `statusCode` values that override the `statusType` mapping, whatever type
# carries them. 25 ("Indleveret i daoSHOP" — dropped off at a daoSHOP) arrives
# as `STATUS_PROCESSING` but the parcel is already with DAO, awaiting transport
# from the shop (issue #5), so it is `in_transit`, not `registered`. 11
# ("Ankommet til terminal"), 23 ("På vej til daoSHOP") and 26 ("På vej til
# terminal") are ASSUMED the same on their text alone: the account API's
# `statusType` for them was never seen, the reporter's poll missed both
# overnight.
_CONFIRMED_CODE_OVERRIDES: dict[int, ParcelStatus] = {
    11: ParcelStatus.IN_TRANSIT,
    23: ParcelStatus.IN_TRANSIT,
    25: ParcelStatus.IN_TRANSIT,
    26: ParcelStatus.IN_TRANSIT,
}

# `statusCode` values a real parcel has reported, so they need no warning:
# 1 is `STATUS_PROCESSING`, 12 is `STATUS_IN_TRANSIT` and 32 ("Klar til
# afhentning" — ready for pickup) is `STATUS_OK` (issue #5), 25 and 51 are the
# confirmed codes above, and 11/23/26 are the assumed in-transit ones.
_CONFIRMED_STATUS_CODES = frozenset({"1", "11", "12", "23", "25", "26", "32", "51"})

# The public barcode route's own map. Its `haendelse` is the same code space
# as the account route's `statusCode`, and every value below is the status the
# account route already derives for that code — so one parcel can never be
# reported differently by the two sources. Unlike the account payload there is
# no `statusType` beside it, so this is the whole mapping: an unseen code
# warns and reports `unknown` rather than being guessed from its Danish text
# (maintainer decision, 2026-11-11).
TRACKING_CODE_MAP: dict[int, ParcelStatus] = {
    1: ParcelStatus.REGISTERED,  # Forsendelse er oprettet
    11: ParcelStatus.IN_TRANSIT,  # Ankommet til terminal (ASSUMED)
    12: ParcelStatus.IN_TRANSIT,  # Sorteret på: terminal …
    23: ParcelStatus.IN_TRANSIT,  # På vej til daoSHOP (ASSUMED)
    25: ParcelStatus.IN_TRANSIT,  # Indleveret i daoSHOP
    26: ParcelStatus.IN_TRANSIT,  # På vej til terminal (ASSUMED)
    32: ParcelStatus.AT_PICKUP_POINT,  # Klar til afhentning
    51: ParcelStatus.DELIVERED,  # Pakken er udleveret
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
_unmapped_tracking_codes_logged: set[str] = set()
# Whether the ASSUMED STATUS_OK split has already been flagged this session.
# A bool, not a set: the thing being flagged is the mapping mechanism itself,
# not a particular value, so it only needs to fire once, ever.
_assumed_ok_split_warned = False
# Whether the detail-call (events[]) shape has already been flagged, ever. It
# was settled by reading the app's own decompiled code, never by a live call —
# same standing as the STATUS_OK split above, so it gets the same one-shot
# treatment the moment it actually produces data.
_unverified_history_warned = False


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

    There is no known table for this field beyond `_CONFIRMED_STATUS_CODES`,
    so every other distinct value is reported the same way an unmapped
    statusType would be — it is never used to derive `status` on its own (only
    as a lookup key against `_STATUS_OK_CONFIRMED_CODES`).
    """
    key = str(status_code)
    if key in _CONFIRMED_STATUS_CODES or key in _unmapped_status_codes_logged:
        return
    _unmapped_status_codes_logged.add(key)
    _LOGGER.warning(
        "DAO reported statusCode=%s, which has no known mapping yet. Open an "
        "issue and paste this line so the code table can be built: %s",
        status_code,
        NEW_ISSUE_URL,
    )


def _warn_unmapped_bound(bound: str) -> None:
    """Log an unrecognised DAO `bound` value once.

    Defensive: only IN/OUT are confirmed.
    """
    if bound in _unmapped_bounds_logged:
        return
    _unmapped_bounds_logged.add(bound)
    _LOGGER.warning(
        "Unrecognised DAO bound=%s — treated as incoming. Open an issue and "
        "paste this line: %s",
        bound,
        NEW_ISSUE_URL,
    )


def _warn_unmapped_tracking_code(code: Any) -> None:
    """Log an event code the public barcode route reports but we do not map.

    This route carries no ``statusType`` beside the code, so an unmapped one
    genuinely means "we do not know this parcel's state" — the Danish text
    next to it is deliberately not pattern-matched to invent one.
    """
    key = str(code)
    if key in _unmapped_tracking_codes_logged:
        return
    _unmapped_tracking_codes_logged.add(key)
    _LOGGER.warning(
        "DAO's public tracking lookup reported event code %s, which has no "
        "mapping yet — reported as 'unknown'. Open an issue and paste this "
        "line so the code table can be extended: %s",
        code,
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
    global _assumed_ok_split_warned  # noqa: PLW0603 - one-shot warning flag; tests reset it
    if _assumed_ok_split_warned:
        return
    _assumed_ok_split_warned = True
    resolved = (
        ParcelStatus.AT_PICKUP_POINT if pickup_point_id else ParcelStatus.DELIVERED
    )
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
    global _unverified_history_warned  # noqa: PLW0603 - one-shot warning flag; tests reset it
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
    try:
        override = _CONFIRMED_CODE_OVERRIDES.get(int(status_code))
    except (TypeError, ValueError):
        override = None
    if override is not None:
        return override
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
    """Map a DAO ``statusType``/``statusCode`` (+ pickup point) to a canonical status.

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


def map_tracking_code(code: Any) -> ParcelStatus:
    """Map the public route's ``haendelse`` event code to a canonical status.

    An unmapped code reports ``unknown`` with a one-shot warning. There is no
    second field to fall back on here, and the event's Danish description is
    deliberately not pattern-matched: a guessed status is worse than an
    honest ``unknown``.
    """
    try:
        resolved = TRACKING_CODE_MAP.get(int(code))
    except (TypeError, ValueError):
        resolved = None
    if resolved is not None:
        return resolved
    if code is not None and str(code) != "":
        _warn_unmapped_tracking_code(code)
    return ParcelStatus.UNKNOWN


def reset_one_shot_warnings() -> None:
    """Forget every "already warned" flag. For tests only.

    The flags are module-level by design (one warning per HA session, not per
    poll), which makes them shared state between tests — a test asserting
    that a warning fires would otherwise depend on which test ran first.
    """
    global _assumed_ok_split_warned, _unverified_history_warned  # noqa: PLW0603 - test hook
    _unmapped_statuses_logged.clear()
    _unmapped_status_codes_logged.clear()
    _unmapped_bounds_logged.clear()
    _unmapped_tracking_codes_logged.clear()
    _assumed_ok_split_warned = False
    _unverified_history_warned = False
