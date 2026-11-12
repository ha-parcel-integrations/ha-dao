"""Constants for the DAO parcel tracker integration."""

from enum import StrEnum

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

DOMAIN = "dao"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"  # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"  # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"  # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"  # Ready to collect at a pickup location
    DELIVERED = "delivered"  # Handed over
    RETURNING = "returning"  # Failed delivery, going back to sender
    PROBLEM = "problem"  # Carrier reports an exception/issue
    UNKNOWN = "unknown"  # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# The docs site's carrier comparison table is generated straight from this
# constant, so drift here does not stay a local mistake, it becomes a wrong
# claim on the website.
#
# "weight" and "dimensions" are confirmed absent from every tracking and
# pickup-point model DAO's own app consumes — not merely unclaimed pending
# confirmation. ``dueDate`` is the last day to collect a parcel from its
# pickup point (issue #5: 2026-11-03, seven days after "Klar til
# afhentning"), not a delivery window. ``pickup.date`` is still unseen, so
# a delivery window is not confirmed either,
# and the one field name that did look window-shaped (``deliveryAt``)
# belongs to the outbound booking flow, not to anything read back off
# tracking — so "delivery_window" stays unclaimed until a real inbound
# parcel settles what these fields actually mean.
# Two sources, one dict: the keyless public barcode lookup and the signed-in
# account inbox are genuinely different backends, so each declares its own
# set rather than having a future divergence silently intersected away or
# overclaimed. They are identical today — both confirmed on real parcels —
# which is exactly why the dict is cheap to keep correct now.
#
# Key order is display order on the docs site's comparison table; each key is
# labelled as the integration's own source picker words it.
CAPABILITIES_BY_VARIANT = {
    "Account": frozenset({"pickup_point", "url", "history"}),
    "Tracking codes": frozenset({"pickup_point", "url", "history"}),
}

# The docs site reads the flat name too; keep it aliased rather than
# duplicated.
CAPABILITIES = CAPABILITIES_BY_VARIANT["Account"]

# Fields not confirmed yet — the docs site shows them as "awaiting data".
# Move a field into the declaration above once a real parcel shows it. Keyed
# per source, as the docs site requires alongside CAPABILITIES_BY_VARIANT:
# neither route has shown a bounded delivery window.
PENDING_CAPABILITIES_BY_VARIANT = {
    "Account": frozenset({"delivery_window"}),
    "Tracking codes": frozenset({"delivery_window"}),
}
PENDING_CAPABILITIES = PENDING_CAPABILITIES_BY_VARIANT["Account"]

# Which backend a config entry talks to. An entry created before the public
# barcode source existed carries no CONF_SOURCE at all, so **every** read
# must default to SOURCE_ACCOUNT — that default is the migration, and there
# is no entry version bump.
CONF_SOURCE = "source"
SOURCE_ACCOUNT = "account"
SOURCE_TRACKING = "tracking"


def entry_source(entry: ConfigEntry) -> str:
    """Return which backend an entry talks to.

    Every read goes through here: the default is the migration for entries
    created before the keyless source existed, and open-coding it would let
    the two defaults drift apart. It lives in ``const`` rather than
    ``__init__`` so ``services.py`` can use it without an import cycle.
    """
    return entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT)


# Tracking-code source: the user's own list of codes, in entry options.
CONF_PARCELS = "parcels"
CONF_BARCODE = "barcode"
# The user-facing field name, in services and the options form. "Tracking
# code" is how the whole suite words it, even where the carrier says
# "barcode"; the stored key stays CONF_BARCODE so no entry migration is
# involved.
CONF_TRACKING_CODE = "tracking_code"

# The public tracking page, which hands out both the lookup endpoint and the
# request nonce. Also the user-facing deep link (TRACKING_URL below).
DAO_TRACKING_PAGE_URL = "https://dao.as/find-din-pakke/"

# Public lookups are issued one at a time, with a gap between them. DAO's
# public route sits behind a generic, short-window rate limiter that answers a
# burst with HTTP 429 and no retry hint, so a household with several codes
# must trickle rather than fire them all at once — measured 2026-11-11: a
# dozen requests inside a few seconds is enough to trip it.
TRACKING_FETCH_GAP_SECONDS = 2.0

# A plain 429 waits this long before the next attempt (doubling, capped),
# rather than the suite's usual 60 s: this limiter needs minutes, not seconds.
RATE_LIMIT_BACKOFF_START_SECONDS = 300
RATE_LIMIT_BACKOFF_CAP_SECONDS = 3600

# Backoff after DAO's bot check rejects a lookup. Deliberately an order of
# magnitude longer than the 429 backoff: a ban is not a rate limit, and
# retrying it on the normal cadence is what would keep it in place.
BAN_BACKOFF_START_SECONDS = 3600
BAN_BACKOFF_CAP_SECONDS = 21600

# OAuth constants for DAO's manual authorization-code + PKCE flow
# (config_flow.py) and its bearer-authenticated tracking endpoint (api.py).
# Home Assistant cannot be the redirect target for either of DAO's public
# clients, which is why the flow shows a URL and asks the user to paste the
# browser's address bar back, instead of a normal HA OAuth handshake.
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCOUNT_SUBJECT = "account_subject"
DAO_CLIENT_ID = "dao-app"
DAO_REDIRECT_URI = "daoapp://auth-callback"
DAO_AUTHORIZATION_URL = (
    "https://auth.dao.as/realms/consumers/protocol/openid-connect/auth"
)
DAO_TOKEN_URL = "https://auth.dao.as/realms/consumers/protocol/openid-connect/token"
DAO_TRACKING_URL = "https://apiv2.dao.as/consumer/shipment/tracking/v1/tracking"
DAO_PICKUP_POINT_URL = (
    "https://apiv2.dao.as/consumer/shipment/pickup-point/v1/pickup-points"
)
DAO_REDIRECT_URL_DOCS_URL = (
    "https://github.com/ha-parcel-integrations/ha-dao/blob/main/docs/"
    "finding-the-redirect-url.md"
)
TRACKING_URL = "https://dao.as/find-din-pakke/#q={tracking_code}"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option (see scaffold/CLAUDE.md's "Dynamic polling"
# section for the full algorithm and the reasoning behind it).
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one active parcel
# is out_for_delivery within HOT_LOOKAHEAD_HOURS of its planned_from (or has
# no planned_from at all); mid = anything else still in flight. Unlike the
# barcode-based model, the account-based coordinator never fully stops — the
# mid-tier poll is also how a new shipment gets discovered.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default: it is a large attribute, and on carriers that
# need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
