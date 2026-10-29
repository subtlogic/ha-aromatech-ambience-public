# Public distribution work

## D51: Establish a reviewed public distribution snapshot
Agent: Codex | Branch: codex/s10-public-launch | Depends: none | Run-Via: manual | Workspace: local-checkout | Evidence: commands | Crosscheck: required | Risk: dangerous

### Goal
Prepare the current integration and user documentation as a reviewed distribution
snapshot. Keep development history separate from public publication.

### Scope
Export only integration source, regression tests, user documentation, examples,
license and validation workflow. Preserve runtime behavior and existing version.
Do not publish research records or raw capture files. Validate before publication;
initial publication requires attended approval. The destination is
subtlogic/ha-aromatech-ambience-public; existing repositories are unchanged.

### Allowed Files
- `.github/workflows/validate.yml`
- `.gitignore`
- `.loop/audit-baseline.json`
- `.loop/acceptances/.gitkeep`
- `README.md`
- `LICENSE`
- `CHANGELOG.md`
- `STRUCTURE.md`
- `hacs.json`
- `custom_components/**`
- `tests/**`
- `docs/**`
- `examples/**`

### Validation Checklist
```sh
python -m unittest discover -s tests -v
node --test tests/aromatech-inventory-card.test.mjs
git diff --check
```

### Definition of Done
Reviewed file inventory, unchanged executable Python syntax trees, passing
regression suites, and guarded publication with verified remote state.
