# Bottle management

Bottle inventory belongs to one diffuser's integration entry. A scent describes
the oil; a bottle is the physical container with its own tare; a fill is one
period of oil in that bottle. Their IDs remain stable when names change. Duplicate
names are allowed: form selectors include an ID suffix.

## First setup and everyday use

1. Open **Settings → Devices & services → AromaTech Ambience → Configure**.
2. Choose **Add scent** and enter its name. The default estimate is **4 mg per
   5 seconds of spraying**. Supply both custom rate fields only when you have a
   better estimate; a rename never resets an existing rate.
3. Choose **Add bottle**, select the scent and enter a name. Select a nominal
   size **30 / 60 / 120** (default **60**) or a custom oil amount. The preset number
   is used as an approximate mass in grams, even if the product is sold by volume.
   Leave empty-bottle weight blank if unknown. For an accurate starting balance,
   enter the actual empty-bottle weight and a gross bottle-plus-oil weight instead.
4. Turn **Scenting off** manually before physically installing or swapping a
   bottle. Choose **Install bottle** for the bottle now installed. Install and
   store operations require a fresh device report confirming power off.
5. Turn scenting back on yourself when ready. The integration does not do this
   as part of inventory management.

Metadata changes do not send Bluetooth commands. Install/store checks obtain a
fresh observation; they do not switch power off for you. Inventory operations
must save successfully before reporting success. A persistence error stays on
the form rather than claiming the change was saved.

## Forms and history

| Form | Effect |
|------|--------|
| Add / Edit scent | Create a scent or change its name, brand, notes or default rate. Renaming preserves history. |
| Delete scent | Confirm permanent deletion. A scent referenced by bottle/fill history cannot be deleted. |
| Add / Edit bottle | Create a physical bottle or change its name, notes and tare. Changing tare without a fresh gross weight makes mass unknown. |
| Install bottle | Make a non-archived bottle active after the power-off check. A stored partial bottle resumes with its retained balance. |
| Store active bottle | Remove the active installation after the power-off check. Stored bottles receive no diffuser usage. |
| Weigh bottle | Record current gross weight against the bottle's tare and establish a current mass checkpoint. |
| Correct a measurement | Record current corrected gross weight and a reason. Keep the old measurement; do not rewrite historical usage or learn a rate across the correction. |
| Refill or change scent | Append a new fill to a stored bottle. Select the scent and supply oil mass or gross weight. Earlier fills remain in history. |
| Archive or restore bottle | Hide a bottle from normal use without deleting history; clear Archived to restore it. An active bottle cannot be archived. |
| Permanently delete bottle | Explicitly confirm deletion of the selected bottle and all its fills/weighings. An active bottle cannot be deleted. |
| Acknowledge refill alert | Persist acknowledgement for the current alert. This does not add oil, change counts or stop scenting. |

Store the active bottle before refilling, archiving or deleting it. Archived
bottles must be restored before installation. Enter only the fields you intend
to change on an edit form. Inventory mutations return to the options menu and
preserve integration settings without a reload.

## Lifetime totals versus fill usage

Existing estimated sprays and active-time counters remain lifetime totals. They
never reset on swap or rename. Fill counters receive elapsed-time usage estimates
while that bottle and fill are active. Repeated reads and restores
do not allocate the same usage again. Swapping A → B → A preserves A's existing
fill counts and balance.

The checked inventory storage envelope also holds the authoritative accounting
timestamp, totals and assumptions, so a crash cannot save the bottle balance
without its matching usage watermark. Alpha estimator totals remain preserved
and unassigned during migration. Creating inventory does not
retroactively assign those totals to a bottle, and old Home Assistant history is
not imported. After the first successful bottle installation, the existing estimated-oil sensor follows
inventory; before that it retains legacy behavior. Removing the integration entry
removes both stores, so take a backup if retaining that history matters.

The continuous-counter migration preserves existing IDs, totals and measurements,
and adds the accounting state and reversible balance fields. b4 and earlier cannot read the continuous-counter fill shape; b5 cannot read
new 0.6.1 default-rate provenance or unknown-tare bottles. A rollback therefore needs the matching
pre-upgrade Home Assistant backup as well as the older integration code.

## Rates and weighing

New scents default to **0.004 g / 5 seconds = 0.0008 g per active second**.
This is a rounded, provisional starting estimate based on limited observations,
not a universal physical constant. Oil viscosity, device variation, residue in
tubing, evaporation and handling loss can change actual use. Existing scent/fill
rates are preserved; changing a name does not apply the default again.

