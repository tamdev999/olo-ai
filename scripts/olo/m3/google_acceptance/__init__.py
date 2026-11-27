"""Olo Google live acceptance harness (prepared in the Google readiness batch).

One controlled run against a dedicated Google test account
(docs/olo/m3/GOOGLE_ACCEPTANCE_READINESS.md):

  guard      preflight refusals: the dedicated account, read-only scopes,
             Testing-mode OAuth, a disposable Hermes home, evidence kept
             apart from credentials
  recorder   a request budget and a record of the response *shapes* Google
             returns (types and enum values only; never personal data)
  scenarios  the plan's scenarios (G*, C*, L*, D*, E*) as checks against
             olo.db, each with the manual action that precedes it
  run.py     the operator's command line (preflight, sync, action, check,
             report)

Nothing here writes to Google: Olo's client is GET-only on an allow-list
(olo.google.http) and its scopes are read-only.  Every scenario ends as
passed, failed, not_run (its action was not recorded) or needs_approval
(mode B); none is ever skipped silently.
"""
