"""Diagnostics support for the DAO parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import DAOConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
TO_REDACT = {
    # canonical fields we publish ourselves
    "barcode",
    "sender",
    "receiver",
    "url",
    # DAO payload fields
    "trackingId",
    "handoverCode",
    "labellessCode",
    "pickupPointId",
    # receiver carries a full postal address alongside its name — the name
    # is already covered by the canonical "receiver" key above, this covers
    # the wire-shape fields underneath it
    "street",
    "houseNumber",
    "zipCode",
    "city",
    "coordinates",
    "latitude",
    "longitude",
    # tokens and the account identity used for the unique id
    "access_token",
    "refresh_token",
    "id_token",
    "account_subject",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DAOConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the DAO config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
