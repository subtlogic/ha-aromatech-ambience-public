# Changelog

## 0.6.3

- Set the maintainer to `@subtlogic` and categorize the integration for HACS.
- Simplify public documentation and remove internal development records.
- Run all Python schema tests and the optional dashboard tests in CI.
- Preserve the bundled standard/high-resolution icons and light/dark logos.
- No device-control, inventory-format or calibration changes from 0.6.2.

## 0.6.2

- Refresh the Bubble dashboard screenshot, documentation and examples ZIP.
- No runtime changes from 0.6.1.

## 0.6.1

- Add name-only scent setup with a provisional 4 mg per 5 seconds rate,
  nominal 30/60/120 bottle presets, custom amounts and optional weighing.
- Simplify default entities and the Bubble example while preserving existing
  IDs, rates, balances and history.

## Upgrading from earlier releases

Back up Home Assistant before updating. Version 0.6.1 and later support
unknown bottle tare and default-rate records that 0.6.0b5 and earlier cannot
read. Version 0.6.0b4 and earlier also cannot read the continuous-counter fill
format. Downgrading requires the matching pre-upgrade inventory backup as well
as the older code. Do not delete the integration to resolve a storage error.

Oil amounts and remaining days are estimates. Extended schedules and Always on
remain experimental. See [bottle setup and weighing](docs/BOTTLES.md) and the
[README](README.md#limitations) for current limitations.
