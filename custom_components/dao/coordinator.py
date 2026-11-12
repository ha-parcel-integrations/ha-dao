"""Compatibility import for the account source's coordinator.

New code imports from :mod:`dao.account.coordinator`. The module's private
polling helpers are re-exported too: they were importable from here before
the source split and the test suite still reads them from this path.
"""

from .account.coordinator import (  # noqa: F401 - re-exported for the pre-split path
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    ENRICH_CONCURRENCY,
    DAOCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _stagger_minutes,
)
from .tracking.coordinator import DAOTrackingCoordinator

# What the shared platforms annotate against: an entry runs one source or the
# other, and sensor/calendar/button read the same attributes from either.
DAOAnyCoordinator = DAOCoordinator | DAOTrackingCoordinator

__all__ = ["DAOAnyCoordinator", "DAOCoordinator", "DAOTrackingCoordinator"]
