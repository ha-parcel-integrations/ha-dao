"""DAO parcel tracker custom component for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import DAOApiClient, DAOApiError, DAOAuthError, resolve_lang
from .account.coordinator import DAOCoordinator
from .const import (
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .services import async_setup_services, async_unload_services
from .tracking.api import DAOTrackingApiClient
from .tracking.coordinator import DAOTrackingCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class DAOData:
    """Runtime data attached to a DAO config entry."""

    client: DAOApiClient | DAOTrackingApiClient
    coordinator: DAOCoordinator | DAOTrackingCoordinator
    session: aiohttp.ClientSession | None


type DAOConfigEntry = ConfigEntry[DAOData]


def entry_source(entry: ConfigEntry) -> str:
    """Return which backend an entry talks to.

    An entry created before the keyless source existed carries no
    ``CONF_SOURCE``, and is an account entry — this default *is* the
    migration, so every read site must go through here.
    """
    return entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT)


async def _async_setup_tracking(hass: HomeAssistant, entry: DAOConfigEntry) -> bool:
    """Set up the keyless barcode source: no login, no token, no session of its own."""
    client = DAOTrackingApiClient(
        async_get_clientsession(hass),
        language=resolve_lang(hass.config.language),
    )
    coordinator = DAOTrackingCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to
    # catch cleanly (it logs a warning and half-sets-up the entry); doing the
    # first refresh here lets a transient failure fail the whole entry so HA
    # retries it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = DAOData(client=client, coordinator=coordinator, session=None)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed codes, history) live via a
    # coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. This is also the resume path after polling
    # suspended entirely: adding a code back triggers this refresh, which
    # recomputes the tier and re-arms scheduling. Only the tracking source
    # gets a listener; the account source's options flow is an
    # OptionsFlowWithReload, which may not be combined with one.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_setup_services(hass)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: DAOConfigEntry) -> None:
    """Apply changed options by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def _async_setup_account(hass: HomeAssistant, entry: DAOConfigEntry) -> bool:
    """Set up the signed-in account source."""
    # Each config entry needs its own cookie jar, or two accounts overwrite
    # each other's auth cookies in the shared session. The connector is reused
    # (connector_owner=False) so this stays cheap.
    session = aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
    )
    client = DAOApiClient(
        session,
        entry.data[CONF_REFRESH_TOKEN],
        language=resolve_lang(hass.config.language),
    )

    try:
        await client.async_refresh()
    except DAOAuthError as err:
        # Credentials rejected — start reauth instead of retrying a password
        # that will never work again.
        await session.close()
        raise ConfigEntryAuthFailed("DAO authentication failed") from err
    except (DAOApiError, aiohttp.ClientError) as err:
        # Non-auth failure (typically a 5xx outage) — retry with backoff.
        await session.close()
        raise ConfigEntryNotReady("DAO login failed") from err

    coordinator = DAOCoordinator(hass, client, entry)

    try:
        # Fetch initial data here, before forwarding to platforms. Raising
        # ConfigEntryNotReady from a forwarded platform is too late for HA to
        # catch cleanly (it logs a warning and half-sets-up the entry); doing
        # the first refresh here lets a transient failure fail the whole entry
        # so HA retries it with backoff.
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Without this, every setup retry leaks a session.
        await session.close()
        raise

    entry.runtime_data = DAOData(
        client=client, coordinator=coordinator, session=session
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await session.close()
        raise

    # No entry.add_update_listener: the options flow is an
    # OptionsFlowWithReload, which may not be combined with one.
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DAOConfigEntry) -> bool:
    """Set up DAO from a config entry, on whichever source it names."""
    if entry_source(entry) == SOURCE_TRACKING:
        return await _async_setup_tracking(hass, entry)
    return await _async_setup_account(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: DAOConfigEntry) -> bool:
    """Unload a DAO config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    session = entry.runtime_data.session
    if session is not None:
        await session.close()
    if entry_source(entry) == SOURCE_TRACKING and not any(
        other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
        and entry_source(other) == SOURCE_TRACKING
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        # The services belong to the tracking source; remove them only once
        # the last tracking entry is gone, never when an account entry
        # unloads beside it.
        async_unload_services(hass)
    return True
