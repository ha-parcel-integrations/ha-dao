# DAO Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-dao.svg)](https://github.com/ha-parcel-integrations/ha-dao/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-dao/total.svg)](https://github.com/ha-parcel-integrations/ha-dao/releases)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 📦 Part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/), a family of package-tracking integrations for Home Assistant that all publish the same parcel format, statuses and events, so they plug into cross-carrier automations and the optional [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator). Browse the other carriers on the website.
>
> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [DAO](https://send.dao.as) parcels. DAO is a Danish parcel carrier, and there are two ways to use this integration — pick one at setup, or run both side by side:

| | **Tracking codes** | **DAO account** |
|---|---|---|
| Needs an account | no | yes |
| Parcels appear | you add the code (by hand or with `dao.track_parcel`) | by themselves, as DAO links them to your account |
| Parcels you sent | not visible | own sensors and events |

The tracking-code source uses DAO's own public "find din pakke" lookup, so it
needs no sign-in at all — just the number from the shipping confirmation.

> ⚠️ **Status mapping is still being confirmed.** DAO's status vocabulary is
> mapped from its confirmed codes, but a handful are mapped on their Danish
> wording alone, and an unseen code is reported as `unknown` rather than
> guessed. Each of those surfaces a one-shot `WARNING` with a ready-made
> issue link rather than failing silently — please
> [report it](https://github.com/ha-parcel-integrations/ha-dao/issues/new?template=unrecognised_status.yml)
> if you see one, so the mapping can be confirmed or corrected.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Actions](#actions)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track parcels **by tracking code without any account**, or sign in and have your DAO account's parcels discovered automatically — or both at once
- `dao.track_parcel` / `dao.untrack_parcel` actions, so a dashboard button or an automation can start following a parcel
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `at_pickup_point` / `delivered` / …), the carrier's own status text and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, awaiting pickup, recently delivered parcels, plus separate outgoing/outgoing-delivered sensors for parcels you sent (account source only)
- Read-only **Deliveries** calendar (empty for now: see the note on delivery windows below)
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed, plus a narrower outgoing pair)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2025.12 or newer
- For the **tracking-code** source: nothing at all — no account, no credentials
- For the **account** source: a DAO account (the same one used by the DAO app or [send.dao.as](https://send.dao.as)), reachable from a desktop browser during setup. Parcels only show up once DAO's own app or website links them to your account — this integration reads that inbox, it does not manage it

## Installation

### HACS (recommended)

1. Open HACS and search for **DAO**.
2. Install it.
3. Restart Home Assistant.

Or click the button below to open it directly in HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-parcel-integrations&repository=ha-dao&category=integration)

### Manual

Copy `custom_components/dao` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=dao)

Setup opens on a choice between the two sources.

### Tracking codes (no account)

Pick **Tracking codes** and the entry is created immediately — there is
nothing to fill in. Add codes afterwards under **Configure → Parcels**, or
call `dao.track_parcel`. One tracking-code entry covers every code; a second
one is not needed and is not allowed.

### DAO account

Add the integration via **Settings → Devices & Services → Add Integration → DAO** and pick **DAO account**. The flow shows a DAO sign-in link — open it in a **desktop browser** (the DAO mobile app will otherwise swallow the login redirect) and sign in. After signing in, the browser lands on an address the integration can't receive directly; copy that address bar URL back into the Home Assistant flow to finish. See [`docs/finding-the-redirect-url.md`](docs/finding-the-redirect-url.md) if you get stuck.

Home Assistant never sees your DAO password — only a token issued after you've signed in with DAO directly.

## Options

Open **Configure** on the integration entry. A **tracking-code** entry opens on
a menu:

| Page | Option | Default | Description |
|---|---|---|---|
| Parcels | Tracking codes | empty | The codes to follow. Edit the whole list at once; changes apply immediately, no restart. |
| Settings | Delivered parcels: filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Settings | Include status history | off | Adds a `history` attribute per parcel with each status update. |

An **account** entry has no code list — its parcels come from DAO — so its
menu holds just the one **Settings** page, with the same two options. Saving
there reloads the entry; on a tracking entry the change applies immediately.

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Actions

Available once the **tracking-code** source is set up (an account-only setup
registers no actions, because it has no code list to add to):

| Action | Field | Description |
|---|---|---|
| `dao.track_parcel` | `tracking_code` | Start following a parcel. Adding a code you already follow does nothing. |
| `dao.untrack_parcel` | `tracking_code` | Stop following a parcel and remove its sensor. |

```yaml
action: dao.track_parcel
data:
  tracking_code: "00370123456789"
```

The code is only checked for being non-empty — it is confirmed real by the
next poll, so a typo shows up as a parcel stuck on `unknown`.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet — in practice this tier doesn't engage for DAO yet:
  nothing in the currently consumed data lets a parcel reach "out for
  delivery" at all).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Account source: never fully stops** — with nothing hot or in transit,
  polling keeps running at the normal cadence, since that's also how a new
  shipment on your account gets discovered.
