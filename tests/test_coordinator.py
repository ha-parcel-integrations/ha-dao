"""Tests for the DAO coordinator: fetching and events.

The parcel mapping itself is covered by ``test_parcels.py``.
"""
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dao.api import DAOApiError, DAOAuthError
from custom_components.dao.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ParcelStatus,
)
from custom_components.dao.coordinator import DAOCoordinator

from .payloads import (
    ACTIVE_CODE,
    PICKUP_POINT_ID,
    SECOND_CODE,
    delivered_sample,
    in_transit_sample,
    parcel,
    pickup_point_response,
    returning_sample,
    status_event,
)

SUBJECT = "subject-1"


def _entry(*, include_history: bool = False) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DAO",
        unique_id=SUBJECT,
        data={CONF_REFRESH_TOKEN: "rt-0", CONF_ACCOUNT_SUBJECT: SUBJECT},
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_INCLUDE_HISTORY: include_history,
        },
    )


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_splits_active_and_delivered(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [in_transit_sample(), delivered_sample(SECOND_CODE)]
    coordinator = DAOCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert [parcel["barcode"] for parcel in data] == [ACTIVE_CODE]
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_handles_an_empty_account(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = []
    coordinator = DAOCoordinator(hass, client, entry)

    assert await coordinator._async_update_data() == []


async def test_expired_session_triggers_reauth(hass):
    """An expired session must start reauth, not retry forever."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.side_effect = DAOAuthError("HTTP 401")
    coordinator = DAOCoordinator(hass, client, entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    """Otherwise every restart floods the user with "registered" events."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [in_transit_sample()]
    coordinator = DAOCoordinator(hass, client, entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = AsyncMock()
    coordinator = DAOCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [in_transit_sample()]
    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [returning_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_fires_status_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = DAOCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [in_transit_sample()]
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_parcels.return_value = [returning_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.RETURNING


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    """The hop to delivered fires exactly one, dedicated event."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = DAOCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcels.return_value = [in_transit_sample()]
    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [delivered_sample(ACTIVE_CODE)]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when it first appears fires nothing."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = DAOCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    client.async_get_parcels.return_value = [in_transit_sample()]
    await coordinator._async_update_data()  # first refresh seeds the state
    client.async_get_parcels.return_value = [in_transit_sample(), delivered_sample(SECOND_CODE)]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = DAOCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    client.async_get_parcels.return_value = [in_transit_sample()]
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_parcels.return_value = [
        in_transit_sample(),
        in_transit_sample(SECOND_CODE),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == SECOND_CODE


async def test_delivery_time_changed_never_fires(hass):
    """`planned_from`/`planned_to` stay `None` for DAO (see parcels.py) —
    the change-detection machinery in the coordinator is suite-wide and
    still runs, it just never has anything to report for this carrier."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = DAOCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcels.return_value = [in_transit_sample()]
    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_parcels.return_value = [delivered_sample()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


# ---------------------------------------------------------------------------
# enrichment — detail call (history) + pickup-point lookup
# ---------------------------------------------------------------------------


def _pickup_raw(tracking_id: str = ACTIVE_CODE) -> dict:
    """A pickup-point parcel with only the id — the resolved object is what
    the coordinator is responsible for merging in, not the sample builder."""
    return parcel(
        "STATUS_OK",
        tracking_id=tracking_id,
        status_code=32,
        pickup_point_id=PICKUP_POINT_ID,
    )


async def test_detail_call_populates_history(hass):
    entry = _entry(include_history=True)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [in_transit_sample()]
    events = [status_event("STATUS_PROCESSING", "2026-04-24T09:00:00Z", 1)]
    client.async_get_parcel_detail.return_value = {"events": events}
    coordinator = DAOCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    client.async_get_parcel_detail.assert_awaited_once_with(ACTIVE_CODE)
    assert data[0]["history"] == [
        {
            "timestamp": "2026-04-24T09:00:00Z",
            "status": ParcelStatus.REGISTERED,
            "raw_status": 1,
        }
    ]


async def test_detail_call_skipped_when_history_option_is_off(hass):
    entry = _entry(include_history=False)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [in_transit_sample()]
    coordinator = DAOCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    client.async_get_parcel_detail.assert_not_awaited()
    assert data[0]["history"] is None


async def test_pickup_point_lookup_resolves_the_name(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [_pickup_raw()]
    client.async_get_pickup_point.return_value = pickup_point_response()
    coordinator = DAOCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    client.async_get_pickup_point.assert_awaited_once_with(PICKUP_POINT_ID)
    assert data[0]["pickup_point"] == "Example Point Central Station"


async def test_history_cache_skips_refetch_when_status_code_unchanged(hass):
    """History only grows on a status change — an unchanged raw statusCode
    across polls (a terminal, delivered parcel included) must not refetch."""
    entry = _entry(include_history=True)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [in_transit_sample()]
    client.async_get_parcel_detail.return_value = {
        "events": [status_event("STATUS_IN_TRANSIT", "2026-04-26T09:00:00Z", 48)]
    }
    coordinator = DAOCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    client.async_get_parcel_detail.assert_awaited_once()


async def test_history_cache_refetches_after_status_code_changes(hass):
    entry = _entry(include_history=True)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel_detail.return_value = {"events": []}
    coordinator = DAOCoordinator(hass, client, entry)

    client.async_get_parcels.return_value = [in_transit_sample()]
    await coordinator._async_update_data()
    client.async_get_parcels.return_value = [delivered_sample()]
    await coordinator._async_update_data()

    assert client.async_get_parcel_detail.await_count == 2


async def test_detail_call_failure_degrades_gracefully(hass):
    """One parcel's detail call failing must not fail the whole poll."""
    entry = _entry(include_history=True)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [in_transit_sample()]
    client.async_get_parcel_detail.side_effect = DAOApiError("HTTP 503", status_code=503)
    coordinator = DAOCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data[0]["barcode"] == ACTIVE_CODE
    assert data[0]["history"] == []


async def test_detail_call_network_failure_degrades_gracefully(hass):
    entry = _entry(include_history=True)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [in_transit_sample()]
    client.async_get_parcel_detail.side_effect = aiohttp.ClientError("boom")
    coordinator = DAOCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data[0]["history"] == []


async def test_pickup_point_call_failure_degrades_gracefully(hass):
    """One parcel's pickup-point lookup failing must not fail the whole poll."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [_pickup_raw()]
    client.async_get_pickup_point.side_effect = DAOApiError("HTTP 503", status_code=503)
    coordinator = DAOCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data[0]["barcode"] == ACTIVE_CODE
    assert data[0]["pickup_point"] is None


async def test_pickup_point_call_network_failure_degrades_gracefully(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [_pickup_raw()]
    client.async_get_pickup_point.side_effect = aiohttp.ClientError("boom")
    coordinator = DAOCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data[0]["pickup_point"] is None


async def test_detail_call_auth_error_propagates_as_reauth(hass):
    """A dead session fails identically for every parcel — surface it as the
    same reauth the main list call would trigger, instead of swallowing it
    as one parcel's missing history."""
    entry = _entry(include_history=True)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [in_transit_sample()]
    client.async_get_parcel_detail.side_effect = DAOAuthError("session dead")
    coordinator = DAOCoordinator(hass, client, entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_pickup_point_auth_error_propagates_as_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcels.return_value = [_pickup_raw()]
    client.async_get_pickup_point.side_effect = DAOAuthError("session dead")
    coordinator = DAOCoordinator(hass, client, entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
