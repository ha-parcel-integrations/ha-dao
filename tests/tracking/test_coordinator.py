"""Tests for the tracking coordinator: per-code fetching, bans, full stop.

The payload mapping itself is covered by ``test_parcels.py``; the shared
polling arithmetic by ``tests/account/test_polling.py``.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dao.const import (
    BAN_BACKOFF_START_SECONDS,
    CONF_BARCODE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_SOURCE,
    DOMAIN,
    RATE_LIMIT_BACKOFF_START_SECONDS,
    SOURCE_TRACKING,
    ParcelStatus,
)
from custom_components.dao.tracking.api import (
    DAOTrackingApiError,
    DAOTrackingBannedError,
)
from custom_components.dao.tracking.coordinator import DAOTrackingCoordinator

from .payloads import CODE, SECOND_CODE, event, parcel


@pytest.fixture(autouse=True)
def _no_trickle_gap():
    """Drop the between-lookup gap, so the suite does not sleep through it."""
    with patch(
        "custom_components.dao.tracking.coordinator.TRACKING_FETCH_GAP_SECONDS", 0
    ):
        yield


def _entry(codes: list[str] | None = None, *, include_history: bool = False):
    return MockConfigEntry(
        domain=DOMAIN,
        title="DAO (tracking codes)",
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_PARCELS: [
                {CONF_BARCODE: code}
                for code in (codes if codes is not None else [CODE])
            ],
            # Keep-most-recent-100, so the retention filter never trims the
            # fixed-date sample parcels these tests assert on.
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            CONF_INCLUDE_HISTORY: include_history,
        },
    )


def _client(side_effect=None, return_value=None) -> AsyncMock:
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(
        side_effect=side_effect, return_value=return_value
    )
    return client


async def _poll(hass, client, entry) -> list[dict]:
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()
    return coordinator


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_every_tracked_code_is_looked_up(hass):
    client = _client(return_value=parcel())
    coordinator = await _poll(hass, client, _entry([CODE, SECOND_CODE]))
    assert client.async_get_parcel.await_count == 2
    assert len(coordinator.data) == 2


async def test_an_unknown_code_is_still_published(hass):
    coordinator = await _poll(hass, _client(return_value=None), _entry())
    assert len(coordinator.data) == 1
    assert coordinator.data[0]["status"] is ParcelStatus.UNKNOWN


async def test_a_delivered_parcel_moves_to_the_delivered_list(hass):
    delivered = parcel(events=[event(51, "Pakken er udleveret")])
    coordinator = await _poll(hass, _client(return_value=delivered), _entry())
    assert coordinator.data == []
    assert len(coordinator.delivered) == 1


async def test_a_delivered_code_is_never_fetched_again(hass):
    """Its payload cannot change, so the second poll must not ask."""
    client = _client(return_value=parcel(events=[event(51, "udleveret")]))
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()
    coordinator.data = await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1
    # Still published from the cache, not dropped.
    assert len(coordinator.delivered) == 1


async def test_the_outgoing_lists_stay_empty(hass):
    """The public route is recipient-side only."""
    coordinator = await _poll(hass, _client(return_value=parcel()), _entry())
    assert coordinator.outgoing == []
    assert coordinator.delivered_outgoing == []


async def test_a_removed_code_is_dropped_from_the_cache(hass):
    client = _client(return_value=parcel())
    entry = _entry([CODE, SECOND_CODE])
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PARCELS: [{CONF_BARCODE: CODE}]}
    )
    coordinator.data = await coordinator._async_update_data()
    assert set(coordinator._raw_cache) == {CODE}


# ---------------------------------------------------------------------------
# per-code failure containment
# ---------------------------------------------------------------------------


async def test_one_failing_code_does_not_fail_the_poll(hass):
    client = _client(
        side_effect=[DAOTrackingApiError("nope"), parcel(code=SECOND_CODE)]
    )
    coordinator = await _poll(hass, client, _entry([CODE, SECOND_CODE]))
    assert len(coordinator.data) == 2


async def test_a_network_error_on_one_code_keeps_the_previous_payload(hass):
    client = _client(side_effect=[parcel(), aiohttp.ClientError("reset")])
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()
    first = coordinator.data[0]["status"]
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.data[0]["status"] is first


# ---------------------------------------------------------------------------
# rate limiting and bans
# ---------------------------------------------------------------------------


async def test_a_429_on_the_very_first_poll_fails_the_refresh(hass):
    """With nothing cached there is nothing to show, so HA should retry."""
    client = _client(side_effect=DAOTrackingApiError("slow", status_code=429))
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == RATE_LIMIT_BACKOFF_START_SECONDS


async def test_a_429_after_a_good_poll_keeps_the_previous_parcels(hass):
    """DAO's limiter is short-windowed; a hiccup must not blank the sensors."""
    client = _client(
        side_effect=[
            parcel(events=[event(12, "Sorteret")]),
            DAOTrackingApiError("slow", status_code=429),
        ]
    )
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()
    first_seen = coordinator.last_success_time

    coordinator.data = await coordinator._async_update_data()

    assert len(coordinator.data) == 1
    assert coordinator.data[0]["status"] is ParcelStatus.IN_TRANSIT
    # Not counted as a successful update, and the next poll is pushed out.
    assert coordinator.last_success_time == first_seen
    assert coordinator.update_interval == timedelta(
        seconds=RATE_LIMIT_BACKOFF_START_SECONDS
    )


