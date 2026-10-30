# AromaTech Ambience for Home Assistant

Maintained by [subtlogic](https://github.com/subtlogic).

Local Bluetooth control of the **AromaTech Ambience** scent diffuser. No cloud,
no vendor account. It works through a local Bluetooth adapter or ESPHome
Bluetooth proxies.

## Simple setup and estimated oil tracking

Enter a scent name to start with an approximate **4 mg per 5 seconds of spraying**
rate. Choose a **30 / 60 / 120** bottle-size preset (60 selected by default), or
enter a custom oil amount. We use the selected size as an approximate starting
mass in grams for convenience; a bottle's volume label is not a measured mass.
You do not need a scale to get started. Weighing improves accuracy later.

Estimated usage follows the device's reported spray/pause timings and your schedule,
not the numeric level. The default rate is a rough starting point, not a validated
rate for every fragrance or diffuser. Oil remaining and days remaining are estimates.
See [Bottle setup and calibration](docs/BOTTLES.md#rates-and-weighing).

The default device view focuses on scent, bottle, oil remaining, days remaining,
refill status and connection health. Technical counters and timing sensors are
optional diagnostics, disabled by default for new entity registrations. Existing
entities keep their IDs and enabled/disabled choices; upgrades do not remove them.

See the [changelog](CHANGELOG.md) for release changes.
Take a Home Assistant backup before upgrading. The inventory format retains
accounting checkpoints and now permits bottles with unknown tare. Returning to
b5 or earlier requires the matching pre-upgrade inventory backup if new default-rate scents or unweighed
bottles were created; b4 and earlier cannot read the continuous-counter fill format
at all. Redownloading older code alone is not a safe rollback. Do not delete the
integration to work around a storage error.

Experimental extended schedule writes and Always on remain opt-in and require
further physical validation. Firmware discovery and OTA are not included.

> Not affiliated with or endorsed by AromaTech. "AromaTech" and "Ambience" are
> used only to identify the supported hardware.

<img src="https://raw.githubusercontent.com/subtlogic/ha-aromatech-ambience-public/main/examples/images/dashboard-popup.png" alt="Current AromaTech Ambience Bubble dashboard with estimated oil remaining" width="320">

## Why a separate integration

The community
[`ha-aromatech-scent-diffuser`](https://github.com/ttrushin/ha-aromatech-scent-diffuser)
integration supports other AromaTech models, which use a different Bluetooth
protocol. The Ambience requires its own interoperability implementation.
This repository distributes the integration, user documentation and regression
tests. Research records are maintained separately; this is not a claim of
clean-room development or manufacturer approval.

## What you get

Technical timing/accounting entries below are optional diagnostic entities. Enable
them from Settings → Devices & services → this integration → Entities when needed.
Oil balance, days, scent/bottle, refill status and connection health stay enabled.

| Entity | Type | Notes |
|--------|------|-------|
| Scenting | switch | Master on/off: whether the diffuser scents at all |
| Light | light | RGB colour, brightness, effects Warm / Cool / Flow / Off |
| Sound | switch | The unit's audible feedback |
| Intensity | number | 1–4, edits schedule slot 1 |
| Schedule enabled | switch | Enables schedule slot 1 |
| Schedule 1 Mon … 7 Sun | switch | One per weekday for slot 1; the numbers order weekdays, not slots |
| Schedule from / to | time | Slot 1 start/end, with minute precision |
| Schedule | sensor | Summary and ordered list of up to five schedules in attributes |
| Schedule 1 spray / pause duration | sensor | Device-reported timing for slot 1's intensity, in seconds |
| Estimated sprays / spray time | sensor | Fractional expected cycles and expected active seconds |
| Observed / unobserved estimation time | sensor | Accounted and unknown intervals, in seconds |
| Estimation coverage | sensor | Observed time divided by observed plus unobserved time |
| Estimated oil remaining | sensor | Active inventory balance after inventory use; legacy calibration before it |
| Active bottle / active scent | sensor | Current installation from this diffuser's inventory |
| Fill sprays | sensor | Estimated cycles attributed to the active fill, separate from lifetime totals |
| Remaining percentage | sensor | Estimated current mass relative to the fill's initial mass |
| Daily grams / days remaining | sensor | Conditional forecast from the recurring weekly schedule and reported timings |
| Refill status | sensor | Weighing/calibration needs, refill soon, low or estimated depleted |

Bottle management and forecasts use the local inventory. App-derived multi-slot and minute
handling still require physical acceptance testing.

Schedule edits are confirmed against a fresh report from the device and retried
up to three times. If the device still holds the old value, the action fails
with an error rather than reporting success.

See [examples](examples/README.md) for the dashboard card above, the device
page, and ready-to-paste automation ideas. For scent/bottle controls,
see the [Bubble dashboard and management popup](examples/README.md#bubble-dashboard-and-bottle-management).

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/subtlogic/ha-aromatech-ambience-public`, category
   **Integration**.
2. Install **AromaTech Ambience** and restart Home Assistant.

### Manual

Copy `custom_components/aromatech_ambience` into your
`config/custom_components/` directory and restart Home Assistant.

## Setup

The diffuser is discovered automatically when a Bluetooth adapter or proxy
hears it. You can also add it from **Settings → Devices & services → Add
integration → AromaTech Ambience**.

The Ambience advertises intermittently. If it is not found, wait a minute and
make sure it is in range of an adapter or proxy.

### Options

Open the integration's **Configure** options menu for **Settings** and native
scent/bottle management forms. Inventory changes return to the menu and do not
reload the integration. Names plus ID suffixes distinguish similarly named items.

**Heartbeat interval** (default 300 s, range 30–3600 s). Each poll connects,
sends a time sync, reads the device's full state and disconnects. Shorter
intervals pick up changes made in the vendor app sooner, but use one of the
proxy's connection slots more often. An ESP32 proxy has only three.

**Experimental schedule writes** defaults to off. Enable it only for deliberate
testing: append, delete, and any edit when multiple slots exist are blocked
otherwise. Reading multiple slots does not require this option. Existing scalar
controls always target slot 1 and never create a missing slot.

**Refill notifications** default to off. Optional persistent Home Assistant
notifications use thresholds of 7 days, 5 g low oil and 0 g estimated depletion.
Low-oil threshold must exceed the depleted threshold. Forecast settings apply
without a reload; changing heartbeat or experimental schedule settings reloads
the entry. Alerts never turn scenting off automatically.

### Schedule actions

Actions target a loaded integration using `config_entry_id`:

- `aromatech_ambience.set_schedule`: `slot` is 1–5. Existing slots accept
  optional `start` / `end` (`HH:MM`), `days_mask` (0–127), `enabled`, and
  `intensity` (1–4). Append only at the next position, supplying every field.
- `aromatech_ambience.delete_schedule`: deletes `slot`; later slots move down.

Day masks use bit 0 for Sunday through bit 6 for Saturday; this mapping remains
inferred. Enabling is separate from the day mask. Writes reject end times at or
before the start and overlaps involving the edited slot on enabled shared days.
Adjacent windows are allowed. Untouched slots and their four opaque bytes are
preserved. Success requires a fresh complete list matching the requested result;
a write acknowledgement alone is insufficient.

### Always on (experimental)

The **Always on** switch saves and restores your timed schedule. Enable **Experimental schedule writes** in
integration options before first use. On saves the complete timed schedule to a
separate checked store and activates the app's full-day encoding. Off restores
the saved schedule, retaining current intensity. Master Scenting is unchanged.
Other slots are temporarily disabled and restored, never discarded.

Timed schedule controls are disabled while Always on is active or recovery is
pending. The updated Bubble example shows the saved days/times greyed out and
provides a restore action for unfinished transitions. The schedule backup survives
HA restart; device edits made elsewhere are not silently overwritten. An externally
created full-day mode without a backup must first be changed in the vendor app.
Full-day operation remains physically unvalidated; normal spray/pause cycles continue.

Bottle consumed-mass, spray-time and mg/s sensors retain their existing data. Missing
historical consumption is not reconstructed. Updating the integration does not activate
Always on or change scenting settings, and does not install dashboard YAML automatically.

### Usage estimates and weighing

Estimated sprays are fractional expected cycles from schedules and the reported
spray/pause durations, not counted physical events. A Home Assistant timer advances
usage from the last known settings, in Home Assistant's time zone. Bluetooth
reports confirm those assumptions; missed reports do not freeze the counter.
Power off and recognized inactive schedule windows contribute zero cycles.
Ambiguous or unsupported windows remain uncertain and overlaps are never counted
twice. Saved accounting time and assumptions permit one-time restart catch-up,
marked uncertain. Legacy stores without those assumptions start prospectively;
missing historical usage is not invented. Intensity 5 can be decoded and estimated when
the device supplies its timing, but new intensity writes remain limited to 1–4.

Use **Add scent**, **Add bottle**, then **Install bottle** in the integration
options. Before installing or storing a bottle, turn **Scenting off** yourself;
the operation requires a fresh power-off observation. Inventory actions do not
automatically change the diffuser's power. Weigh close to submitting **Weigh
bottle**. See [Bottle management](docs/BOTTLES.md) for all forms, history,
calibration, corrections and uncertainty.

Lifetime counters continue across swaps. Each fill receives only newly estimated
usage while its bottle is installed. A stored partially used bottle retains its
balance. Renaming preserves identity and history; refilling creates a new fill.
The counter and bottle ledger share a checked atomic checkpoint. Existing alpha
totals are preserved on migration and are not assigned to a newly created bottle.

Bluetooth health, Last Bluetooth contact and Bluetooth confirmation age expose
whether current settings are confirmed. The heartbeat runs independently of light
commands and accounting. Stale/offline describes communication, not diffuser power.
An unexpected external settings change records an uncertain boundary rather than
inventing its timestamp; known command boundaries can produce auditable adjustments.

New scents use a provisional 0.004 g per 5 seconds unless both custom rate fields
are supplied. Existing scents and fills are not repriced on upgrade. A scent may
also have a rate learned from covered weighings. Each fill retains its own
rate; editing the scent default affects future fills. Reusing a scent rate and
scaling it across intensity levels are assumptions.

Brief gaps can retain a bounded remaining-mass range: possible unobserved loss
is at most unknown seconds multiplied by the fill's grams-per-active-second
rate. These gaps continue estimated usage from usable assumptions, but do not
count as precise calibration coverage. Weighing is needed when loss cannot be bounded or the lower bound
crosses the low-oil threshold. Restart preserves stored bottle balances; active
bottle downtime contributes uncertainty. No mass uncertainty or refill alert
automatically stops scenting.

Daily consumption uses a seven-day average of enabled windows and current
reported spray/pause timings. Days remaining assumes that schedule continues;
it is not an exact depletion date. Missing rates/timings, unsupported windows,
or unresolved mass uncertainty can make numerical forecasts unavailable. Zero
consumption does not produce an infinite days-remaining value.

The alpha `record_bottle_weight` action remains available for legacy calibration.
Use the inventory forms/actions for a named bottle; alpha history is not silently
converted into inventory history.

## Requirements

- Home Assistant 2026.3 or newer
- A Bluetooth adapter or an ESPHome Bluetooth proxy in range of the diffuser

## Troubleshooting

**Download diagnostics** from the device page when investigating an issue. Review
the file before sharing it. It includes the last 24 Bluetooth frames sent and received, which is
usually what it takes to tell an ignored write from one that was never sent.
The device address is redacted.

Device controls show as unavailable until the first successful poll. Restored
usage totals remain readable while Bluetooth is unavailable. A miss at boot is
normal; the integration retries on the next heartbeat.

## Limitations

- Tested on one Ambience unit. Other AromaTech models are not supported.
- Multi-slot, append/delete and minute-level behavior have offline test
  coverage but remain pending physical validation.
- The weekday bit order (bit 0 = Sunday) is inferred. Please open an issue if
  the day switches do not match the vendor app.

Firmware availability checks and OTA installation are not supported.

Ideas and reports are welcome in
[issues](https://github.com/subtlogic/ha-aromatech-ambience-public/issues).

## Development

See [validation](docs/VALIDATION.md) for setup and test commands.

Original diffuser artwork, including high-resolution icons and light/dark logos,
is bundled in `custom_components/aromatech_ambience/brand/`.

## License

MIT
