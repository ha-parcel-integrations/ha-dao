"""Services for the DAO parcel tracker integration.

``dao.track_parcel`` / ``dao.untrack_parcel`` add or remove a tracked code
without opening the integration options, so a dashboard button or an
automation can start tracking a parcel on its own (ha-dao#16).

They belong to the **keyless tracking source** only. The account source
discovers parcels from DAO's own inbox and has no "add a code" concept, so an
account-only install registers no services at all — and that is deliberate
feedback: if ``dao.track_parcel`` is missing from Developer tools, the
tracking source is not set up.

Validation is format-only (a non-empty code), never a live lookup: a service
call must not block on a network round-trip. A code is only confirmed real on
the next poll.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    CONF_BARCODE,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .tracking.parcels import parcel_key

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})


def normalize_code(value: str) -> str:
    """Return the tracking code trimmed, nothing else.

    DAO publishes no barcode format or checksum, so this deliberately does
    not upper-case or strip separators — a guessed transform could turn a
    working code into one the endpoint rejects.
    """
    return (value or "").strip()


def _tracking_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Return the configured tracking-source entries."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_TRACKING
    ]


def _resolve_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the single tracking entry, or explain why there is none.

    The tracking source is a singleton (``config_flow`` aborts a second one),
    so there is never a "which one" question to answer here.
    """
    entries = _tracking_entries(hass)
    if not entries:
        raise ServiceValidationError(
            "DAO's tracking-code source is not set up — add DAO again and "
            "choose 'Tracking codes'. A DAO account entry cannot take "
            "tracking codes, it lists whatever the account itself knows."
        )
    return entries[0]


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the DAO services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A DAO tracking code is required")
        entry = _resolve_entry(hass)

        parcels = [dict(item) for item in entry.options.get(CONF_PARCELS, [])]
        new_key = parcel_key(code)
        if any(parcel_key(item[CONF_BARCODE]) == new_key for item in parcels):
            return  # already tracked — no-op
        parcels.append({CONF_BARCODE: code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        target = parcel_key(code)
        entry = _resolve_entry(hass)
        current = entry.options.get(CONF_PARCELS, [])
        kept = [item for item in current if parcel_key(item[CONF_BARCODE]) != target]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    hass.services.async_register(DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the DAO services."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
