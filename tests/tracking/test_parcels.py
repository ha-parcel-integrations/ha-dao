"""Tests for the public route's normaliser: the code map, timestamps, shape."""

import logging

import pytest

from custom_components.dao.const import (
    CAPABILITIES_BY_VARIANT,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
import custom_components.dao.status as status_module
from custom_components.dao.tracking import parcels as tracking_parcels
from custom_components.dao.tracking.parcels import (
    build_history,
    event_timestamp,
    normalize_tracking_parcel,
    parcel_key,
)

from .payloads import CODE, CONFIRMED_TIMELINE, event, found, parcel, pickup_block

# The canonical key set, in order. Guarded here as well as on the account
# source: both normalisers must publish exactly the contract.
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


@pytest.fixture(autouse=True)
def _reset_one_shot_warnings():
    """One-shot warning state is module-global; reset it per test."""
    status_module.reset_one_shot_warnings()
    tracking_parcels.reset_one_shot_warnings()


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------


def test_normalize_publishes_exactly_the_canonical_keys():
    result = normalize_tracking_parcel(CODE, parcel())
    assert list(result) == CANONICAL_KEYS


def test_an_unknown_code_still_gets_a_parcel():
    """A code DAO does not know yet must still produce a sensor."""
    result = normalize_tracking_parcel(CODE, None)
    assert result["barcode"] == CODE
    assert result["status"] is ParcelStatus.UNKNOWN
    assert result["delivered"] is False
    assert result["raw_status"] is None
    assert result["pickup_point"] is None
    assert result["url"] is not None


def test_fields_that_are_none_on_purpose():
    result = normalize_tracking_parcel(CODE, parcel())
    assert result["weight"] is None
    assert result["dimensions"] is None
    assert result["planned_from"] is None
    assert result["planned_to"] is None


def test_capabilities_match_what_the_tracking_source_fills():
    """What the docs site claims for this source is what it actually sets."""
    result = normalize_tracking_parcel(
        CODE, parcel(pickup=True), include_history=True
    )
    filled = {key for key in KNOWN_CAPABILITIES if result.get(key) is not None}
    assert filled == set(CAPABILITIES_BY_VARIANT["Tracking codes"])


def test_raw_is_the_whole_payload_untouched():
    body = parcel(pickup=True)
    assert normalize_tracking_parcel(CODE, body)["raw"] == body


# ---------------------------------------------------------------------------
# status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1, ParcelStatus.REGISTERED),
        (11, ParcelStatus.IN_TRANSIT),
        (12, ParcelStatus.IN_TRANSIT),
        (23, ParcelStatus.IN_TRANSIT),
        (25, ParcelStatus.IN_TRANSIT),
        (26, ParcelStatus.IN_TRANSIT),
        (32, ParcelStatus.AT_PICKUP_POINT),
        (51, ParcelStatus.DELIVERED),
    ],
)
def test_every_confirmed_code_maps(code, expected):
    body = parcel(events=[event(code, "text")])
    assert normalize_tracking_parcel(CODE, body)["status"] is expected


def test_the_newest_event_decides_the_status():
    """``events`` is newest first on this route."""
    body = parcel(
        events=[
            event(51, "Pakken er udleveret"),
            event(32, "Klar til afhentning"),
            event(1, "Oprettet"),
        ]
    )
    result = normalize_tracking_parcel(CODE, body)
    assert result["status"] is ParcelStatus.DELIVERED
    assert result["delivered"] is True
    assert result["delivered_at"] is not None


def test_an_unmapped_code_is_unknown_and_warns_once(caplog):
    body = parcel(events=[event(99, "Noget helt nyt")])
    with caplog.at_level(logging.WARNING):
        first = normalize_tracking_parcel(CODE, body)
        normalize_tracking_parcel(CODE, body)
    assert first["status"] is ParcelStatus.UNKNOWN
    assert len([r for r in caplog.records if "event code 99" in r.message]) == 1


def test_an_unmapped_code_is_never_guessed_from_its_text():
    """No pattern-matching on the Danish description — unknown stays unknown."""
    body = parcel(events=[event(97, "Pakken er udleveret")])
    assert normalize_tracking_parcel(CODE, body)["status"] is ParcelStatus.UNKNOWN


def test_a_malformed_leading_event_does_not_decide_the_status():
    body = parcel(
        events=[
            {"date": {}, "description": "no raw block"},
            event(12, "Sorteret"),
        ]
    )
    assert normalize_tracking_parcel(CODE, body)["status"] is ParcelStatus.IN_TRANSIT


def test_a_parcel_with_no_events_is_unknown():
    body = parcel(events=[])
    result = normalize_tracking_parcel(CODE, body)
    assert result["status"] is ParcelStatus.UNKNOWN
    assert result["delivered_at"] is None


# ---------------------------------------------------------------------------
# pickup
# ---------------------------------------------------------------------------


def test_pickup_is_true_as_soon_as_a_shop_is_named():
    """The public route names the daoSHOP before the parcel arrives there."""
    body = parcel(events=[event(23, "På vej til daoSHOP")], pickup=True)
    result = normalize_tracking_parcel(CODE, body)
    assert result["status"] is ParcelStatus.IN_TRANSIT
    assert result["pickup"] is True
    assert result["pickup_point"] == pickup_block()["name"]


def test_pickup_is_true_at_pickup_point_without_a_shop_block():
    body = parcel(events=[event(32, "Klar til afhentning")])
    result = normalize_tracking_parcel(CODE, body)
    assert result["pickup"] is True
    assert result["pickup_point"] is None


