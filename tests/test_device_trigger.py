"""Tests for DAO device triggers."""

from unittest.mock import AsyncMock

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE

from custom_components.dao.const import DOMAIN
from custom_components.dao.device_trigger import (
    TRIGGER_EVENTS,
    async_attach_trigger,
    async_get_triggers,
)


async def test_get_triggers_returns_all_six(hass):
    triggers = await async_get_triggers(hass, "device123")
    types = {t["type"] for t in triggers}
    assert types == {
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    }
    for trigger in triggers:
        assert trigger["domain"] == DOMAIN
        assert trigger["device_id"] == "device123"


def test_trigger_events_map_to_domain_prefix():
    assert TRIGGER_EVENTS["parcel_registered"] == f"{DOMAIN}_parcel_registered"


async def test_attaching_a_trigger_listens_for_that_event(hass):
    """The device trigger is the event trigger, scoped to this device."""
    action = AsyncMock()
    remove = await async_attach_trigger(
        hass,
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: "device-1",
            CONF_TYPE: "parcel_delivered",
        },
        action,
        {
            "name": "test",
            "domain": DOMAIN,
            "home_assistant_start": False,
            "variables": {},
            "trigger_data": {},
        },
    )

    hass.bus.async_fire(f"{DOMAIN}_parcel_delivered", {"device_id": "device-1"})
    await hass.async_block_till_done()
    assert action.await_count == 1

    # A different device must not trip it.
    hass.bus.async_fire(f"{DOMAIN}_parcel_delivered", {"device_id": "device-2"})
    await hass.async_block_till_done()
    assert action.await_count == 1

    remove()
