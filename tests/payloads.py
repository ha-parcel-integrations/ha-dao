"""Synthetic DAO API payloads shared by the test modules.

These are hand-built from the field names confirmed by static analysis of
the DAO app (see the carrier's own research notes) — **none of this was ever
fetched from a real DAO account**. There is still no real-parcel capture for
this carrier; keep these payloads in one module so a future correction only
has to happen in one place.
"""
from __future__ import annotations

from typing import Any

TRACKING_ID = "TESTBARCODE001"
ACTIVE_CODE = TRACKING_ID
SECOND_CODE = "TESTBARCODE002"
PICKUP_POINT_ID = "PP-CPH-001"


def status_event(
    status_type: str,
    timestamp: str,
    status_code: int,
    status_text: str = "",
) -> dict:
    """One entry of DAO's own event timeline (parcel-level or history)."""
    return {
        "timestamp": timestamp,
        "status": {
            "statusType": status_type,
            "statusCode": status_code,
            "statusText": status_text,
        },
    }


def pickup_point_response(point_id: str = PICKUP_POINT_ID) -> dict:
    """A resolved ``pickup-points/{id}`` response.

    Mirrors ``DaoShopPickupInfoCard``'s destructured prop shape — the field
    names are confirmed, the values here are invented.
    """
    return {
        "id": point_id,
        "name": "Example Point Central Station",
        "streetAddress": "Testvej 1",
        "city": "Copenhagen",
        "zipCode": "1000",
        "type": "pickup_point",
        "coordinates": {"latitude": 55.6761, "longitude": 12.5683},
        "openingHours": {
            "monday": "08:00 - 18:00",
            "tuesday": "08:00 - 18:00",
            "wednesday": "08:00 - 18:00",
            "thursday": "08:00 - 18:00",
            "friday": "08:00 - 18:00",
            "saturday": "10:00 - 14:00",
            "sunday": "closed",
        },
        "distance": 350,
        "distanceMinutes": 5,
    }


def parcel(
    status_type: str,
    *,
    tracking_id: str = TRACKING_ID,
    status_code: int = 1,
    status_text: str = "",
    timestamp: str = "2026-04-29T13:12:42Z",
    pickup_point_id: str | None = None,
    pickup_point: dict | None = None,
    events: list[dict] | None = None,
    sender: str | None = "Example Shop",
    receiver: str | None = "Jane Doe",
) -> dict:
    """Build one inbox-item-shaped raw parcel dict.

    ``pickup_point`` stands in for the object a separate
    ``pickup-points/{id}`` call would resolve — DAO's inbox item only ever
    carries the id, never the resolved object, so a caller (not
    ``normalize_parcel``, which stays I/O-free) is expected to merge it in
    under this key when ``pickup_point_id`` is set.
    """
    raw: dict[str, Any] = {
        "trackingId": tracking_id,
        "bound": "IN",
        "sender": {"name": sender} if sender is not None else None,
        "receiver": {"name": receiver} if receiver is not None else None,
        "lastEvent": {
            "timestamp": timestamp,
            "status": {
                "statusType": status_type,
                "statusCode": status_code,
                "statusText": status_text,
            },
        },
        "delivery": {"pickupPointId": pickup_point_id} if pickup_point_id else {},
    }
    if events is not None:
        raw["events"] = events
    if pickup_point is not None:
        raw["pickupPoint"] = pickup_point
    return raw


def registered_sample(tracking_id: str = TRACKING_ID) -> dict:
    """STATUS_PROCESSING — confirmed mapping, no ambiguity."""
    return parcel(
        "STATUS_PROCESSING", tracking_id=tracking_id, status_code=1, status_text="Announced"
    )


def in_transit_sample(tracking_id: str = TRACKING_ID) -> dict:
    """STATUS_IN_TRANSIT — confirmed mapping, no ambiguity."""
    return parcel(
        "STATUS_IN_TRANSIT",
        tracking_id=tracking_id,
        status_code=48,
        status_text="In transit",
    )


def returning_sample(tracking_id: str = TRACKING_ID) -> dict:
    """STATUS_RETURN — confirmed mapping, no ambiguity."""
    return parcel(
        "STATUS_RETURN", tracking_id=tracking_id, status_code=60, status_text="Returning"
    )


def problem_sample(tracking_id: str = TRACKING_ID) -> dict:
    """STATUS_ALERT — confirmed mapping, no ambiguity."""
    return parcel(
        "STATUS_ALERT", tracking_id=tracking_id, status_code=99, status_text="Exception"
    )


def info_sample(tracking_id: str = TRACKING_ID) -> dict:
    """STATUS_INFO — confirmed mapping, no ambiguity."""
    return parcel(
        "STATUS_INFO", tracking_id=tracking_id, status_code=10, status_text="Informational"
    )


def delivered_sample(tracking_id: str = TRACKING_ID) -> dict:
    """STATUS_OK with no ``pickupPointId`` — the ASSUMED "delivered" branch."""
    return parcel(
        "STATUS_OK",
        tracking_id=tracking_id,
        status_code=32,
        status_text="Delivered",
        timestamp="2026-04-29T13:12:42Z",
        events=[
            status_event("STATUS_PROCESSING", "2026-04-24T09:00:00Z", 1, "Announced"),
            status_event("STATUS_IN_TRANSIT", "2026-04-26T09:00:00Z", 48, "In transit"),
            status_event("STATUS_IN_TRANSIT", "2026-04-28T09:00:00Z", 48, "In transit"),
            status_event("STATUS_OK", "2026-04-29T13:12:42Z", 32, "Delivered"),
        ],
    )


def pickup_sample(tracking_id: str = TRACKING_ID) -> dict:
    """STATUS_OK with ``pickupPointId`` set — the ASSUMED "at_pickup_point" branch."""
    return parcel(
        "STATUS_OK",
        tracking_id=tracking_id,
        status_code=32,
        status_text="Ready for collection",
        pickup_point_id=PICKUP_POINT_ID,
        pickup_point=pickup_point_response(),
    )


def pending_sample() -> dict:
    """A tracked code with no lastEvent at all yet."""
    return {"trackingId": SECOND_CODE, "sender": None, "receiver": None}
