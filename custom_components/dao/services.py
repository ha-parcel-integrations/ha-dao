"""Services for the DAO parcel tracker integration.

``dao.track_parcel`` / ``dao.untrack_parcel`` add or remove a tracked code
without opening the integration options, so a dashboard button or an
automation can start tracking a parcel on its own (ha-dao#16).

They belong to the **keyless tracking source** only. The account source
discovers parcels from DAO's own inbox and has no "add a code" concept, so an
account-only install does not register them — and that is deliberate
feedback: if ``dao.track_parcel`` is missing from Developer tools, the
tracking source is not set up.

``dao.update_contents`` sets or clears the text describing what is in a
parcel, tracked or not, on every entry that lists the code, and is registered
for account-only setups too.

Validation is format-only (a non-empty code), never a live lookup: a service
call must not block on a network round-trip. A code is only confirmed real on
the next poll.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    CONF_BARCODE,
    CONF_CONFIG_ENTRY_ID,
    CONF_CONTENTS,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
    entry_source,
)
from .contents import ContentsStore, normalize_code as normalize_contents_code
from .tracking.parcels import parcel_key

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

SERVICE_UPDATE_CONTENTS = "update_contents"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)


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
        if entry_source(entry) == SOURCE_TRACKING
    ]


def _resolve_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the single tracking entry, or explain why there is none.

    The tracking source is a singleton (``config_flow`` aborts a second one),
    so there is never a "which one" question to answer here.
    """
    entries = _tracking_entries(hass)
    if not entries:
        raise ServiceValidationError("DAO's tracking codes are not set up")
    return entries[0]


def _loaded_coordinator(entry: ConfigEntry):
    """Return the coordinator of a set-up entry, or raise when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("DAO is not loaded")
    return entry.runtime_data.coordinator


def _contents_store(entry: ConfigEntry) -> ContentsStore | None:
    """Return the entry's contents store, or ``None`` when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data.coordinator.contents_store


def async_setup_services(hass: HomeAssistant, *, hub: bool = True) -> None:
    """Register the DAO services (idempotent).

    ``update_contents`` serves every entry; ``track_parcel`` and
    ``untrack_parcel`` belong to the tracking source, so an account entry
    passes ``hub=False``.
    """

    async def _track(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A DAO tracking code is required")
        entry = _resolve_entry(hass)

        has_contents = CONF_CONTENTS in call.data
        coordinator = _loaded_coordinator(entry) if has_contents else None

        parcels = [dict(item) for item in entry.options.get(CONF_PARCELS, [])]
        new_key = parcel_key(code)
        if any(parcel_key(item[CONF_BARCODE]) == new_key for item in parcels):
            # Already tracked: only a changed contents value does anything.
            if coordinator is not None and coordinator.contents_store.async_set(
                code, call.data[CONF_CONTENTS]
            ):
                await coordinator.async_request_refresh()
            return
        if coordinator is not None:
            # Stored before the options write, whose listener refreshes.
            coordinator.contents_store.async_set(code, call.data[CONF_CONTENTS])
        parcels.append({CONF_BARCODE: code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        target = parcel_key(code)
        entry = _resolve_entry(hass)
        if (store := _contents_store(entry)) is not None:
            store.async_set(code, None)
        current = entry.options.get(CONF_PARCELS, [])
        kept = [item for item in current if parcel_key(item[CONF_BARCODE]) != target]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    async def _update_contents(call: ServiceCall) -> None:
        code = normalize_contents_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A DAO tracking code is required")
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("DAO is not set up")
        if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(f"'{entry_id}' is not a loaded DAO entry")
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two entries, so every entry that lists it gets
            # the value; guessing an entry would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if code in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(code, call.data.get(CONF_CONTENTS)):
                await coordinator.async_request_refresh()

    if hub and not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        hass.services.async_register(
            DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
        )
        hass.services.async_register(
            DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_CONTENTS,
            _update_contents,
            schema=_UPDATE_CONTENTS_SCHEMA,
        )


def async_unload_services(
    hass: HomeAssistant, *, hubs_left: bool, entries_left: bool
) -> None:
    """Remove the DAO services their entries no longer need.

    The tracking services go with the last tracking entry; ``update_contents``
    stays while any entry, tracking or account, is still loaded.
    """
    services = []
    if not hubs_left:
        services += [SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL]
    if not entries_left:
        services.append(SERVICE_UPDATE_CONTENTS)
    for service in services:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