- **Tracking-code source: stops completely** when you follow no codes, or
  every code you follow is delivered. It resumes the moment you add one.
  A delivered parcel is never looked up again — its history cannot change.
- A small, fixed per-hub offset is added on top, so not every DAO hub out
  there polls at exactly the same second.

## Removal

Standard HA removal applies: **Settings → Devices & Services → DAO → ⋮ → Delete**. Nothing is stored on DAO's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.dao_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.dao_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.dao_next_delivery` | Earliest expected delivery moment across all active parcels — **unavailable until DAO exposes a delivery window** |
| `sensor.dao_en_route_to_pickup_point` | Parcels headed for a daoSHOP but not collectable yet |
| `sensor.dao_awaiting_pickup` | Parcels currently waiting for collection at a pickup point |
| `sensor.dao_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.dao_outgoing_parcels` | Number of active parcels you sent (account source only) |
| `sensor.dao_outgoing_delivered_parcels` | Recently delivered outgoing parcels (account source only) |
| `calendar.dao_deliveries` | Read-only calendar of expected deliveries, no extra API calls — **empty until DAO exposes a delivery window** |
| `sensor.dao_last_successful_update` | Diagnostic: when DAO was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically. Outgoing parcels (ones you sent, not received) never appear on `sensor.dao_incoming_parcels` — they are tracked separately on the two `outgoing` sensors above. DAO's public lookup is recipient-side only, so a tracking-code entry has no outgoing sensors at all.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. DAO's own status vocabulary has no equivalent of "out for delivery", so this integration never reports it:

| Status | Meaning |
|---|---|
| `registered` | Announced / received by DAO |
| `in_transit` | In the sorting network |
| `at_pickup_point` | Waiting for you at a pickup location |
| `delivered` | Delivered |
| `returning` | Going back to the sender |
| `problem` | DAO reports an exception |
| `unknown` | Not yet scanned, or a status we have not mapped yet |

The carrier's own human-readable text is always available as `raw_status`.

### No delivery window, on either source

Neither DAO source reports a bounded delivery window, so `planned_from` and
`planned_to` are always empty. That is a gap in DAO's own data, not something
this integration drops: the account route's `dueDate` is the last day to
collect a parcel from a pickup point, not a delivery time. Three things
therefore stay empty until DAO starts publishing one: the **Deliveries**
calendar, the **Next delivery** sensor, and the
`dao_parcel_delivery_time_changed` event. They are shipped anyway so that a
dashboard or automation written against the rest of the suite keeps working
unchanged if DAO ever does.

## Events

The integration fires these on the event bus (also available as device triggers on the DAO device):

| Event | When |
|---|---|
| `dao_parcel_registered` | A new parcel appears in the active list |
| `dao_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `dao_parcel_delivered` | A parcel is delivered |
| `dao_parcel_delivery_time_changed` | The expected delivery window changes — never fires on DAO, see above |
| `dao_outgoing_parcel_status_changed` | An outgoing parcel's canonical status changes, except the final hop to delivered |
| `dao_outgoing_parcel_delivered` | An outgoing parcel is delivered |

The two outgoing events only ever come from an account entry.

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up. The outgoing pair is deliberately narrower than the incoming one — no `registered`/`delivery_time_changed` equivalent.

## Examples

Ready-to-paste automations live in [`examples/`](examples/).

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/ha-parcel-card](https://github.com/jonisnet/ha-parcel-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.dao: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — DAO has not scanned it yet, or reports a status this integration does not map yet.
- **A status logs "Unrecognised DAO status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-dao/issues/new?template=unrecognised_status.yml) with the logged line so the mapping can be extended.
- **A tracking code never leaves `unknown`** — check it against DAO's own
  [find din pakke](https://dao.as/find-din-pakke/) page. If that page knows
  the parcel and Home Assistant does not, please open an issue.
- **"DAO rate-limited this update; showing the parcels from the previous
  one"** — DAO's public lookup allows only a short burst. Nothing is lost:
  the parcels you already had stay on screen and the next check waits five
  minutes, doubling while it persists. Adding several codes in a row, or
  pressing Refresh repeatedly, is the usual cause.
- **"DAO's bot check rejected the tracking lookup"** — DAO's public lookup
  limits how much traffic it accepts. The integration backs off for an hour
  and doubles from there, up to six hours; nothing needs doing, and following
  fewer codes makes it less likely.
- **Setup asks you to sign in again out of nowhere** — DAO's session eventually expires if Home Assistant has been offline for a long time, or the account's password changed. Re-running setup signs you back in; your parcels and history are unaffected.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by DAO, Home Assistant, or any other third party referenced in this project. Please don't contact DAO for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to DAO's terms. Data is sent only to DAO's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration signs in to your DAO account using the same login DAO's own app and website use, and reads your existing parcel inbox — it never manages, subscribes to or archives shipments. Your DAO password never reaches Home Assistant or this integration; only a token issued by DAO after you sign in directly with DAO is stored, and only on your own Home Assistant instance.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