def test_a_home_delivery_is_not_a_pickup():
    body = parcel(events=[event(12, "Sorteret")])
    assert normalize_tracking_parcel(CODE, body)["pickup"] is False


# ---------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------


def test_an_iso_tidspunkt_is_read_as_copenhagen_local_time():
    """Summer and winter, since the one captured parcel straddles the change."""
    summer = event_timestamp(event(12, "x", tidspunkt="2026-10-24T08:38:48"))
    assert summer == "2026-10-24T08:38:48+02:00"
    # Copenhagen leaves summer time on 2026-10-25.
    winter = event_timestamp(event(12, "x", tidspunkt="2026-10-26T08:38:48"))
    assert winter == "2026-10-26T08:38:48+01:00"


def test_an_offset_in_the_value_is_honoured():
    stamp = event_timestamp(event(12, "x", tidspunkt="2026-10-25T08:38:48+00:00"))
    assert stamp.endswith("+00:00")


def test_a_time_only_tidspunkt_is_dated_from_the_localised_block(caplog):
    with caplog.at_level(logging.WARNING):
        stamp = event_timestamp(
            event(12, "x", tidspunkt="08:38:48", day="25. oktober", year="2026")
        )
    assert stamp.startswith("2026-10-25T08:38:48")
    assert any("without a date" in r.message for r in caplog.records)


def test_an_english_month_name_is_understood_too():
    stamp = event_timestamp(
        event(12, "x", tidspunkt="08:38", day="25 October", year="2026")
    )
    assert stamp.startswith("2026-10-25T08:38:00")


def test_a_day_first_datetime_is_read_and_flagged(caplog):
    with caplog.at_level(logging.WARNING):
        stamp = event_timestamp(event(12, "x", tidspunkt="25-10-2026 08:38:48"))
    assert stamp.startswith("2026-10-25T08:38:48")
    assert any("without a date" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "broken",
    [
        {"tidspunkt": "08:38:48", "day": "25.", "year": ""},
        {"tidspunkt": "08:38:48", "day": "ingen måned", "year": "2026"},
        {"tidspunkt": "32:99", "day": "25. oktober", "year": "2026"},
        {"tidspunkt": "", "day": "25. oktober", "year": "2026"},
        {"tidspunkt": "40:00:00", "day": "25. oktober", "year": "2026"},
        {"tidspunkt": "99-99-2026 08:38:48"},
    ],
)
def test_an_unplaceable_event_keeps_no_timestamp(broken):
    """Never dated from "today": a wrong date is worse than no entry."""
    assert event_timestamp(event(12, "x", **broken)) is None


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_is_oldest_first_and_carries_the_contract_keys():
    # Newest first, as the route returns them: the last-confirmed code gets
    # the latest date.
    events = [
        event(code, text, tidspunkt=f"2026-10-{20 + index:02d}T08:00:00")
        for index, (code, text) in enumerate(CONFIRMED_TIMELINE)
    ][::-1]
    history = build_history(parcel(events=events))
    assert [entry["timestamp"] for entry in history] == sorted(
        entry["timestamp"] for entry in history
    )
    assert set(history[0]) == {"timestamp", "status", "raw_status"}
    assert history[-1]["status"] is ParcelStatus.DELIVERED
    assert history[-1]["raw_status"] == "Pakken er udleveret"


def test_history_is_off_unless_asked_for():
    assert normalize_tracking_parcel(CODE, parcel())["history"] is None
    assert normalize_tracking_parcel(CODE, parcel(), include_history=True) != []


def test_history_is_capped():
    events = [
        event(12, "Sorteret", tidspunkt=f"2026-10-25T08:{minute:02d}:00")
        for minute in range(30)
    ]
    assert len(build_history(parcel(events=events), max_events=5)) == 5


def test_history_drops_events_it_cannot_place_in_time():
    events = [event(12, "placed"), event(12, "unplaced", tidspunkt="noon-ish")]
    history = build_history(parcel(events=events))
    assert len(history) == 1


def test_history_skips_a_non_dict_event():
    assert build_history({"events": ["nope", event(12, "ok")]})


def test_history_entry_without_a_code_keeps_a_null_status():
    history = build_history(
        {"events": [{"date": {}, "description": "x", "raw": {"tidspunkt": "2026-10-25T08:00:00"}}]}
    )
    assert history[0]["status"] is None


# ---------------------------------------------------------------------------
# one-shot first-sighting warning
# ---------------------------------------------------------------------------


def test_a_real_payload_warns_once_that_the_shape_rests_on_one_parcel(caplog):
    with caplog.at_level(logging.WARNING):
        normalize_tracking_parcel(CODE, parcel())
        normalize_tracking_parcel(CODE, parcel())
    hits = [r for r in caplog.records if "single real parcel" in r.message]
    assert len(hits) == 1


def test_a_not_found_code_does_not_warn_about_the_shape(caplog):
    with caplog.at_level(logging.WARNING):
        normalize_tracking_parcel(CODE, None)
    assert not [r for r in caplog.records if "single real parcel" in r.message]


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def test_parcel_key_trims_and_nothing_else():
    assert parcel_key("  abc123 ") == "abc123"
    assert parcel_key("AbC") == "AbC"
    assert parcel_key("") == ""


def test_sender_and_receiver_come_from_the_payload():
    result = normalize_tracking_parcel(CODE, found()["parcel"])
    assert result["sender"] == "Test***hop"
    assert result["receiver"] is None
