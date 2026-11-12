"""The suite's dynamic, status-driven polling algorithm, shared by both sources.

One copy, one level up from either source package, so the account route and
the public barcode route cannot drift apart on the quiet window, the anchors
or the stagger. The two tiering functions are the algorithm's two documented
models, not two carriers' opinions:

* :func:`account_tier_minutes` never suspends — one account call returns the
  whole inbox, so the mid-tier poll is also the only way a *new* shipment is
  discovered.
* :func:`tracked_tier_minutes` returns ``None`` (stop entirely) when nothing
  is tracked or every tracked code is delivered, because a barcode-based
  source has nothing to ask about until the user adds a code.

Full algorithm and reasoning: ``ha-carrier-template/scaffold/CLAUDE.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from homeassistant.util import dt as dt_util

from .const import (
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)


def stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    return (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )


def _is_hot(active_parcels: list[dict], now: datetime) -> bool:
    """Whether any active parcel is out for delivery within the lookahead."""
    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return True
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return True
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return True
    return False


def account_tier_minutes(active_parcels: list[dict], now: datetime) -> int:
    """Tier for the account-based model. Never ``None``: it never fully stops."""
    if _is_hot(active_parcels, now):
        return HOT_INTERVAL_MINUTES
    return MID_INTERVAL_MINUTES


def tracked_tier_minutes(active_parcels: list[dict], now: datetime) -> int | None:
    """Tier for the barcode-based model.

    ``None`` means "stop polling entirely" — nothing is tracked, or every
    tracked parcel is already delivered (already filtered out of
    ``active_parcels`` by the caller).
    """
    if not active_parcels:
        return None
    return account_tier_minutes(active_parcels, now)


def next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Turn a tier into the coordinator's next ``update_interval``.

    ``None`` fully suspends scheduling (``DataUpdateCoordinator`` honours
    this natively). Otherwise, clamp the naive next-due time forward to the
    next anchor whenever it would land inside the quiet window — including
    when ``now`` itself is already inside it (an anchor poll computing its
    own follow-up).
    """
    if tier_minutes is None:
        return None

    if in_quiet_window(now):
        return next_anchor(now) - now

    stagger = timedelta(minutes=stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if in_quiet_window(candidate):
        return next_anchor(now) - now
    return candidate - now
