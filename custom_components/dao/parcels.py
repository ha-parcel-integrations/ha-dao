"""Compatibility module for the account source's normaliser.

New code imports from :mod:`dao.account.parcels`; this module preserves the
pre-split import path, which custom automations and the test suite still use.
The names are re-exported explicitly rather than by copying the module
namespace, so a type checker can follow them and so "what the pre-split
contract was" stays answerable. The shared status vocabulary itself lives in
:mod:`dao.status`, which both sources read.
"""

from .account.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parcel_direction,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracking_url,
)

# The private one-shot warning flags the test suite resets. They are the same
# objects as ``dao.status``'s, not copies: the sets are mutated in place, so
# clearing one clears it everywhere.
from .status import (  # noqa: F401 - re-exported for the pre-split import path
    _unmapped_bounds_logged,
    _unmapped_status_codes_logged,
    _unmapped_statuses_logged,
    _warn_assumed_ok_split,
    _warn_unmapped_bound,
    _warn_unmapped_status,
    _warn_unmapped_status_code,
    _warn_unverified_history,
)

__all__ = [
    "apply_delivered_filter",
    "build_history",
    "map_event_status",
    "map_parcel_status",
    "normalize_parcel",
    "parcel_direction",
    "parse_iso",
    "sort_parcels_by_ts",
    "to_iso_timestamp",
    "tracking_url",
]
