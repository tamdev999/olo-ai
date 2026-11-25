# S4 commitment-extraction corpus — label changelog

The 30-label corpus lives in `tests/olo/test_s4_extraction.py` (`_CORPUS`).
`corpus_label_hash()` is the sha256 of the canonical JSON of every case's
`id`, `kind` and `label` (payloads and mock responses are not included).
Live accuracy results are only comparable between runs that report the same
label version and hash; every live report records both.

| Version | Label hash (sha256) | Introduced |
|---|---|---|
| v1 | `ee3821e92f838bfe7aa065e0f84e79f3c064a5858ea11dd72432a622c8e7099d` | `3e648ac62dd18aa397f1122490f135ac4ff082cd` |
| v2 | `3fa1265d25007e6692f325d864a84ff557624e5497e7275ba0c8967f9670cc49` | cancellation contract commit (this change) |

## v2 — cancellation contract (S4 C1–C4)

Contract (`olo.ledger.cancellation_field_error`, enforced by the ledger and
the extractor): a cancellation carries the cancelled date in
`prior_confirmed_date`; `confirmed_date` and `proposed_date` must be null.
Violations are rejected, never remapped.

| Case | Kind | v1 label | v2 label | Evidence in signal text |
|---|---|---|---|---|
| s07 | email.sent (business) | `confirmed_date=2024-04-10` | `prior_confirmed_date=2024-04-10` | "cancel your meeting BOOK-S07 on 2024-04-10" |
| s13 | email.sent (business) | `confirmed_date=2024-03-20` | `prior_confirmed_date=2024-03-20` | "previously scheduled for 2024-03-20" |
| s06 | email.received (customer) | no date | `prior_confirmed_date=2024-03-15` | "scheduled for 2024-03-15" |
| s09 | email.received (customer) | no date | `prior_confirmed_date=2024-04-10` | "scheduled for 2024-04-10" |
| s19 | email.received (customer) | `business_key=ORD-2024-S19` | `+ prior_confirmed_date=2024-03-18` | "scheduled for 2024-03-18" |
| s20 | email.received (customer) | no date | `prior_confirmed_date=2024-04-01` | "for the 2024-04-01 slot" |

Also in v2:

- `_matches` requires null `confirmed_date` and `proposed_date` on every
  cancellation result; the live scorer reports them as mismatched fields.
- Mock responses for the six cases updated to the contract (s20 keeps its
  deliberate `confidence=0.50`, s17 its unverifiable passage).
- No other case, payload, threshold or the extraction prompt changed.

## Preserved v1 evidence: first live run

The only scored live run so far was made against **v1** labels and is kept
unchanged; it is not rescored under v2.

- Code: `2882157253e9c94fa28ebdbf73c39ed4074d7a8f`, model `deepseek-chat`, temperature 0
- Report: `docs/olo/m3/s4_live_accuracy_2882157253.json`
  (sha256 `2cd625bcd92cdc57ee7a9c858a6c0a6a6a131d691c8912cccfa2b7bc14aee059`; held locally, not committed)
- 30/30 calls, 0 errors, 0 skips, 15,038 tokens, $0.0021 estimated
- TP 18, WRONG 2, FN 0, FP 0, TN 10; precision 0.90, recall 0.90, F1 0.90
- Acceptance: FAIL — s07 and s13 placed the cancelled date in
  `prior_confirmed_date`, which v1 labels scored as wrong. That
  disagreement between labels and prompt is what v2 resolves.
- Because the first run's outputs informed v2, these 30 cases are a
  development set; acceptance evidence for S7 needs a separate holdout.
