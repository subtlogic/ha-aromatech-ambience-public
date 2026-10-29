# Changelog

## 0.6.2

- Refresh the public dashboard screenshot to show the current Bubble popup,
  with illustrative scent and oil values.
- Correct README image placement and descriptions to match the Bubble example.
- Include the refreshed screenshot and documentation in the release examples ZIP.
- No runtime, calibration, storage or device-control changes from 0.6.1.

## 0.6.1

- Start a scent by name using a provisional 4 mg per 5 seconds default; custom
  and learned rates remain supported and existing records are preserved.
- Add 30/60/120 nominal bottle-size presets, default 60, custom oil amounts and
  optional tare. Unknown tare stays explicit and must be supplied before weighing.
- Keep everyday entities visible; technical counters/timings become optional
  diagnostics, disabled by default for new registrations. Preserve existing IDs.
- Simplify the generic Bubble popup and update setup, calibration, timing and
  rollback guidance. Estimates do not claim hardware-measured spray events.
- Extended schedule writes remain experimental. Automated and browser validation
  do not establish physical calibration for every oil or diffuser.

## 0.6.0b5 — prerelease

- Advance estimated cycles, pump-on time and bottle consumption on an independent
  HA clock; Bluetooth reports confirm settings rather than gate counting.
- Persist accounting assumptions and watermark with the bottle ledger; catch up
  downtime once with uncertainty, preserving existing totals and fill identities.
- Reconcile known command boundaries and retain an audit of external mismatches
  and weighing adjustments. Failed commands do not silently change assumptions.
- Run the Bluetooth heartbeat independently of lighting updates; expose health,
  last contact and confirmation age, including when communication is offline.
- Extend the existing Bubble example with consumption and confirmation details.
- Dashboard example uses missing-safe visibility checks so existing timed
  controls remain visible before the Always on switch entity is installed.

## 0.6.0b4 — prerelease

- Replace the one-shot full-day button with an Always on switch. On saves the
  previous schedule before activating 24-hour mode; Off restores it across
  restarts, retaining current intensity and master power.
- Preserve all slots and opaque bytes; additional slots are disabled temporarily
  and restored. Failed writes retain recovery information. External edits are
  not silently overwritten.
- Disable timed-schedule controls while Always on or recovery is pending. Add
  Bubble dashboard toggle, grey read-only saved-schedule view and recovery action.
- Keep experimental opt-in explicit with an actionable message. The switch no
  longer appears offline merely because experimental writes are disabled.

## 0.6.0b3 — prerelease

- Preserve spray-time estimation across light and sound commands; previously a
  lighting heartbeat could invalidate every sampling interval and stall counters.
- Expose per-fill estimated oil consumed, active spray seconds and mg per spraying
  second, with equivalent five-second dose metadata. Existing balances and store
  formats are retained; missing historical usage is not fabricated.
- Add an experimental Set 24-hour schedule button. It uses the app's 08:00–08:00
  full-day encoding, enables all weekdays and retains intensity/master power.
  Conflicting slots are rejected. App intent is established; physical full-day
  behavior still requires validation. Estimation and forecasts use full-day timing.

## 0.6.0b2 — prerelease

- Include a complete Bubble dashboard example: normal Bubble summaries, native
  estimate rows, existing device controls and a separate inventory management popup.
- Bundle the optional management JavaScript resource, entity-mapping instructions
  and 19 focused frontend tests. No dashboard is installed or overwritten by an
  integration update; resource registration and entity mapping remain explicit.
- Integration behavior is unchanged from 0.6.0b1. Multi-slot writes remain
  experimental; the example edits only the first schedule slot.

## 0.6.0b1 — prerelease

- Add per-diffuser scent, bottle and fill inventory with stable identities,
  native options forms and matching automation actions. Rename, swap, store,
  refill, weigh, correct, archive and confirmed deletion preserve the intended
  history boundaries. Install/store require a fresh, manually selected power-off
  state; inventory does not automatically control scenting.
- Keep lifetime counters and alpha estimator storage intact. Attribute only new
  usage to the active fill; stored bottles preserve their balances, including
  across restart. Past counts are not assigned to new inventory.
- Add per-fill rates, conditional weekly consumption forecasts and bounded mass
  ranges for brief observation gaps. Unbounded loss or a range crossing the
  low-oil threshold requires weighing. Gaps never invent counted sprays or
  calibration coverage.
- Add active bottle/scent, fill sprays, remaining percentage, daily grams,
  days remaining and refill status. Persistent notifications default off, support
  acknowledgement and never stop the diffuser.
- Beta remains opt-in. Live HA workflow acceptance and extended schedule hardware
  validation are outstanding; firmware checks and OTA remain deferred.

## 0.6.0a1 — prerelease

- Decode up to five ordered schedules, including minutes and opaque bytes;
  retain existing entity IDs and slot 1 controls.
- Add `set_schedule` and `delete_schedule` actions with full-list readback,
  overlap validation and explicit append rules. Multi-slot/append/delete writes
  require a default-off experimental option and remain hardware-unvalidated.
- Add reported spray/pause timing and conservative estimated usage, with
  observed/unknown coverage and no downtime backfill.
- Add `record_bottle_weight` calibration and estimated remaining grams. Persist
  totals and a valid learned rate; require a fresh weight after restart before
  showing remaining oil again.
- Firmware checks and OTA remain deferred. This alpha is for opt-in testing;
  hardware validation and further weight calibration remain outstanding.

## 0.5.0 — unreleased

First standalone release.

- Domain renamed from `ambience` to `aromatech_ambience`. Installs of the
  earlier private build must remove the old integration and add this one.
- The Bluetooth frame trace moved from a sensor attribute to the diagnostics
  download, so it is no longer written to the recorder on every poll.
- The master switch is named "Scenting" instead of "Diffuser", so it does not
  read as a second strength control beside Intensity.
- Added diagnostics, and brand images: a stylised capsule diffuser icon plus
  light and dark logos. They are our own drawings, deliberately not
  AromaTech's artwork or lettering.

## 0.4.3

- Light: Off is selectable as an effect alongside Warm, Cool and Flow.

## 0.4.2

- Schedule writes retry up to three times, each confirmed against a fresh
  device report, and fail explicitly if the device keeps the old value.

## 0.4.1

- Schedule writes send the four-byte continuation fragment. Without it the
  device ignored intensity and schedule edits.
- A bare turn-on after Off selects Warm instead of Custom black.
- Light replies update state immediately.

## 0.4.0

- A missed advertisement at boot no longer fails setup; entities stay
  unavailable until the first successful poll.

## 0.3.x

- Greet the device with a time sync before any command on a new link.
- Stop writing the device's status bytes back as part of a schedule command.
- Frame trace ring buffer for diagnosing ignored writes.
