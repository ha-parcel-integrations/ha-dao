"""Tests for the HA-bus contract shared by both sources.

The firing rules themselves are exercised through each coordinator; this
module covers the shared edges neither coordinator can easily reach — a
parcel with no barcode, an ETA that only changes on one bound, and an
outgoing parcel seen for the first time.
"""

from custom_components.dao.const import DOMAIN, ParcelStatus
from custom_components.dao.events import (
    fire_incoming_change_events,
    fire_outgoing_change_events,
    snapshot_delivery_times,
    snapshot_states,
)


def _parcel(**overrides) -> dict:
    parcel = {
        "barcode": "A",
        "status": ParcelStatus.IN_TRANSIT,
        "planned_from": None,
        "planned_to": None,
    }
    parcel.update(overrides)
    return parcel


def _listen(hass, *event_types) -> list:
    fired: list = []
    for event_type in event_types:
        hass.bus.async_listen(f"{DOMAIN}_{event_type}", fired.append)
    return fired


async def test_a_parcel_without_a_barcode_is_skipped(hass):
    """Nothing can be diffed without a key, and it must not raise."""
    fired = _listen(hass, "parcel_registered", "parcel_status_changed")
    fire_incoming_change_events(hass, [_parcel(barcode=None)], {}, {}, None)
    await hass.async_block_till_done()
    assert fired == []


async def test_an_eta_appearing_on_one_bound_fires_once(hass):
    """A planned_to that arrives on its own is still a changed window."""
    fired = _listen(hass, "parcel_delivery_time_changed")
    fire_incoming_change_events(
        hass,
        [_parcel(planned_to="2026-11-12T10:00:00+00:00")],
        {"A": ParcelStatus.IN_TRANSIT},
        {"A": (None, None)},
        "device-1",
    )
    await hass.async_block_till_done()
    assert len(fired) == 1
    assert fired[0].data["new_planned_to"] == "2026-11-12T10:00:00+00:00"
    assert fired[0].data["old_planned_to"] is None


async def test_an_eta_going_away_is_silent(hass):
    """The carrier losing a window is not worth waking someone up for."""
    fired = _listen(hass, "parcel_delivery_time_changed")
    fire_incoming_change_events(
        hass,
        [_parcel(planned_from=None)],
        {"A": ParcelStatus.IN_TRANSIT},
        {"A": ("2026-11-12T10:00:00+00:00", None)},
        "device-1",
    )
    await hass.async_block_till_done()
    assert fired == []


async def test_an_unseen_outgoing_parcel_fires_nothing(hass):
    """A parcel you sent is not news the first time it appears."""
    fired = _listen(hass, "outgoing_parcel_status_changed", "outgoing_parcel_delivered")
    fire_outgoing_change_events(hass, [_parcel()], {}, "device-1")
    await hass.async_block_till_done()
    assert fired == []


async def test_an_outgoing_parcel_without_a_barcode_is_skipped(hass):
    fired = _listen(hass, "outgoing_parcel_status_changed")
    fire_outgoing_change_events(
        hass, [_parcel(barcode=None)], {"A": ParcelStatus.REGISTERED}, None
    )
    await hass.async_block_till_done()
    assert fired == []


def test_the_snapshots_skip_parcels_without_a_barcode():
    parcels = [_parcel(), _parcel(barcode=None)]
    assert snapshot_states(parcels) == {"A": ParcelStatus.IN_TRANSIT}
    assert snapshot_delivery_times(parcels) == {"A": (None, None)}
