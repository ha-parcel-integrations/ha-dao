# Examples

Ready-to-paste Home Assistant snippets for the DAO integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |

How parcels get in depends on which source you set up. With the **account**
source there is nothing to register by hand — whatever the account knows
about shows up automatically. With the keyless **tracking-code** source you
add codes yourself, by hand or with `dao.track_parcel`
([example](automations/track_parcel_from_a_dashboard.yaml)).

All examples assume a single DAO entry. Adjust entity IDs to match yours;
with more than one entry configured, every entity ID carries that entry's
name.

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `dao_parcel_registered` | A new parcel appears in the active list | The full normalised parcel dict |
| `dao_parcel_status_changed` | A parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `dao_parcel_delivered` | A parcel reaches the delivered status | Same (fires *instead of* `status_changed` on that final hop) |
| `dao_parcel_delivery_time_changed` | A parcel's expected delivery time changes | Same, plus `old_planned_from` / `new_planned_from` / `old_planned_to` / `new_planned_to` |

Every payload also carries the account's `device_id`, which is what device
triggers filter on. Events are suppressed on the first refresh after start-up.
