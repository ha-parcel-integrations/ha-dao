"""The pre-split import paths must keep resolving.

``api.py``, ``coordinator.py`` and ``parcels.py`` became re-export shims when
the account source moved into its own package. Automations, custom cards and
anything else that imported them by their old path must not break, so the
shims are tested rather than assumed.
"""

from custom_components.dao import api, coordinator, parcels, status
from custom_components.dao.account import parcels as account_parcels
from custom_components.dao.account.client import DAOApiClient
from custom_components.dao.account.coordinator import DAOCoordinator
from custom_components.dao.tracking.coordinator import DAOTrackingCoordinator
from custom_components.dao.tracking.parcels import map_tracking_code


def test_the_api_shim_exposes_the_account_client():
    assert api.DAOApiClient is DAOApiClient
    assert issubclass(api.DAOAuthError, api.DAOApiError)


def test_the_coordinator_shim_exposes_both_coordinators():
    assert coordinator.DAOCoordinator is DAOCoordinator
    assert coordinator.DAOTrackingCoordinator is DAOTrackingCoordinator


def test_the_coordinator_shim_still_exposes_the_polling_helpers():
    """``tests/account/test_polling.py`` and older code read them from here."""
    for name in (
        "_hottest_tier_minutes",
        "_in_quiet_window",
        "_next_anchor",
        "_next_update_interval",
        "_stagger_minutes",
    ):
        assert callable(getattr(coordinator, name))


def test_the_parcels_shim_mirrors_the_account_normaliser():
    assert parcels.normalize_parcel is account_parcels.normalize_parcel
    assert parcels.map_parcel_status is account_parcels.map_parcel_status
    # Including the private one-shot flags the test suite resets.
    assert (
        parcels._unmapped_statuses_logged is account_parcels._unmapped_statuses_logged
    )


def test_the_shared_status_vocabulary_is_one_object():
    """Both sources must read the same tables — not copies of them."""
    assert parcels.map_parcel_status is status.map_parcel_status
    assert map_tracking_code is status.map_tracking_code