async def test_a_rate_limited_poll_stops_asking_for_the_other_codes(hass):
    """Hammering a short-window limiter is what keeps it closed."""
    client = _client(
        side_effect=[
            parcel(),
            parcel(code=SECOND_CODE),
            DAOTrackingApiError("slow", status_code=429),
        ]
    )
    entry = _entry([CODE, SECOND_CODE])
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()

    client.async_get_parcel.reset_mock()
    client.async_get_parcel.side_effect = [
        DAOTrackingApiError("slow", status_code=429),
        parcel(code=SECOND_CODE),
    ]
    coordinator.data = await coordinator._async_update_data()

    # Only the first code was tried; the second came from the cache.
    assert client.async_get_parcel.await_count == 1
    assert len(coordinator.data) == 2


async def test_the_rate_limit_backoff_doubles_and_then_resets(hass):
    client = _client(side_effect=[parcel()])
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()

    client.async_get_parcel.side_effect = DAOTrackingApiError("slow", status_code=429)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(
        seconds=RATE_LIMIT_BACKOFF_START_SECONDS
    )
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(
        seconds=RATE_LIMIT_BACKOFF_START_SECONDS * 2
    )

    client.async_get_parcel.side_effect = None
    client.async_get_parcel.return_value = parcel()
    coordinator.data = await coordinator._async_update_data()
    assert coordinator._consecutive_429 == 0
    assert coordinator.update_interval != timedelta(
        seconds=RATE_LIMIT_BACKOFF_START_SECONDS
    )


async def test_a_ban_backs_off_far_harder_than_a_rate_limit(hass):
    client = _client(side_effect=DAOTrackingBannedError("banned"))
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == BAN_BACKOFF_START_SECONDS
    with pytest.raises(UpdateFailed) as second:
        await coordinator._async_update_data()
    assert second.value.retry_after == BAN_BACKOFF_START_SECONDS * 2


async def test_a_ban_is_not_swallowed_per_code(hass):
    """A ban hits every code, so it must fail the refresh, not one parcel."""
    client = _client(side_effect=[DAOTrackingBannedError("banned"), parcel()])
    entry = _entry([CODE, SECOND_CODE])
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_a_successful_poll_clears_the_backoff_counters(hass):
    client = _client(side_effect=[DAOTrackingBannedError("banned"), parcel()])
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert coordinator._consecutive_bans == 0
    assert coordinator._consecutive_429 == 0


# ---------------------------------------------------------------------------
# the account-less polling model
# ---------------------------------------------------------------------------


async def test_polling_stops_entirely_with_nothing_tracked(hass):
    entry = _entry([])
    coordinator = await _poll(hass, _client(return_value=None), entry)
    assert coordinator.update_interval is None
    assert coordinator.current_tier_minutes is None


async def test_polling_stops_once_everything_is_delivered(hass):
    client = _client(return_value=parcel(events=[event(51, "udleveret")]))
    coordinator = await _poll(hass, client, _entry())
    assert coordinator.update_interval is None


async def test_polling_resumes_while_a_parcel_is_in_flight(hass):
    client = _client(return_value=parcel(events=[event(12, "Sorteret")]))
    coordinator = await _poll(hass, client, _entry())
    assert coordinator.update_interval is not None
    assert coordinator.update_interval > timedelta(0)


async def test_a_failed_poll_still_reschedules(hass):
    client = _client(side_effect=TimeoutError)
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.update_interval = timedelta(hours=5, minutes=40)
    with pytest.raises(TimeoutError):
        await coordinator._async_update_data()
    assert coordinator.update_interval is not None
    assert coordinator.update_interval < timedelta(hours=5)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_the_first_refresh_fires_nothing(hass):
    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", fired.append)
    await _poll(hass, _client(return_value=parcel()), _entry())
    await hass.async_block_till_done()
    assert fired == []


async def test_a_status_change_fires_an_event(hass):
    client = _client(
        side_effect=[
            parcel(events=[event(1, "Oprettet")]),
            parcel(events=[event(12, "Sorteret")]),
        ]
    )
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", fired.append)
    coordinator.data = await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(fired) == 1
    assert fired[0].data["new_status"] == ParcelStatus.IN_TRANSIT


async def test_delivery_fires_only_the_delivered_event(hass):
    client = _client(
        side_effect=[
            parcel(events=[event(32, "Klar til afhentning")]),
            parcel(events=[event(51, "Pakken er udleveret")]),
        ]
    )
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()

    delivered, changed = [], []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", delivered.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", changed.append)
    coordinator.data = await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(delivered) == 1
    assert changed == []


async def test_no_outgoing_event_is_ever_fired(hass):
    client = _client(
        side_effect=[
            parcel(events=[event(1, "Oprettet")]),
            parcel(events=[event(51, "udleveret")]),
        ]
    )
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = DAOTrackingCoordinator(hass, client, entry)
    coordinator.data = await coordinator._async_update_data()

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_outgoing_parcel_delivered", fired.append)
    hass.bus.async_listen(f"{DOMAIN}_outgoing_parcel_status_changed", fired.append)
    coordinator.data = await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert fired == []


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


async def test_history_follows_the_opt_in_option(hass):
    off = await _poll(hass, _client(return_value=parcel()), _entry())
    assert off.data[0]["history"] is None

    on = await _poll(hass, _client(return_value=parcel()), _entry(include_history=True))
    assert on.data[0]["history"]


async def test_last_success_time_is_recorded(hass):
    coordinator = await _poll(hass, _client(return_value=parcel()), _entry())
    assert coordinator.last_success_time is not None
