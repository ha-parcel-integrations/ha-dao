"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime

import pytest

from custom_components.dao.account.parcels import (
    is_pickup,
    normalize_parcel,
    parse_iso,
    planned_for_status,
    planned_window,
)
from custom_components.dao.const import ParcelStatus
from custom_components.dao.tracking.parcels import normalize_tracking_parcel

from .account import payloads as account
from .tracking import payloads as tracking

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

ZONE = datetime.fromisoformat("2099-01-01T00:00:00+01:00").tzinfo


def _collected_tracking() -> dict:
    return tracking.parcel(
        events=[
            tracking.event(51, "Pakken er udleveret", tidspunkt="2026-10-26T16:00:00"),
            tracking.event(32, "Klar til afhentning", tidspunkt="2026-10-26T09:00:00"),
        ]
    )


SAMPLES = {
    "account_registered": lambda: normalize_parcel(account.registered_sample()),
    "account_in_transit": lambda: normalize_parcel(account.in_transit_sample()),
    "account_delivered": lambda: normalize_parcel(account.delivered_sample()),
    "account_at_pickup_point": lambda: normalize_parcel(account.pickup_sample()),
    "account_en_route": lambda: normalize_parcel(
        account.en_route_to_pickup_point_sample()
    ),
    "account_collected": lambda: normalize_parcel(
        account.delivered_from_pickup_point_sample()
    ),
    "account_returning": lambda: normalize_parcel(account.returning_sample()),
    "account_problem": lambda: normalize_parcel(account.problem_sample()),
    "account_pending": lambda: normalize_parcel(account.pending_sample()),
    "tracking_pickup": lambda: normalize_tracking_parcel(
        tracking.CODE,
        tracking.parcel(events=[tracking.event(32, "Klar til afhentning")]),
    ),
    "tracking_collected": lambda: normalize_tracking_parcel(
        tracking.CODE, _collected_tracking()
    ),
    "tracking_unknown": lambda: normalize_tracking_parcel(tracking.CODE, None),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_account_parcel_stays_pickup():
    parcel = normalize_parcel(account.delivered_from_pickup_point_sample())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Example Point Central Station"


def test_an_account_pickup_bound_parcel_is_pickup_from_the_route_alone():
    raw = account.en_route_to_pickup_point_sample()
    parcel = normalize_parcel(raw)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["pickup"] is True


def test_a_collected_tracking_parcel_stays_pickup_without_a_shop_block():
    parcel = normalize_tracking_parcel(tracking.CODE, _collected_tracking())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


def test_a_collected_tracking_parcel_keeps_its_shop_name():
    body = _collected_tracking()
    body["pickup"] = tracking.pickup_block()
    parcel = normalize_tracking_parcel(tracking.CODE, body)
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == tracking.pickup_block()["name"]


def test_a_shop_block_without_a_pickup_is_not_published_as_a_point():
    parcel = normalize_tracking_parcel(
        tracking.CODE, tracking.parcel(events=[tracking.event(12, "Sorteret")])
    )
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00Z", None, zone=ZONE)
    stop = datetime.fromisoformat(end)
    assert (stop.hour, stop.minute, stop.second, stop.microsecond) == (23, 59, 59, 0)
    assert stop.date() == parse_iso(start).date()


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2099-04-29", None, zone=ZONE)
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    assert (first.hour, first.minute, first.second) == (0, 0, 0)
    assert (last.hour, last.minute, last.second, last.microsecond) == (23, 59, 59, 0)
    assert first.date() == last.date() == date(2099, 4, 29)


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.DELIVERED, ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING],
)
def test_the_window_is_emptied_for_a_finished_or_arrived_parcel(status):
    assert planned_for_status(status, "2099-04-29", "2099-04-29") == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, "a", "b") == ("a", "b")


def test_is_pickup_route_flag_status_and_events():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(False, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(None, ParcelStatus.DELIVERED, [None]) is False
