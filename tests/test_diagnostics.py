"""Tests for DAO diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.dao.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"delivered_filter_type": "days", "delivered_filter_amount": 7}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "EXAMPLE123456",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "out_for_delivery",
            "raw": {
                "trackingId": "EXAMPLE123456",
                "sender": {"name": "Example Shop"},
                "receiver": {"name": "Jane Doe"},
                "delivery": {"handoverCode": "1234", "pickupPointId": "shop-9"},
                "labellessCode": "ABCD",
                # `receiver.street`/etc. are already covered by the parent
                # "receiver" key being redacted wholesale — this checks the
                # field names are redacted in their own right too, in case a
                # future payload shape ever surfaces them outside "receiver".
                "street": "Testvej",
                "houseNumber": "1",
                "zipCode": "1000",
                "city": "Copenhagen",
                "pickupPoint": {
                    "coordinates": {"latitude": 55.67, "longitude": 12.56},
                },
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = []
    entry.runtime_data.coordinator.delivered_outgoing = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "outgoing_active": 0,
        "outgoing_delivered": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # non-identifying options survive; identifying payload fields don't, at
    # every nesting level
    assert result["entry_options"] == entry.options
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["trackingId"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["sender"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["labellessCode"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["delivery"]["handoverCode"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["delivery"]["pickupPointId"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["street"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["houseNumber"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["zipCode"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["city"] == "**REDACTED**"
    assert (
        result["incoming"][0]["raw"]["pickupPoint"]["coordinates"] == "**REDACTED**"
    )
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"
    assert result["outgoing"] == []
    assert result["outgoing_delivered"] == []


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = []
    entry.runtime_data.coordinator.delivered_outgoing = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
