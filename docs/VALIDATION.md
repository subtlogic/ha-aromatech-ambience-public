# Validation

Run from the repository root:

```sh
python -m unittest discover -s tests -v
node --test tests/aromatech-inventory-card.test.mjs
git diff --check
```

Use an environment with Home Assistant's dependencies for the full Python suite;
minimal environments skip tests requiring unavailable dependencies. The GitHub
workflow also runs HACS validation and hassfest.

Tests check encoding, conservative state parsing, schedule updates, inventory
accounting, persistence, entity behavior and the optional inventory card.
Regression fixtures contain protocol bytes required to verify interoperability.
They do not establish universal hardware acceptance. See the
[remaining acceptance work](DEVELOPMENT_PLAN.md).

Local diagnostics can contain recent BLE frames. Downloading diagnostics does
not publish a report; review it before sharing.
