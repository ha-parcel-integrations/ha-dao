# DAO Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-dao.svg)](https://github.com/ha-parcel-integrations/ha-dao/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-dao/total.svg)](https://github.com/ha-parcel-integrations/ha-dao/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [DAO](https://send.dao.as) parcels. DAO is a Danish parcel carrier; sign in with your DAO account once and every shipment DAO already links to it — whether added by tracking number or auto-imported via a verified email or phone number — shows up automatically, no per-parcel tracking codes to enter.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

> ### ⚠️ Unverified against a real parcel
>
> This integration's status mapping and field lookups come from static
> analysis of DAO's own app, not from a real tracked parcel — there has been
> no live login yet. The confirmed six-value status vocabulary is mapped;
> whether `at_pickup_point` vs. `delivered` is derived correctly, and every
> `statusCode` value, are still open questions. Both surface with a one-shot
> `WARNING` and a ready-made issue link rather than failing silently — please
> [report it](https://github.com/ha-parcel-integrations/ha-dao/issues/new?template=unrecognised_status.yml)
> if you see one, so the mapping can be confirmed or corrected.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
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

- Automatically discovers every parcel your DAO account tracks — nothing to add by hand
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `at_pickup_point` / `delivered` / …), the carrier's own status text and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, awaiting pickup, recently delivered parcels, plus separate outgoing/outgoing-delivered sensors for parcels you sent
- Read-only **Deliveries** calendar with the expected delivery windows
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed, plus a narrower outgoing pair)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- A DAO account (the same one used by the DAO app or [send.dao.as](https://send.dao.as)), reachable from a desktop browser during setup
- Parcels only show up once DAO's own app or website links them to your account — this integration reads that inbox, it does not manage it

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-dao` as an **Integration**.
3. Install **DAO** and restart Home Assistant.

### Manual

Copy `custom_components/dao` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → DAO**. The flow shows a DAO sign-in link — open it in a **desktop browser** (the DAO mobile app will otherwise swallow the login redirect) and sign in. After signing in, the browser lands on an address the integration can't receive directly; copy that address bar URL back into the Home Assistant flow to finish. See [`docs/finding-the-redirect-url.md`](docs/finding-the-redirect-url.md) if you get stuck.

Home Assistant never sees your DAO password — only a token issued after you've signed in with DAO directly.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule (quiet overnight window, faster when a parcel is out
for delivery, stopped entirely once nothing is left to track) with nothing to
configure. See [CLAUDE.md](CLAUDE.md) for the details.

## Removal

Standard HA removal applies: **Settings → Devices & Services → DAO → ⋮ → Delete**. Nothing is stored on DAO's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.dao_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.dao_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.dao_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.dao_awaiting_pickup` | Parcels currently waiting for collection at a pickup point |
| `sensor.dao_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.dao_outgoing_parcels` | Number of active parcels you sent (DAO's `bound: "OUT"`) |
| `sensor.dao_outgoing_delivered_parcels` | Recently delivered outgoing parcels |
| `sensor.dao_last_successful_update` | Diagnostic: when DAO was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically. Outgoing parcels (ones you sent, not received) never appear on `sensor.dao_incoming_parcels` — they are tracked separately on the two `outgoing` sensors above.

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

## Events

The integration fires these on the event bus (also available as device triggers on the DAO device):

| Event | When |
|---|---|
| `dao_parcel_registered` | A new parcel appears in the active list |
| `dao_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `dao_parcel_delivered` | A parcel is delivered |
| `dao_parcel_delivery_time_changed` | The expected delivery window changes |
| `dao_outgoing_parcel_status_changed` | An outgoing parcel's canonical status changes, except the final hop to delivered |
| `dao_outgoing_parcel_delivered` | An outgoing parcel is delivered |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up. The outgoing pair is deliberately narrower than the incoming one — no `registered`/`delivery_time_changed` equivalent.

## Examples

Ready-to-paste automations live in [`examples/`](examples/).

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.dao: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — DAO has not scanned it yet, or reports a status this integration does not map yet.
- **A status logs "Unrecognised DAO status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-dao/issues/new) with the logged line so the mapping can be extended.
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
