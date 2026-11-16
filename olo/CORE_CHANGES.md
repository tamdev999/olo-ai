# Olo M1 — Core Changes

No Hermes core files were modified in Milestone 1.

All new code lives exclusively in:
- `olo/`
- `tests/olo/`
- `docs/olo/`
- `scripts/olo/`
- `.github/workflows/olo-guard.yml`

The core-diff guard (`scripts/olo/check_core_diff.sh`) enforces this with no
exceptions, no allow-list, and no approval file. This document is for human
reference only; the guard does **not** read it.
