# Working in this repository

Home Assistant custom integration for **DAO** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

API mechanics — endpoints, parameters, status vocabularies — live in the
private `carrier-research/dao/api/` and are **never** copied here.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).
- **If this carrier can reach `ParcelStatus.AT_PICKUP_POINT` from a real raw
  status/code**, it needs an `awaiting_pickup` sensor — see *Parcel contract*
  in `CONVENTIONS.md`. Say "pickup point", not "ServicePoint"/"parcel
  shop"/"locker", for the generic concept. `ha-dhl-nl`, `ha-dpd`, `ha-gls`,
  `ha-inpost` are reference implementations; `dao` here does not
  demonstrate it yet.

## Carrier-specific notes

DAO is account-based: there is no per-parcel tracking-code entry, one signed-in
account discovers whatever DAO's own app/site already links to it.

**Why not `config_entry_oauth2_flow`.** Neither of DAO's two public Keycloak
clients accepts a Home Assistant redirect URI (both `daoapp://auth-callback`
and the webshop's origin-scoped redirect reject anything HA could receive on),
so the standard OAuth2 helper — which assumes HA itself is the redirect
target — does not apply. `config_flow.py` instead runs a single `dao` step:
generate a PKCE verifier/challenge/state, show the user DAO's own login URL to
open in a **desktop browser** (a phone with the DAO app installed will
otherwise swallow the redirect before the user can copy it), and have them
paste the resulting URL back in. The pasted `state` is checked against the
flow's own before the code is ever exchanged, since the user is transcribing
a URL by hand. **A bare code (no surrounding URL) is rejected up front**
(`config_flow._exchange`, error key `paste_full_url`) rather than accepted
and silently skipped past the state check — a bare code carries no state to
verify, and accepting it anyway would reopen exactly the login-CSRF hole
`state` exists to close (an attacker's own authorization code, pasted by the
victim, would silently link the attacker's DAO account to this entry).
Rejecting and asking for the full URL is the safe fix; weakening the check
for that one path is not, even though it means a user who only copies the
`code=` parameter has to go back and copy the whole address bar instead.

**Auth-error → HA-exception mapping.** `DAOAuthError` (a rejected or expired
token) always means "only a fresh authorization can recover this": raised
during `async_setup_entry`'s initial `client.async_refresh()`, it becomes
`ConfigEntryAuthFailed` and starts reauth immediately, before any session is
left open. Raised later from the coordinator's poll (`DAOAuthError` out of
`async_get_parcels`), it becomes `ConfigEntryAuthFailed` from there instead,
which HA turns into the same reauth flow on a running entry. Every other
`DAOApiError` (a 5xx, a malformed body, a network error) becomes
`ConfigEntryNotReady` — an outage must retry with backoff, never push a user
into a reauth they cannot complete with a token that will work again on its
own.

**Refresh-token handling.** Only `refresh_token` (plus the account identity
used for `async_set_unique_id`) is persisted in the config entry; the access
token lives in memory only and is refreshed proactively within 120 s of
expiry, and once more on a 401 before giving up. Real refresh-token/session
lifetime numbers are not yet measured (blocked on the real-parcel capture),
so there is currently no independent keepalive timer separate from the
coordinator's own poll cycle — a long dynamic-polling interval could
theoretically let the session go idle between refreshes. Revisit once session
lifetime is measured; do not add a keepalive on a guess.

**Reauth account-mismatch guard.** Reauth re-runs the same three steps and
calls `async_set_unique_id` + `_abort_if_unique_id_mismatch` before touching
the entry, so pasting a different DAO account's login aborts with
`wrong_account` instead of silently rebinding an existing entry's sensors to
someone else's parcels.

**Status mapping (`parcels.py`).** `_STATUS_MAP` covers DAO's confirmed
six-value `statusType` enum. `STATUS_OK` is not in that map — it first checks
`_STATUS_OK_CONFIRMED_CODES` (a real `statusCode` → `ParcelStatus` override),
and only falls back to splitting on `delivery.pickupPointId` into `delivered`
(unset) or `at_pickup_point` (set) — an ASSUMED split (never checked against
a real parcel's event timeline for anything but code 51) that fires a
one-shot `WARNING` the first time either branch is exercised
(`parcels._warn_assumed_ok_split`), not just a comment. **Code 51 is
confirmed `delivered`**, even with a `pickupPointId` set (issue #7,
2026-09-17): a real parcel collected from a pickup point reported
`statusCode: 51`, `statusText: "Pakken er udleveret"` ("handed over") — the
presence of a pickup point alone does not mean "still waiting there", it can
also mean "was delivered via one". Add further confirmed codes to that dict
as real parcels report them; never widen the assumption itself on a guess.
`out_for_delivery` is unreachable: no field in the consumed shape expresses
it, and none is invented. `lastEvent.status.statusCode` has no known table
beyond `_STATUS_OK_CONFIRMED_CODES` — every distinct value seen logs its own
one-shot warning (`parcels._warn_unmapped_status_code`), since none of it is
safe to assume mapped. `raw_status` is `statusText` (the carrier's own
human-readable text), falling back to the bare `statusCode` only when no
text is present — matching the rest of the suite (issue #7 also caught this:
it used to carry the bare code even though `statusText` was always present,
which broke a third-party card expecting a string).

**Fields that are `None` on purpose** (must agree with `const.py`'s
`CAPABILITIES`): `weight` and `dimensions` are confirmed absent from every
tracking and pickup-point model in the app — always `None`. `planned_from`/
`planned_to` stay unclaimed (`delivery_window` is deliberately absent from
`CAPABILITIES`) until a real inbound parcel confirms what the carrier's
ETA-shaped fields (`dueDate`, `pickup.date`) actually mean; claiming a
delivery window on the current guess would risk shipping a wrong ETA, which
is worse than shipping none. `pickup_point`, `url` and `history` are the only
extras currently claimed. `pickup_point`'s *name* additionally depends on a
resolved pickup-point object being merged into the raw parcel dict before
`normalize_parcel()` sees it — `coordinator.py`'s `_enrich_parcel()` does
that merge now (`api.async_get_pickup_point()`, cached on the client per id
since pickup points are near-static and shared across parcels), so a
tracked `pickup: true` parcel resolves a real `pickup_point` name once the
lookup succeeds. `history` is opt-in (`CONF_INCLUDE_HISTORY`, off by
default) and comes from `api.async_get_parcel_detail()`'s `events[]`,
fetched once per parcel and re-fetched only when that parcel's raw
`statusCode` changes — a delivered parcel's timeline is then fixed and is
never refetched again. Both calls are unverified against a live account
(see `TODO.md`): a failure on either degrades that one parcel to no
history / no pickup point rather than failing the poll (the per-parcel
tripwire below), and the first time either actually produces data it fires
its own one-shot `WARNING` (`parcels._warn_unverified_history` /
`parcels._warn_unverified_pickup_point`), same standing as the `STATUS_OK`
split above.

**`awaiting_pickup` sensor** exists (`sensor.DAOAwaitingPickupSensor`) since
the mapping above can reach `ParcelStatus.AT_PICKUP_POINT`; its unique id is
in `sensor.py`'s stale-entity sweep exclusion set, same as the other summary
sensors.

**Incoming and outgoing come from the same inbox call.** `api.py`'s
`async_get_parcels()` already fetches and combines both `bound: "IN"` and
`bound: "OUT"` in one list (two requests, one per bound, merged before
returning) — `coordinator.py` splits that one fetched list into
`data`/`delivered` (incoming) and `outgoing`/`delivered_outgoing` via
`parcels.parcel_direction()`, the same one-coordinator-splits-one-list shape
as `ha-posten-bring`'s `direction` field. Before this split existed (fixed
alongside the `STATUS_OK`/`raw_status` issues above, issue #7), an outgoing
parcel was silently counted and listed as incoming — `bound` was fetched and
used only to build the two API requests, never read back out of the
response. An unrecognised `bound` value defaults to incoming with a one-shot
warning (`parcels._warn_unmapped_bound`) rather than being dropped. Outgoing
events are deliberately narrower than incoming's — only
`outgoing_parcel_status_changed`/`_delivered`, no `registered`/
`delivery_time_changed` — matching the rest of the suite's account-based
outgoing model. The two outgoing summary sensors'
(`DAOOutgoingParcelsSensor`/`DAOOutgoingDeliveredParcelsSensor`) unique ids
are in `sensor.py`'s stale-entity sweep exclusion set too.

**API mechanics go in your own private research notes, NOT here and not in
a local `docs/api/`** — the endpoint(s) and what keys them, auth flow, rate
limits, response content type, the "unknown code" vs error signalling, the
status vocabulary and its `ParcelStatus` mapping, and the timestamp format.
See CONVENTIONS.md.

## Options and reloads

For code-based carriers, the options flow starts with exactly `Parcels` and
`Settings`. `Parcels` is one editable multi-code list; `Settings` is
a flat form — some carriers use one sectioned form
(`data_entry_flow.section`) instead; both are generator variants, not carrier
decisions. Changes apply without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default) apply changes live: an update listener
  calls `async_request_refresh()`, so added/removed parcel sensors appear
  immediately (this is also the resume path after polling has fully
  suspended — see "Dynamic polling" below).
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+.

## Dynamic polling

There is no user-facing polling interval — this is a deliberate suite-wide
choice, not a gap. `coordinator.py`'s `_hottest_tier_minutes` /
`_next_update_interval` recompute `update_interval` at the end of every
refresh. Full algorithm and reasoning:
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).

