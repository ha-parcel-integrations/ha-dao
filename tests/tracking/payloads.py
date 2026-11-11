"""Synthetic payloads for DAO's keyless public barcode route.

Hand-built from the field names and the one confirmed event timeline in the
carrier's own research notes — **none of this is a captured response**, and
every person-shaped field is blank or obviously fake on purpose. Keep them in
one module so a correction only has to happen once.
"""

from __future__ import annotations

from typing import Any

CODE = "TESTBARCODE001"
SECOND_CODE = "TESTBARCODE002"
ENDPOINT = "https://dao.as/wp-json/dao-wp/v1/track-parcel"
NONCE = "testnonce00"

# The page's embedded config object, as the tracking page serves it.
PAGE_HTML = (
    "<html><head><script>var TRACK_PARCEL_API = "
    '{"endpoint":"' + ENDPOINT + '","nonce":"' + NONCE + '",'
    '"lang":"DA","turnstile_site_key":"0x0000"};</script></head>'
    "<body>find din pakke</body></html>"
)


def event(
    code: int,
    description: str,
    *,
    tidspunkt: str | None = None,
    day: str | None = None,
    year: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    """One event of the public route's ``events[]`` list.

    ``date`` is the localised block the page renders; ``raw`` is the
    API-only block a client actually reads.
    """
    return {
        "date": {"day": day or "25. oktober", "year": year or "2026", "time": time or "08:38"},
        "description": description,
        "raw": {
            "tidspunkt": tidspunkt if tidspunkt is not None else "2026-10-25T08:38:48",
            "haendelse": code,
            "beskrivelse": description,
            "pakketype": "P",
            "sted": "",
        },
    }


def pickup_block() -> dict[str, Any]:
    """A ``pickup`` block, as a daoSHOP parcel carries it."""
    return {
        "name": "daoSHOP Testbutik",
        "shop_id": "12345",
        "address": "",
        "postnr": "",
        "city": "",
        "opening_hours": {
            "man": "07:00-20:00",
            "tir": "07:00-20:00",
            "ons": "07:00-20:00",
            "tor": "07:00-20:00",
            "fre": "07:00-20:00",
            "lor": "08:00-18:00",
            "son": "08:00-18:00",
        },
        "latitude": "0.0",
        "longitude": "0.0",
    }


def parcel(
    *,
    code: str = CODE,
    events: list[dict[str, Any]] | None = None,
    pickup: bool = False,
) -> dict[str, Any]:
    """A ``found: true`` parcel object.

    ``events`` is newest first, as the route returns it.
    """
    if events is None:
        events = [
            event(12, "Sorteret på: terminal Erritsø", tidspunkt="2026-10-26T04:10:00"),
            event(25, "Indleveret i daoSHOP"),
            event(1, "Forsendelse er oprettet, afventer registrering"),
        ]
    body: dict[str, Any] = {
        "barcode": code,
        "latest_event": {"description": events[0]["description"]} if events else {},
        "events": events,
        "external_tracking": "",
        "dist_barcode": "",
        "sender_masked": "Test***hop",
        "recipient": {"name": "", "address": "", "postal": ""},
    }
    if pickup:
        body["pickup"] = pickup_block()
    return body


def found(parcel_body: dict[str, Any] | None = None) -> dict[str, Any]:
    """A successful lookup envelope."""
    return {"found": True, "parcel": parcel_body or parcel()}


NOT_FOUND = {"found": False}

# The full confirmed timeline of the one real parcel, oldest → newest, as
# codes. The payload is newest first, so tests reverse it.
CONFIRMED_TIMELINE = [
    (1, "Forsendelse er oprettet, afventer registrering"),
    (25, "Indleveret i daoSHOP"),
    (26, "På vej til terminal"),
    (11, "Ankommet til terminal"),
    (12, "Sorteret på: terminal Erritsø"),
    (23, "På vej til daoSHOP"),
    (32, "Klar til afhentning"),
    (51, "Pakken er udleveret"),
]
