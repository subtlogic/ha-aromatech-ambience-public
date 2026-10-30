# Validation

Run from the repository root with Python 3.13 and Node.js 22 or newer:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install voluptuous==0.16.0
python -m unittest discover -s tests -v
node --test tests/aromatech-inventory-card.test.mjs
git diff --check
```

The tests use isolated Home Assistant stubs. Installing voluptuous enables the
native-form and service-schema tests; without it, 30 tests are skipped. A complete
run has no skips. CI installs this dependency, runs both suites, and also runs
HACS validation without ignored checks and hassfest.

Tests check encoding, conservative state parsing, schedule updates, inventory
accounting, persistence, entity behavior and the optional inventory card.
Regression fixtures contain protocol bytes required to verify interoperability.
They do not establish universal hardware acceptance. Single-slot operation has
been exercised on one Ambience. Minute-level and multi-slot writes, Always on,
weekday mapping and behavior across firmware revisions still need physical
validation. Experimental controls remain opt-in. Calibrating oil consumption
requires real paired weighings with sufficient recorded coverage.

Local diagnostics can contain recent BLE frames. Downloading diagnostics does
not publish a report; review it before sharing.