- **Quiet window:** no polling 00:00–06:00 local time, except two daily
  anchors (~00:00 and ~06:00) for overnight / end-of-day catch-up.
- **Tiers while polling:** *hot* (15 min) when a tracked, not-yet-delivered
  parcel is `out_for_delivery` within an hour of its `planned_from` (or has no
  `planned_from` at all); *mid* (45 min) for anything else still in flight —
  `problem`/`returning` included, deliberately not hot. Account-based carriers
  never fully stop even with nothing hot or in transit: the mid-tier poll is
  also how a new shipment gets discovered.
- **Full stop (account-less carriers only):** `update_interval = None` when
  nothing is tracked or every tracked parcel is delivered. Resumes the moment
  a parcel is added back, via the options-flow refresh above.
- **Stagger:** a small, stable per-install offset (hash of the config entry
  id) is added to every computed interval so installs don't all hit an anchor
  or tier boundary at the same second.
- **429 backoff:** a 429 anywhere in a poll raises `UpdateFailed` with
  `retry_after` — the carrier's own `Retry-After` header if present, otherwise
  an exponential backoff tracked per-coordinator. `api.py`'s
  `…ApiError.status_code` / `.retry_after` carry this from the HTTP layer.

A carrier that genuinely throttles or soft-bans traffic harder than the 429
backoff handles is a documented, local divergence from this in that one
repo's own `CLAUDE.md` — not a generator flag.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (HTTP client, error types) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (URLs) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (`_STATUS_MAP`, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` | partly (code validation) |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `device.py` (shared device-info helper) | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (`track_parcel` / `untrack_parcel`, account-less only) | no |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Running tests

```
python -m pytest tests/ --cov=custom_components.dao
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in your own private research notes, never in
this repo.
