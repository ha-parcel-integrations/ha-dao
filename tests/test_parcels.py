"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.dao.parcels as parcels_module
from custom_components.dao.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.dao.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import (
    delivered_sample,
    in_transit_sample,
    info_sample,
    pending_sample,
    pickup_sample,
    problem_sample,
    registered_sample,
    returning_sample,
    status_event,
)


@pytest.fixture(autouse=True)
def _reset_one_shot_warnings():
    """One-shot warning state is module-global — reset it so tests don't
    depend on execution order."""
    parcels_module._unmapped_statuses_logged.clear()
    parcels_module._unmapped_status_codes_logged.clear()
    parcels_module._assumed_ok_split_warned = False
    parcels_module._unverified_history_warned = False
    parcels_module._unverified_pickup_point_warned = False
    yield


# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_type,expected",
    [
        ("STATUS_PROCESSING", ParcelStatus.REGISTERED),
        ("STATUS_IN_TRANSIT", ParcelStatus.IN_TRANSIT),
        ("STATUS_RETURN", ParcelStatus.RETURNING),
        ("STATUS_ALERT", ParcelStatus.PROBLEM),
        ("STATUS_INFO", ParcelStatus.UNKNOWN),
    ],
)
def test_map_parcel_status_confirmed_members(status_type, expected):
    assert map_parcel_status(status_type) == expected


def test_map_parcel_status_ok_without_pickup_point_is_delivered():
    assert map_parcel_status("STATUS_OK", None) == ParcelStatus.DELIVERED


def test_map_parcel_status_ok_with_pickup_point_is_at_pickup_point():
    assert map_parcel_status("STATUS_OK", "PP1") == ParcelStatus.AT_PICKUP_POINT


def test_map_parcel_status_ok_warns_once_ever(caplog):
    map_parcel_status("STATUS_OK", None)
    map_parcel_status("STATUS_OK", "PP1")
    assert caplog.text.count("unverified assumption") == 1
    assert "issues/new" in caplog.text


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("STATUS_TELEPORTED") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("STATUS_SOMETHING_NEW") is None
    assert map_event_status("STATUS_PROCESSING") == ParcelStatus.REGISTERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("STATUS_ABDUCTED") == ParcelStatus.UNKNOWN
    assert map_parcel_status("STATUS_ABDUCTED") == ParcelStatus.UNKNOWN
    assert caplog.text.count("STATUS_ABDUCTED") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_epoch_milliseconds():
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest():
    history = build_history(delivered_sample()["events"])
    assert len(history) == 4
    assert history[0]["raw_status"] == 1
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_max_events():
    events = [
        status_event("STATUS_IN_TRANSIT", f"2026-04-{day:02d}T10:00:00Z", 48)
        for day in range(1, 26)
    ]
    assert len(build_history(events, max_events=20)) == 20


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"status": {"statusType": "STATUS_IN_TRANSIT"}}]) == []  # no timestamp
    assert build_history(["not-a-dict"]) == []


def test_build_history_keeps_unparseable_timestamp_last():
    history = build_history(
        [
            status_event("STATUS_PROCESSING", "2026-04-24T10:00:00Z", 1),
            status_event("STATUS_IN_TRANSIT", "not-a-date", 48),
        ]
    )
    assert [entry["raw_status"] for entry in history] == [1, 48]


def test_build_history_applies_parent_pickup_point_id_to_status_ok():
    """A STATUS_OK event has no pickup-point field of its own — the parent
    parcel's value is applied uniformly across its whole history."""
    history = build_history(
        [status_event("STATUS_OK", "2026-04-29T13:12:42Z", 32)],
        pickup_point_id="PP1",
    )
    assert history[0]["status"] == ParcelStatus.AT_PICKUP_POINT


def test_build_history_warns_on_every_distinct_status_code(caplog):
    build_history([status_event("STATUS_IN_TRANSIT", "2026-04-24T10:00:00Z", 12345)])
    assert "statusCode=12345" in caplog.text


def test_build_history_warns_once_ever_when_it_returns_real_entries(caplog):
    """The events[] shape is only confirmed by reading the app's own code,
    never by a live call — flag it the moment it actually produces history."""
    build_history(delivered_sample()["events"])
    build_history(delivered_sample()["events"])
    assert caplog.text.count("only confirmed by reading the app's own code") == 1
    assert "issues/new" in caplog.text


def test_build_history_does_not_warn_when_there_is_nothing_to_show():
    build_history(None)
    build_history([])
    build_history([{"status": {"statusType": "STATUS_IN_TRANSIT"}}])  # no timestamp
    assert parcels_module._unverified_history_warned is False


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample.

    Copy this test into a real carrier's own test_parcels.py verbatim — it
    stays correct for whatever subset of CAPABILITIES that carrier declares.
    """
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(in_transit_sample())
    pickup = normalize_parcel(pickup_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_normalize_registered_parcel():
    parcel = normalize_parcel(registered_sample())
    assert parcel["carrier"] == "DAO"
    assert parcel["barcode"] == "TESTBARCODE001"
    assert parcel["sender"] == "Example Shop"
    assert parcel["receiver"] == "Jane Doe"
    assert parcel["status"] == ParcelStatus.REGISTERED
    assert parcel["raw_status"] == 1
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None  # opt-in, default off


@pytest.mark.parametrize(
    "sample,expected_status",
    [
        (in_transit_sample, ParcelStatus.IN_TRANSIT),
        (returning_sample, ParcelStatus.RETURNING),
        (problem_sample, ParcelStatus.PROBLEM),
        (info_sample, ParcelStatus.UNKNOWN),
    ],
)
def test_normalize_confirmed_status_types(sample, expected_status):
    assert normalize_parcel(sample())["status"] == expected_status


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42Z"
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["url"] == "https://send.dao.as/parcel/TESTBARCODE001"


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 4
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_pickup_parcel():
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Example Point Central Station"


def test_normalize_pickup_point_id_without_resolved_object_is_none():
    """The id is enough to derive `status`/`pickup`; the *name* needs the
    separate pickup-points call to have actually resolved it."""
    raw = pickup_sample()
    del raw["pickupPoint"]
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup_point"] is None


def test_normalize_pickup_point_warns_once_ever(caplog):
    """The pickup-point shape is only confirmed by reading the app's own
    code, never by a live call — flag it the moment a name is resolved."""
    normalize_parcel(pickup_sample())
    normalize_parcel(pickup_sample())
    assert caplog.text.count("only confirmed by reading the app's own code") == 1
    assert "issues/new" in caplog.text


def test_normalize_does_not_warn_when_pickup_point_is_unresolved():
    raw = pickup_sample()
    del raw["pickupPoint"]
    normalize_parcel(raw)
    assert parcels_module._unverified_pickup_point_warned is False


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-scanned code still yields a full parcel dict."""
    parcel = normalize_parcel(pending_sample())
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_blank_fields_become_none():
    raw = registered_sample()
    raw["sender"] = None
    raw["receiver"] = None
    parcel = normalize_parcel(raw)
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_normalize_keeps_raw_payload():
    raw = in_transit_sample()
    assert normalize_parcel(raw)["raw"] is raw


def test_normalize_never_maps_raw_status_from_status_code(caplog):
    """`statusCode` has no known table — it is only ever carried through."""
    raw = registered_sample()
    raw["lastEvent"]["status"]["statusCode"] = 7654321
    parcel = normalize_parcel(raw)
    assert parcel["raw_status"] == 7654321
    assert parcel["status"] == ParcelStatus.REGISTERED
    assert "statusCode=7654321" in caplog.text


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
