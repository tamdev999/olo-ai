# Olo M1 — Core Changes

## Approved exceptions (count: 1)

### README.md

**Approved by:** BBC (repository owner), M1 implementation prompt §2 Case B  
**Justification:** The `import/hermes-base` merge brought in Hermes's full history,
producing a conflict between the Hermes upstream README and the Olo-branded README
already on `main`. The conflict was resolved by keeping the Olo-branded README.
This is the only Hermes core file changed in M1; no source code, configuration,
or tooling file is modified.  
**Content:** The Olo AI project description authored by BBC, as pushed to `main`
in commit `75fa3894c0` and cleaned in `1f0838bafd`.

---

## All other Hermes core files

No other Hermes core files were modified in Milestone 1.

All new Olo source code lives exclusively in:
- `olo/`
- `tests/olo/`
- `docs/olo/`
- `scripts/olo/`
- `.github/workflows/olo-guard.yml`

The core-diff guard (`scripts/olo/check_core_diff.sh`) enforces this. The README.md
exception is the smallest possible change to the allow-list: a single exact filename
with an explicit justification. All other paths outside the Olo trees remain
prohibited, with no approval mechanism.
