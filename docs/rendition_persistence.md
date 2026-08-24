# Rendition Persistence

Fixes the gap found while productionizing New Account Enrichment: production's
`parsed_rendition_results` table had zero rows because no code path anywhere
in the app ever wrote to it. The browser held the OCR/extraction result in
memory for the entire run → lock → save sequence and re-POSTed it at each
step; the server returned JSON and (for the final step) a downloaded PDF, but
never persisted anything.

## Where the tables actually came from

`rendition_uploads`, `rendition_jobs`, `parsed_rendition_results`,
`review_notes`, `manual_overrides`, and `exports` are **not created by any
migration tracked in this repo** — they exist in production out-of-band.
Their live shape was confirmed directly via PostgREST's own OpenAPI
introspection (`GET {SUPABASE_URL}/rest/v1/` with
`Accept: application/openapi+json`) on 2026-08-22, not assumed from a
migration file or docstring. `app/rendition_persistence.py` writes only
within that confirmed shape and never attempts to alter it.

Real column list for `parsed_rendition_results` (nullable except `id`,
`created_at`, `updated_at`):
```
id uuid (pk), district_id uuid (fk -> districts.id), created_by uuid,
tax_year integer, upload_id uuid (fk -> rendition_uploads.id),
job_id uuid (fk -> rendition_jobs.id), result jsonb,
assessment_summary jsonb, recommended_value numeric, confidence numeric,
created_at, updated_at
```
`rendition_uploads` and `rendition_jobs` have the analogous upload/job shape
(`file_name`/`status`/`result` etc.) -- see the module docstring for the
full column lists.

## What changed

`POST /api/review/lock` now persists the locked review, and only there --
not on every `/api/review/run` (which would write one row per OCR attempt,
including ones a reviewer immediately discards before locking). At lock
time, the appraiser has already confirmed a final value and a BPP account
number, which is exactly the trustworthy data a durable record should hold.

- `LockReviewRequest` gained an optional `access_token` field. When present
  and valid, it resolves a **server-verified** district via the existing
  `get_authenticated_district_context()` (the same helper every other
  authenticated endpoint uses) and persists under that. The pre-existing
  `district_context` field is never trusted for the write -- it was always
  a client-supplied, unverified dict, and using it for a database write
  would let any caller write into an arbitrary district by editing the
  request body.
- Missing or invalid tokens don't error -- the lock still succeeds exactly
  as before, it just doesn't persist (`response.persisted == false`). This
  keeps the existing endpoint contract unchanged for any other caller.
- A persistence-layer failure is logged to stderr but never blocks the
  appraiser's response.

## Account number semantics

`_extract_metadata`'s `account_number` (matched against labels like
"account number", "acct.", "appraisal district account", expecting a
`P####...`-style value) genuinely is the CAD's own Business Personal
Property account number -- confirmed against the same UI that requires it
before locking ("Enter the appraisal district account / P# before
locking"). This is a **different identifier space** from a real-property
R-account/QuickRefID (Property Enrichment's `real_account_number`), which
uses an `R####`-style convention and comes from an entirely separate
extraction path (`app/arb/arb_analyzer.py`, for ARB protest packets, not BPP
renditions). The two are never compared or stored as though interchangeable
anywhere in this codebase; New Business Detection's HIGH-confidence
corroboration explicitly checks that a property's `real_account_number`
*matches* a BPP `account_number` -- it does not assume they're the same
kind of number, only that the appraiser's own workflow links a specific BPP
account to a specific R-account through Property Enrichment.

The persisted `account_number` is always the **appraiser-confirmed** value
typed at lock time, not the raw OCR guess -- it overwrites
`result.metadata.account_number` before storage, since the confirmed value
is what future matching should trust.

## Dedup / upsert

One row per `(district_id, tax_year, account_number)`. Re-locking the same
account/year updates that row in place (last-locked-wins); a different
`tax_year` for the same account always gets its own row, never overwriting
prior-year history. No unique database constraint exists on this
out-of-band table, so this is an application-level check-then-write --
acceptable given this is a low-concurrency, one-appraiser-at-a-time
workflow. When no tax year can be parsed, every lock creates a new row
(there's no year to dedup against) -- a documented limitation, not a bug.

## Connecting to New Business Detection

No changes were needed to `app/comptroller/matching.py` -- `fetch_candidate_records()`
already reads `parsed_rendition_results` via `result->metadata->>account_number`
and `result->metadata->>owner_name` (its existing default `COMPTROLLER_MATCH_*`
config), and the persisted row's `result` column is the full pipeline
result dict (with the corrected `account_number`), so it matches that shape
exactly. Verified with an integration test that persists a review, reloads
it, and confirms the real `fetch_candidate_records`/`MatchCandidate`/
`match_closure_to_account` path reads it correctly -- not a hand-built
stand-in candidate.

## What this does not do

- Does not persist an address -- `_extract_metadata` never extracted one
  (only `owner_name`/`account_number`/`tax_year`/`signed_date`), so there is
  no address to lose in persistence. This is the same limitation
  `matching.py`'s docstring has documented since the closure-monitor work.
- Does not add authentication to `/api/review/run` or `/api/review/save` --
  those endpoints' existing (lack of) auth is unchanged; only the new write
  path in `/api/review/lock` requires a verified token to activate.
- Does not create, modify, or link any official CAD account, property, or
  appraiser assignment -- this is exactly the same read/advisory boundary
  every other module in this codebase holds.
