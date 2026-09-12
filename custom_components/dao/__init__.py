"""DAO parcel tracker custom component for Home Assistant."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DAOApiClient,
    DAOApiError,
    DAOAuthError,
    resolve_lang,
)
from .const import CONF_REFRESH_TOKEN, PLATFORMS
from .coordinator import DAOCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class DAOData:
    """Runtime data attached to a DAO config entry."""

    client: DAOApiClient
    coordinator: DAOCoordinator
    session: aiohttp.ClientSession


type DAOConfigEntry = ConfigEntry[DAOData]


async def async_setup_entry(hass: HomeAssistant, entry: DAOConfigEntry) -> bool:
    """Set up DAO from a config entry."""
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

    # No entry.add_update_listener: the options flow calls
    # async_schedule_reload itself. Combining an update listener with a
    # reload-on-update flow is deprecated and becomes an error in HA 2026.12+.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DAOConfigEntry) -> bool:
    """Unload a DAO config entry."""
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.session.close()
        return True
    return False
