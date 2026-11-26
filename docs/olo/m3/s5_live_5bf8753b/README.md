# First live S5 acceptance (5bf8753b), preserved evidence

The run took place on 2026-11-26 from 08:33:43 to 08:34:49 UTC, at
`5bf8753b72a521ab1126946821471b18e8ddc565`. It used real DeepSeek
(`deepseek-flash`, thinking disabled) and a disposable business and bank.
The driver's verdict was **PASS**. The memory-correctness defects it missed
are described in `../S5_LIVE_FINDINGS_REMEDIATION.md`.

| File | What it is | sha256 |
|---|---|---|
| `evidence.json` | the driver's evidence, byte-identical to `/root/s5_acceptance_evidence.json` | `1a8bfc8519c3adf5b1d96bd81db761a1243ae95c6ee17f46d8950974672e0922` |
| `server.redacted.log` | the Hindsight server log as `down.sh` redacted it (original sha256 `ddaf45c46dac0ef991f3209eccfb64ad8185038180c865a606ab05e2ce9ace34`) | `35dd1f0354d95ac29872c7ac5b77aa23a8619e031cb43e7d56175298200d0cc3` |

The committed log differs from `down.sh`'s copy on one line only (line 48).
That line held the disposable embedded PostgreSQL URL with its default local
credentials, which are now masked as `postgresql://***:***@…`, as Hindsight
masks them elsewhere in the log. Neither file contains the DeepSeek key or
the tenant key; both were checked against the real values before copying.

`tests/olo/test_s5_acceptance_checks.py` re-judges this evidence with the
strengthened checks and asserts that they identify the run's shortcomings.
Do not edit these files.