A provisional scent rate is grams per spray divided by reference spray seconds,
yielding grams per active spray second. A fill is seeded with the scent's rate
when created. That fill keeps its rate if the scent default is edited; future
fills use the new default. Reusing the rate for the same scent and scaling across
intensities are assumptions, not laboratory measurements.

Two weight checkpoints can establish a learned rate only with positive active
time, positive measured loss and no intervening unknown interval. Learning does
not bridge fills, swaps or corrected measurements. A new rate changes future
estimated usage, not already recorded consumption. A first weight establishes
mass; it does not alone establish a consumption rate.

If tare is unknown, first use **Edit bottle** to record the actual empty-bottle
weight. Never enter zero to mean unknown; a real zero tare is a different value.
Changing tare requires a fresh weighing before remaining mass is trusted. A nominal
size does not establish a calibration checkpoint. Two eligible real weighings
are needed to learn a rate.

Weigh close to submitting the form. Gross and tare must be finite, nonnegative,
within supported limits and physically consistent. A correction retains the old
measurement and reason and anchors current remaining mass; it does not establish
a calibration baseline. Changing tare
without a fresh weight removes confidence in remaining mass until weighed again.

## Continuous accounting and Bluetooth confirmation

HA advances usage every 30 seconds from the last known scenting state, active
schedule and spray/pause timings. It settles elapsed usage before settings or
bottle boundaries. Estimated cycles = eligible elapsed seconds / (spray + pause);
pump-on seconds = estimated cycles × spray duration. A missing Bluetooth report
does not pause that clock. Confirmed off periods and recognized inactive schedule
windows add zero cycles. Ambiguous/unsupported schedules remain uncertain.

The configurable Bluetooth heartbeat runs independently, and complete incidental
reports also confirm settings. Bluetooth health is healthy while contact is within
one heartbeat interval plus reply timeout with no failed check, stale after that
or one/two failures, and offline after three intervals or three failures. No
confirmation is offline. Health sensors remain available even when BLE is offline;
offline never implies the diffuser has stopped.

HA commands are pending until readback confirms success. Known successful command
boundaries may reconcile the interim estimate; failed commands do not silently
install their requested settings. External mismatches record an uncertainty window
because their exact onset is unknown. Adjustments remain auditable and distinct
from accumulated usage. The estimator retains its most recent 100 adjustment
records and the bottle ledger its most recent 500 records; cumulative totals
survive that log retention limit. Weighings correct remaining mass; they are not evidence of
an exact physical spray count.

## Uncertainty and remaining mass

Counters remain estimates of schedule-based cycles, not measured spray events.
Unconfirmed intervals continue to be estimated from saved assumptions, with
uncertainty tracked separately. They are not treated as precise calibration
evidence. Unsupported timing inputs cannot produce a reliable cycle estimate.
For a brief gap with a usable per-fill rate, possible extra consumption is bounded
by:

```text
maximum unobserved loss = unknown seconds × fill grams per active second
```

This uses continuous spraying as a conservative uncertainty bound. The
remaining-mass sensor exposes a numerical estimate only while the mass confidence
rules permit it, with range attributes `remaining_lower_g` and `remaining_upper_g`.
If uncertainty crosses the low-oil threshold, mass, percentage and days can show
Unknown even while a working balance is retained internally. A fresh weighing
restores the mass anchor; it does not necessarily learn a new rate.
Uncertainty covers the possibility of both less and more use than estimated. If the
loss cannot be bounded, or only the lower bound reaches the configured low-oil
threshold, the bottle needs weighing. If the working estimate is already low or
estimated depleted, that status remains visible despite bounded uncertainty. A brief bounded gap need not discard all
useful mass information.

Restart preserves stored bottle balances. The active bottle's elapsed downtime is
estimated once from a saved timestamp and usable assumptions, with uncertainty;
the integration does not claim to have observed that operation. Legacy storage
without an accounting anchor cannot reconstruct missed history. A fresh weighing
restores a current mass anchor. Unknown mass never automatically turns scenting off.

## Forecasts and alerts

