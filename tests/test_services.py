"""Tests for ``dao.track_parcel`` / ``dao.untrack_parcel``.

The services belong to the keyless tracking source: an account entry has no
tracked-code list to add to.
"""

from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dao.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_BARCODE,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.dao.services import (
    SERVICE_TRACK_PARCEL,
    SERVICE_UNTRACK_PARCEL,
    async_setup_services,
    async_unload_services,
)

CODE = "TESTBARCODE001"


@pytest.fixture(autouse=True)
def _services(hass):
    """Register the services directly; entry setup is tested elsewhere."""
    async_setup_services(hass)
    yield
    async_unload_services(hass)


def _tracking_entry(hass, codes: list[str] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="DAO (tracking codes)",
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_BARCODE: c} for c in (codes or [])]},
    )
    entry.add_to_hass(hass)
    return entry


def _account_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="DAO",
        unique_id="subject-1",
        # Deliberately no CONF_SOURCE on this one: a pre-split entry must
        # read as an account entry, which is what the default encodes.
        data={CONF_REFRESH_TOKEN: "rt-0", CONF_ACCOUNT_SUBJECT: "subject-1"},
        options={},
    )
    entry.add_to_hass(hass)
    return entry


def _codes(entry) -> list[str]:
    return [item[CONF_BARCODE] for item in entry.options.get(CONF_PARCELS, [])]


async def test_track_adds_the_code(hass):
    entry = _tracking_entry(hass)
    await hass.services.async_call(
        DOMAIN, SERVICE_TRACK_PARCEL, {CONF_TRACKING_CODE: CODE}, blocking=True
    )
    assert _codes(entry) == [CODE]


async def test_track_trims_but_does_not_transform_the_code(hass):
    entry = _tracking_entry(hass)
    await hass.services.async_call(
        DOMAIN, SERVICE_TRACK_PARCEL, {CONF_TRACKING_CODE: f"  {CODE.lower()} "},
        blocking=True,
    )
    assert _codes(entry) == [CODE.lower()]


async def test_tracking_the_same_code_twice_is_a_no_op(hass):
    entry = _tracking_entry(hass, [CODE])
    await hass.services.async_call(
        DOMAIN, SERVICE_TRACK_PARCEL, {CONF_TRACKING_CODE: CODE}, blocking=True
    )
    assert _codes(entry) == [CODE]


async def test_a_blank_code_is_rejected(hass):
    _tracking_entry(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_TRACK_PARCEL, {CONF_TRACKING_CODE: "   "}, blocking=True
        )


async def test_untrack_removes_the_code(hass):
    entry = _tracking_entry(hass, [CODE, "OTHER"])
    await hass.services.async_call(
        DOMAIN, SERVICE_UNTRACK_PARCEL, {CONF_TRACKING_CODE: CODE}, blocking=True
    )
    assert _codes(entry) == ["OTHER"]


async def test_untracking_an_unknown_code_changes_nothing(hass):
    entry = _tracking_entry(hass, [CODE])
    await hass.services.async_call(
        DOMAIN, SERVICE_UNTRACK_PARCEL, {CONF_TRACKING_CODE: "NOPE"}, blocking=True
    )
    assert _codes(entry) == [CODE]


async def test_an_account_only_setup_cannot_take_codes(hass):
    """The error names the tracking source, so the user knows what to add."""
    _account_entry(hass)
    with pytest.raises(ServiceValidationError, match="Tracking codes"):
        await hass.services.async_call(
            DOMAIN, SERVICE_TRACK_PARCEL, {CONF_TRACKING_CODE: CODE}, blocking=True
        )


async def test_an_account_entry_beside_a_tracking_entry_is_never_touched(hass):
    account = _account_entry(hass)
    tracking = _tracking_entry(hass)
    await hass.services.async_call(
        DOMAIN, SERVICE_TRACK_PARCEL, {CONF_TRACKING_CODE: CODE}, blocking=True
    )
    assert _codes(tracking) == [CODE]
    assert CONF_PARCELS not in account.options
    assert account.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_ACCOUNT


async def test_registration_is_idempotent(hass):
    async_setup_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)


async def test_unload_removes_both_services(hass):
    async_unload_services(hass)
    assert not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)
    assert not hass.services.has_service(DOMAIN, SERVICE_UNTRACK_PARCEL)
    # Removing twice is harmless.
    async_unload_services(hass)