Daily grams uses a seven-day average of enabled weekly schedule windows and the
device's current spray/pause timings. Missing timings or rates, unsupported or
overlapping windows, and unresolved mass uncertainty can prevent a numerical
forecast. Zero predicted consumption does not produce infinite days remaining.
Days remaining is conditional on continuing that recurring schedule, not a
promise of an exact depletion date.

Refill states are `no_bottle`, `needs_weighing`, `needs_calibration`, `ok`,
`refill_soon`, `low`, `estimated_depleted` and `forecast_unavailable`. Missing
forecast inputs do not count as recovery or erase an acknowledged alert.
The default thresholds are 7 days
for refill soon, 5 g for low oil and 0 g for estimated depletion. Low-oil threshold
must be strictly greater than the depleted threshold.

Persistent Home Assistant notifications are optional and default off. Repeated
polls do not repeatedly create the same fill/status alert. Acknowledgement is
saved; recovery, a different fill or increased severity can rearm alerts. A new
weighing-needed condition also prompts once rather than clearing a low-oil alert.
Resolving a weighing prompt can produce a fresh refill alert. Genuine severity
downgrades dismiss the alert and stay dismissed after restart; unacknowledged
notifications are restored after HA restart. Alerts do
not send messages to an external destination and never stop the diffuser.

Heartbeat and experimental schedule settings still work. Those settings reload
the integration; forecast/notification settings apply without a reload. Extended
schedule writes remain default-off and hardware-unvalidated.

## Scope and verification

Inventory and forecasts are local estimates, not a hardware oil sensor. Native
form schemas, persistence and accounting have automated coverage with HA stubs.
See [validation](VALIDATION.md) for the current candidate and outstanding physical
checks. Firmware discovery and OTA are outside this release.
# Spray-time accounting

Oil use is accumulated from estimated active pump seconds, independent of the
numeric intensity label. For each elapsed interval within a known
schedule, cycles = enabled seconds / (spray seconds + pause seconds), and active
seconds = cycles × spray seconds. Consumption = active seconds × the fill's
grams-per-active-second rate. The same rate across burst lengths is a calibration
assumption, not proof of constant physical pump output.

Bottle estimated oil consumed and Bottle estimated spray time expose the existing
persisted fill totals. Estimated oil per spraying second uses mg/s; attributes
also expose the equivalent five-second dose in mg and rate provenance. HA Recorder
can retain these sensor values alongside level and timing history. Five-second
units and fractional cycles are estimates, not hardware-confirmed spray events.
Level changes affect future intervals, not previously priced consumption. Weighing
updates the remaining mass; it does not relabel measured loss as counted sprays.
Bluetooth gaps continue the estimate with uncertainty, and missing pre-migration
historical usage is not backfilled. Light/sound commands do not discard scenting
accounting or postpone the independent heartbeat.


## Timing and everyday estimates

Use the durations reported by your diffuser, not its level number as a multiplier.
Example device-reported timings give these estimates at the rounded default rate:

| Level | Spray | Pause | Oil per estimated spray | Oil per 12-hour day |
|---|---:|---:|---:|---:|
| 1 | 5 s | 300 s | 4 mg | 0.57 g |
| 2 | 10 s | 240 s | 8 mg | 1.38 g |
| 3 | 10 s | 120 s | 8 mg | 2.66 g |
| 4 | 15 s | 90 s | 12 mg | 4.94 g |

These are modeled averages, including fractional cycles, not counted bursts.
The integration uses the device's actual reported timings if they differ.

```text
cycles = scheduled enabled seconds / (spray seconds + pause seconds)
pump-on seconds = cycles × spray seconds
consumed grams = pump-on seconds × grams per active second
days remaining = remaining grams / average daily grams
```

For example, an approximate 60 g fill at level 2 for 12 hours every day gives
about 43 days at the default rate. A shorter schedule changes the forecast; off
periods add no use. The forecast uses a seven-day schedule average. A fresh weight
updates the balance; only eligible fully covered weighing intervals refine the rate.

## Default and advanced entities

The everyday sensors are installed scent/bottle, estimated oil grams/percentage,
estimated days remaining, refill status and Bluetooth health. Schedule and device
controls remain available. Timing, lifetime accounting, fill usage/rate, daily-use
and Bluetooth timestamp/age sensors are diagnostic and disabled by default for
new installations. Enable those you need on the integration's entity list.
Existing registrations retain their choices and stable IDs. Raw frames and internal
accounting details belong in **Download diagnostics** and optional diagnostic
sensors, not the ordinary view.
