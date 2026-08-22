# Texas Comptroller Sales-Tax Permit Closure Monitoring

Watches Texas Comptroller sales-tax permit data for Lubbock County locations
that go inactive, and turns each newly-detected closure into a reviewable
signal for appraisers -- **never** an automatic change to appraisal, BPP,
ownership, or exemption data.

**A closed sales-tax permit is a possible business/location closure signal,
not proof the business stopped operating.** It can also mean: the location
moved, ownership changed, the permit was replaced, or an administrative
reorganization happened. Every review item is labeled accordingly (e.g.
"Possible Business Closure / Inactive Sales Tax Location — Review Required")
and the workflow statuses include `Not Closed / False Positive`,
`Ownership Change`, and `Relocated` alongside `Confirmed Closure`.

## Official data source

**Texas Open Data Portal (Socrata) dataset "All Permitted Sales Tax Locations
and Local Sales Tax Responsibility"**, published by the Comptroller:

- Dataset page: https://data.texas.gov/Government-and-Taxes/All-Permitted-Sales-Tax-Locations-and-Local-Sales-/3kx8-uryv
- API (SODA): `https://data.texas.gov/resource/3kx8-uryv.json`
- No API key required. An optional Socrata app token can be set via
  `COMPTROLLER_APP_TOKEN` (sent as `X-App-Token`) to raise the
  unauthenticated per-IP rate limit; not required at our request volume
  (one fetch of ~15k rows/day).

**Why this dataset and not the others considered:**

- The Comptroller's "Taxpayer Search" page and its underlying "Sales
  Taxpayer (STP)" API (api-doc.comptroller.texas.gov) are designed for
  looking up one taxpayer/location at a time, not for pulling a whole
  county's universe daily.
- The "Active Sales Tax Permit Holders" dataset
  (`data.texas.gov/.../jrea-zgmq`) only lists *currently active* permits.
  Detecting a closure from it would mean inferring one from a record's
  *absence* between two daily pulls -- which the product requirement
  explicitly forbids ("missing from one fetch is not itself sufficient
  evidence of closure"), since a transient fetch problem would look
  identical to a closure.
- "All Permitted Sales Tax Locations..." instead lists every location
  active in the last four years **plus an explicit `out_of_business_date`
  column**, so a closure is a positive signal (a populated date), not an
  absence. This is the dataset used.

### Field mapping

The dataset has no separate ACTIVE/INACTIVE column and no separate "permit
end date" column -- `out_of_business_date` is the single field that encodes
both, mapped in `app/comptroller/client.py` as:

| Dataset field | Meaning here |
|---|---|
| `tp_number` + `loc_number` | Durable composite key (`taxpayer_id` + `location_number`). Verified unique on the live dataset. |
| `tp_name` | `legal_name` |
| `loc_name` | `location_name` (DBA / location name) |
| `address_text`, `loc_city`, `loc_state`, `loc_zip` | Location address |
| `loc_county` | County (see below) |
| `permit_date` | `permit_start_date` |
| `out_of_business_date` is null | `current_status = ACTIVE`, `permit_end_date = null` |
| `out_of_business_date` is not null | `current_status = INACTIVE`, `permit_end_date = out_of_business_date` |

Because status is a pure function of `out_of_business_date` in this dataset,
a real-world closure always arrives as **one** combined
`STATUS_AND_END_DATE_CHANGE` event, never as two separate events on the same
day -- the "don't double-count a simultaneous status flip + end date" rule is
satisfied structurally, not just by application logic (though the logic in
`app/comptroller/service.py::detect_change` also handles a status flip and an
end-date addition arriving as genuinely separate changes, in case a future
data source reports them independently).

## Geographic scope: how "Lubbock County" is determined

`loc_county` is **not** a county name -- it's Texas's statewide county
number (the same number used as the prefix of local sales tax jurisdiction
codes, e.g. the City of Lubbock's code is `152-104-03`). This is the
*location's* county, populated independently of how any mailing address is
formatted, which is why it's used instead of a city-name filter. Verified
empirically against live data on 2026-08-18: of 14,013 rows with
`loc_city = LUBBOCK`, 13,958 (99.6%) carry `loc_county = 152`; the remainder
are Lubbock-adjacent addresses actually inside neighboring counties (110,
153, 191) -- exactly the kind of city-name false-positive/negative a naive
`city == "Lubbock"` filter would get wrong in both directions. Cross-checked
against other well-known codes present in the same data (Bexar=15,
Dallas=57, Travis=227) to confirm this is Texas's standard alphabetical
county numbering.

County configuration lives in `app/comptroller/counties.py`:

```python
TEXAS_COUNTY_CODES = {
    "Lubbock": "152",
}
```

`COMPTROLLER_MONITORED_COUNTIES` (comma-separated county names, default
`Lubbock`) controls which counties actually get synced. **Adding a new Texas
county is a one-line code change plus a config change, not a rewrite:** add
`"CountyName": "code"` to `TEXAS_COUNTY_CODES` (see the docstring there for
how to verify a county's code), then add the name to
`COMPTROLLER_MONITORED_COUNTIES`.

## Data model

New tables, added in
`supabase/migrations/20260818_comptroller_closure_monitor.sql` (idempotent,
follows this repo's existing migration conventions -- `gen_random_uuid()`
PKs, `set_updated_at()` triggers, `add column if not exists`):

- **`comptroller_permit_locations`** -- current state, one row per
  `(taxpayer_id, location_number)`. Carries `is_baseline`, `first_seen_at`,
  `last_checked_at`, `last_changed_at`, and the raw source row (`source_row_raw`)
  for traceability.
- **`comptroller_permit_status_events`** -- append-only history. One row per
  meaningful change (`change_type` in `BASELINE`, `STATUS_CHANGE`,
  `PERMIT_END_DATE_ADDED`, `STATUS_AND_END_DATE_CHANGE`, `REOPENED`, `OTHER`),
  with `previous_status`/`new_status`, `previous_permit_end_date`/
  `new_permit_end_date`, `detected_at`, `source_data_date`,
  `month_end_processed_at`, and `review_item_id`. This table answers "when did
  RenditionPilot first detect this permit became inactive?" indefinitely,
  including for baseline rows.
- **`comptroller_sync_runs`** -- one row per sync attempt (baseline, daily,
  month-end, manual) for admin observability: counts checked/new/newly-inactive,
  status, error message, source data date.
- **`comptroller_closure_reviews`** -- the month-end review queue. One row per
  status event selected for review (unique on `status_event_id`, so
  reprocessing never duplicates a review item), carrying both the Comptroller
  facts and the match result. `match_ambiguous` (added by the follow-up
  migration below) flags when a second RenditionPilot record scored
  similarly to the chosen one.

`supabase/migrations/20260821_comptroller_match_schema_correction.sql`
(additive only) adds the `match_ambiguous` column above -- a follow-up after
live-schema validation found the original `accounts`-based design didn't
match reality (see "Account matching" below).

**As of 2026-08-21, none of these migrations have been run against the live
Supabase project yet** (verified: `GET /rest/v1/comptroller_permit_locations`
etc. all return 404). Run both files in the Supabase SQL editor before
enabling the cron jobs or any manual sync/month-end command -- every one of
them will fail immediately otherwise.

No table in this repo defines the `accounts`/`renditions` schema RenditionPilot
already uses for appraisal accounts -- see "Account matching caveat" below.

## Baseline behavior

The very first sync for a county (detected as "zero existing
`comptroller_permit_locations` rows for this county", not a separate flag)
imports the entire current permit universe with `is_baseline = true` and
records one `BASELINE` status event per row -- **not** a closure event.
`change_type = 'BASELINE'` rows are excluded from month-end processing by
type, so the ~5,200 Lubbock County permits that are already inactive on day
one never generate a false closure. Re-running `baseline` for a county that
already has rows on file is refused (`app/comptroller/cli.py::cmd_baseline`)
so re-baselining is always an explicit, intentional operation.

**Run for real against production on 2026-08-21**: 15,497 Lubbock County
permit locations imported, all `is_baseline = true`, zero non-`BASELINE`
events -- confirmed correct against the live database, not just the mocked
test suite. This live run also caught and fixed three real bugs the mocked
tests couldn't see (the test double didn't enforce a constraint the real
database does):

1. **The crash-safety dedup guard treated a permit's own `BASELINE` event as
   "already recorded."** Any permit that was already `INACTIVE` at baseline
   time has a `BASELINE` event whose `new_status`/`new_permit_end_date`
   equal its real current state by definition -- `has_unprocessed_duplicate_event`
   didn't exclude `change_type = 'BASELINE'`, so a later re-detection landing
   back on that same state was silently swallowed as a false "duplicate"
   instead of recorded. Fixed by adding `change_type: not.in.(BASELINE)` to
   that query.
2. **A bulk upsert with mixed new/changed/unchanged rows failed outright**
   with PostgREST's "All object keys must match" -- `first_seen_at`,
   `is_baseline`, and (my own fix for bug 3) `last_changed_at` were only
   conditionally included per row, producing objects with different key sets
   in the same batch. Fixed by always including every key in every payload,
   preserving (rather than blanking) the existing value for rows where it
   shouldn't change this sync -- see `_build_location_payload`'s docstring.
   This was a **latent, pre-existing bug** independent of anything from this
   validation pass: any daily sync that ever saw even one new permit
   alongside existing ones would have hit it.
3. **`last_changed_at` was never actually populated anywhere**, despite
   being in the schema and named in this doc's own table description. Fixed
   in the same change as #2.
4. **`python -m app.comptroller.cli ...` never loaded `.env`** -- only
   `backend/main.py` did. Fixed by loading it the same way at the top of
   `cli.py`; a no-op on platforms (like Render) that inject env vars
   directly into the process.

All four are covered by new regression tests in `tests/test_comptroller_service.py`
and a general "PostgREST rejects heterogeneous-key batches" check added to
the test double itself (`tests/comptroller_fakes.py`), so this class of bug
fails in the test suite going forward instead of needing another live run to
surface it.

A genuine transient TLS error (`SSLV3_ALERT_BAD_RECORD_MAC`) also occurred
twice mid-upsert during this validation, unrelated to any code bug -- both
times, `comptroller_sync_runs` correctly logged a `FAILED` row with the real
error message and `comptroller_permit_locations` was left completely
untouched, then a plain retry succeeded. This is exactly the resiliency
behavior described in "Daily sync behavior" below, confirmed against a real
failure rather than a simulated one.

## Daily sync behavior

`app/comptroller/service.py::sync_county()`:

1. Fetches the county's current permit universe.
2. Guards against a partial/failed download: if this isn't the baseline run
   and the fetch returns fewer than `COMPTROLLER_MIN_EXPECTED_ROW_RATIO`
   (default 50%) of the last *successful* run's row count, the sync aborts
   as `FAILED` **without writing anything** to `comptroller_permit_locations`
   or `comptroller_permit_status_events`. An outage never makes existing
   permits look closed.
3. Upserts every fetched row's descriptive fields + status + end date
   (`on_conflict=taxpayer_id,location_number`, chunked bulk upserts).
4. Diffs each existing row's prior `current_status`/`permit_end_date`
   against the freshly fetched value (`detect_change()`) and records exactly
   one event per meaningful change:
   - `ACTIVE -> INACTIVE` alone -> `STATUS_CHANGE`
   - end date added while status field is unchanged -> `PERMIT_END_DATE_ADDED`
   - both at once -> `STATUS_AND_END_DATE_CHANGE` (one event, not two)
   - `INACTIVE -> ACTIVE` -> `REOPENED` (tracked for audit, not counted as a
     closure)
   - end date changed to a different non-null date -> `OTHER`
5. A brand-new `(taxpayer_id, location_number)` on a non-baseline run gets
   `first_seen_at` set and counts toward `permits_new`, but gets **no**
   status event and does not enter the review queue -- V1 is scoped to
   closures, per spec.
6. Every run (success or failure) is logged to `comptroller_sync_runs`.

Re-running `sync` any number of times for the same day is safe: a location
already in its current state produces no new event.

## Month-end behavior

`app/comptroller/month_end.py::process_month_end(target_month, dry_run=False)`:

- Selects every `comptroller_permit_status_events` row with
  `change_type != 'BASELINE'` and `month_end_processed_at IS NULL`, whose
  `detected_at` falls in `[first-of-month, first-of-next-month)`.
- Month boundaries are computed with `calendar.monthrange()` + plain date
  arithmetic (`month_bounds()`), so 28/29/30/31-day months and the
  December -> January rollover need no special-casing.
- For each event: runs account matching, writes one
  `comptroller_closure_reviews` row, and marks the event
  `month_end_processed_at` (+ `review_item_id`). The unique index on
  `comptroller_closure_reviews.status_event_id` (with
  `Prefer: resolution=ignore-duplicates`) means even a crash between "create
  review" and "mark event processed" can't produce a duplicate review on
  retry.
- `resolve_target_month(None)` (no `--month` argument) always resolves to
  **the calendar month before today** -- so running the job once on the 1st
  of a month correctly closes out the previous month regardless of its
  length. See "Scheduler setup" below for why this, rather than
  `day == 31`-style logic, is the scheduling contract.
- `--dry-run` computes and reports match results without writing anything.

## Account matching

**Updated 2026-08-21 after live-schema validation.** The original version of
this feature assumed an `accounts` table would exist (per the migration
comment below) and matched on address + ZIP + city + name. That table does
not exist. Verified directly against the live production Supabase project:

- `GET {SUPABASE_URL}/rest/v1/` (PostgREST's schema listing) shows **no**
  `accounts`, `renditions`, `rendition_accounts`, `rendition_reviews`,
  `completed_reviews`, `review_queue`, or `saved_renditions` table --
  despite `supabase/migrations/20260429_multi_district_renditions.sql`
  assuming `accounts` already exists and only ever `ALTER`ing it if present.
  The real schema is `rendition_uploads` -> `rendition_jobs` ->
  `parsed_rendition_results` (+ `review_notes`, `manual_overrides`, `exports`).
- **None of those tables carry a situs address, city, ZIP, or DBA name.**
  Confirmed against `app/pipeline.py`'s `_extract_metadata()` (~line 3030),
  the only code anywhere that extracts business-identity data from a
  rendition PDF: it only ever produces `owner_name`, `account_number`,
  `tax_year`, `signed_date`, nested under a `"metadata"` key in the
  pipeline's result dict (`app/pipeline.py` lines ~3203/3289).
- **All three tables currently have zero rows**, and nothing in
  `backend/main.py` or `app/*.py` writes to any of them today -- the
  deployed review flow returns results to the browser and (Streamlit-only,
  not the deployed FastAPI path) writes local files, never Supabase. See
  "Known limitations" below; this is a pre-existing gap in RenditionPilot's
  persistence layer, not something this feature can fix on its own.

Given that, RenditionPilot has exactly **one** usable, uncorroborated
matching signal: business-name similarity against `owner_name`. There is no
address/ZIP to cross-check it against, and `account_number` is a
RenditionPilot-internal appraisal account number in a completely different
ID space from the Comptroller's `taxpayer_id`/`location_number` -- useful as
a display field on a match, never as a scoring signal.

```
COMPTROLLER_MATCH_TABLE=parsed_rendition_results
COMPTROLLER_MATCH_ID_COLUMN=id
COMPTROLLER_MATCH_DISTRICT_ID_COLUMN=district_id
COMPTROLLER_MATCH_TAX_YEAR_COLUMN=tax_year
COMPTROLLER_MATCH_ACCOUNT_NUMBER_PATH=result->metadata->>account_number
COMPTROLLER_MATCH_OWNER_NAME_PATH=result->metadata->>owner_name
```

The table/column names above are schema-verified (confirmed live via
PostgREST's schema cache). The two JSON path defaults are **not**
schema-verified -- they're inferred from `app/pipeline.py`'s code, since
`result`/`assessment_summary` are `jsonb` columns with zero real rows to
check the actual key layout against. Override
`COMPTROLLER_MATCH_OWNER_NAME_PATH`/`COMPTROLLER_MATCH_ACCOUNT_NUMBER_PATH`
once real data exists if the shape turns out to differ.

`app/comptroller/matching.py` fetches every `parsed_rendition_results` row
for the closure's RenditionPilot district and scores each candidate on the
one available signal:

| Signal | Confidence if best match |
|---|---|
| Owner-name similarity >= 0.85 | `MEDIUM` |
| Owner-name similarity >= 0.6 | `LOW` |
| Below 0.6, or no candidates, or no district mapping | `UNMATCHED` |

**`HIGH` is intentionally unreachable.** A single, uncorroborated
name-similarity signal is exactly the kind of "maybe, but I can't be sure"
evidence that should never carry the same confidence as a genuinely
corroborated match (the original address+ZIP+name design's `HIGH` tier). If
RenditionPilot's data model ever gains a situs address, city/ZIP, or a
cross-referenced taxpayer ID, add it as a second signal and `HIGH` becomes
reachable again.

**Ambiguity is tracked explicitly** (`comptroller_closure_reviews.match_ambiguous`,
added by `supabase/migrations/20260821_comptroller_match_schema_correction.sql`):
if a second RenditionPilot record scores within 0.05 of the best candidate
and is itself at least a partial match, the result is flagged `ambiguous`
rather than silently picking the top-scoring one. This is common and
expected under a name-only signal -- e.g. the same owner name appearing on
rendition records for multiple tax years, or two distinct businesses that
happen to share a similar name -- and there's no address data available to
break the tie, so ambiguity is surfaced to the reviewer instead of guessed
away.

A closure with no RenditionPilot district mapping or no district records at
all is `UNMATCHED`, never guessed.

## Review workflow

`comptroller_closure_reviews.workflow_status`:
`PENDING_REVIEW` (default) -> `CONFIRMED_CLOSURE` | `NOT_CLOSED` |
`OWNERSHIP_CHANGE` | `RELOCATED` | `DUPLICATE` | `OTHER_NEEDS_RESEARCH`.

Updating workflow status/notes (`app.comptroller.admin.update_review_workflow`,
exposed via `POST /api/admin/comptroller/review-queue/update`) is the
**only** write this feature makes to review state -- it never touches
property value, appraisal status, ownership, account status, BPP records, or
exemption data. Any consequential appraisal change stays a human decision
made through RenditionPilot's normal tools.

## Admin/observability endpoints

Gated the same way as CAD onboarding (`require_district_admin`: the caller
must be an admin of the district that owns the data):

- `POST /api/admin/comptroller/status` -- last baseline/daily run per
  monitored county, last month-end run, and the current pending month's
  unprocessed-event count.
- `POST /api/admin/comptroller/review-queue` -- list review items for the
  caller's district (optional `review_month` / `workflow_status` filters).
- `POST /api/admin/comptroller/review-queue/update` -- update one review
  item's `workflow_status`/`reviewer_notes`.

## Scheduler setup

This repo has no pre-existing scheduler/cron/worker infrastructure and
deploys as a single Render web service (see README.MD). Rather than run a
scheduler thread inside the request-serving web process (risk of missed runs
on restart, or duplicate runs if ever scaled to multiple instances), this
feature adds two **Render Cron Job** resources in `render.yaml`:

- `comptroller-daily-sync` -- `0 11 * * *` -> `python -m app.comptroller.cli sync`
- `comptroller-month-end` -- `0 12 1 * *` -> `python -m app.comptroller.cli month-end`

Running month-end on the 1st of every month with no `--month` argument
(which always resolves to "the previous calendar month") is the "robust
scheduler" choice called for by the spec -- it needs no day-of-month
arithmetic and is naturally correct across 28/29/30/31-day months and the
year rollover.

`render.yaml` is new to this repo; applying it (Render dashboard -> New ->
Blueprint) is the recommended way to add these two cron jobs, but they can
equally be created by hand with the same schedule/command if this Render
account doesn't otherwise use Blueprints.

## Manual / recovery commands

```bash
# Initial baseline import (refuses to run if the county already has data)
python -m app.comptroller.cli baseline --county Lubbock

# Daily sync (idempotent, safe to re-run; defaults to all monitored counties)
python -m app.comptroller.cli sync

# Month-end processing for a specific month
python -m app.comptroller.cli month-end --month 2026-08

# Dry run: report match results without writing review rows
python -m app.comptroller.cli month-end --month 2026-08 --dry-run

# Reprocess/retry: safe to re-run either command any number of times --
# upserts and the status_event_id unique index make both idempotent.

# Excel export of a month's review queue (all monitored districts;
# defaults to the previous calendar month, same as month-end)
python -m app.comptroller.cli export --month 2026-08
python -m app.comptroller.cli export --out ~/Downloads/closures.xlsx
```

## Monthly Excel export

`app/comptroller/export.py::build_review_queue_workbook()` turns a month's
`comptroller_closure_reviews` rows into a single-sheet `.xlsx` -- one column
per field the review record exposes (Comptroller evidence, match result and
reason, workflow status), read-only, no appraisal data involved. Three ways
to get it:

- **Website** (`frontend/index.html`, Admin tab -> "Comptroller Closures"
  card, added 2026-08-21) -- pick a month, click "Download Excel". This is
  the primary way to get it day-to-day; calls the API below under the hood.
- **CLI** (`python -m app.comptroller.cli export`, see above) -- runs
  server-side with the service-role key, covers every monitored district,
  writes a local file. Useful for ops/backfill without going through the
  website.
- **API** (`POST /api/admin/comptroller/review-queue/export`, body
  `{"access_token": "...", "review_month": "2026-08"}`) -- gated the same
  way as the other admin endpoints (must be an admin of the district being
  exported), returns the `.xlsx` as a file download. This is what the
  website button above calls.

**Automatic monthly email exists in code but is NOT currently configured.**
`app/comptroller/emailer.py` (stdlib `smtplib` only, no new dependency) can
send the just-processed month's workbook via SMTP every time
`process_month_end()` completes a real (non-`--dry-run`) run -- including
when there were zero candidates that month, as confirmation the pipeline
ran. The user decided against setting this up (Microsoft 365's MFA/app-password
requirement was more friction than wanted) in favor of the website download
above. **Because of this, every real month-end run will report
`email_sent=False` with an `EmailConfigError` ("SMTP_USERNAME and
SMTP_PASSWORD must both be set") in its logs -- this is expected, not a
bug**, and does not affect the review data, which is saved regardless (see
below for why). If email delivery is wanted later, set the env vars below on
the `comptroller-month-end` cron job only.

This is best-effort: an email failure (bad credentials, SMTP outage) does
**not** fail the month-end run or lose any review data -- the reviews are
already saved regardless. `MonthEndResult.email_sent`/`.email_error` report
what happened; `cmd_month_end` in the CLI prints it and exits `1` on an email
failure specifically (so it surfaces as a failed cron run worth checking),
separately from the exit code covering the actual data processing.

Configured for Microsoft 365's SMTP relay by default:

```
SMTP_USERNAME=<the sending mailbox address>
SMTP_PASSWORD=<its app password>
SMTP_HOST=smtp.office365.com        (default, override for a different provider)
SMTP_PORT=587                        (default)
SMTP_FROM=<from address>             (default: same as SMTP_USERNAME)
COMPTROLLER_EXPORT_EMAIL_TO=bbarrett@lubbockcad.org   (default)
```

`SMTP_USERNAME`/`SMTP_PASSWORD` are only required on the `comptroller-month-end`
cron job (not the daily sync or the web service) -- see `render.yaml`. If
Microsoft 365 basic SMTP AUTH is disabled for this tenant (Microsoft has been
migrating many tenants to OAuth-only), this will fail with an auth error;
switch to a dedicated transactional email provider (SendGrid, Postmark, etc.)
in that case by pointing `SMTP_HOST`/`SMTP_PORT` at their relay instead.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `COMPTROLLER_MONITORED_COUNTIES` | `Lubbock` | Comma-separated county names to sync. |
| `COMPTROLLER_DATASET_ID` | `3kx8-uryv` | Socrata dataset id, in case the Comptroller republishes under a new id. |
| `COMPTROLLER_BASE_URL` | `https://data.texas.gov/resource` | SODA API base URL. |
| `COMPTROLLER_APP_TOKEN` | (none) | Optional Socrata app token (`X-App-Token`). |
| `COMPTROLLER_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `COMPTROLLER_MIN_EXPECTED_ROW_RATIO` | `0.5` | Partial-download guard threshold. |
| `COMPTROLLER_DISTRICT_SLUG__<COUNTY>` | `<county>-cad` | Override the `districts.slug` a county maps to. |
| `COMPTROLLER_MATCH_TABLE` / `_ID_COLUMN` / `_DISTRICT_ID_COLUMN` / `_TAX_YEAR_COLUMN` / `_ACCOUNT_NUMBER_PATH` / `_OWNER_NAME_PATH` | see "Account matching" | Table/column mapping for matching against RenditionPilot's rendition records. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_HOST` / `SMTP_PORT` / `SMTP_FROM` | see above | Monthly export email delivery (month-end cron only). |
| `COMPTROLLER_EXPORT_EMAIL_TO` | `bbarrett@lubbockcad.org` | Recipient of the monthly export email. |

No new required secret: `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` (already
required by the rest of the app) are all this feature needs.

## Testing

`tests/test_comptroller_*.py`, using mocked Comptroller/Supabase responses
(no live network calls) via `tests/comptroller_fakes.py`, an in-memory
PostgREST double, following this repo's existing
`monkeypatch.setattr("module._request_json", fake)` convention
(`tests/test_district_service.py`). Run with:

```bash
python -m pytest tests/test_comptroller_client.py tests/test_comptroller_service.py \
  tests/test_comptroller_matching.py tests/test_comptroller_month_end.py \
  tests/test_comptroller_cli.py tests/test_comptroller_counties.py
```

## Known limitations / follow-up work

- **Matching has nothing to match against yet.** `parsed_rendition_results`
  is schema-verified but has zero rows, and no code path in this app
  currently writes to it (confirmed 2026-08-21 -- see "Account matching").
  Every closure will be `UNMATCHED` in production until RenditionPilot's
  review flow is wired up to persist processed renditions to Supabase; that
  is a pre-existing gap in the app, not something introduced by or fixable
  within this feature.
- **Only one matching signal exists (owner name).** No situs address, ZIP,
  or DBA field exists anywhere in RenditionPilot's data model today, so
  `HIGH` confidence is structurally unreachable and same-name-different-location
  ambiguity can only be flagged, never resolved algorithmically (see
  "Account matching").
- **The `COMPTROLLER_MATCH_*_PATH` JSON key names are inferred, not
  verified.** `result`/`assessment_summary` are `jsonb` with zero real rows;
  the default paths are inferred from `app/pipeline.py`'s code and should be
  confirmed once real rendition data exists.
- **Frontend: export only, no review-queue UI.** `frontend/index.html`'s
  Admin tab has a "Comptroller Closures" card (month picker + "Download
  Excel" button, calling `/api/admin/comptroller/review-queue/export`) added
  2026-08-21. There is still no page for browsing individual closures or
  changing a review's workflow status from the website -- that would still
  need a genuine review-queue UI (list + status controls calling the
  existing `/api/admin/comptroller/review-queue` and `.../update`
  endpoints), which wasn't built since only the monthly export was asked for.
- **No true cross-table transactions.** All Supabase access in this repo (not
  just this feature) goes through PostgREST/REST, which has no client-visible
  multi-statement transaction control. Each bulk upsert is one atomic
  PostgREST request; sequencing across tables relies on idempotency rather
  than rollback. Specifically, `sync_county` writes an existing permit's
  status event *before* upserting its new status/end date (not after), and
  `has_unprocessed_duplicate_event` de-dupes on insert -- so a crash between
  the two writes is safe either way: a retry either re-detects the same
  change and finds its event already recorded (no-op), or, had the ordering
  been reversed, would have silently seen no change at all. The same
  ignore-duplicates + lookup pattern applies to month-end's
  event -> review -> "mark processed" sequence.
- **"Missing from a fetch" is intentionally not itself a signal.** A permit
  that silently disappears from the dataset (rather than gaining an
  `out_of_business_date`) is not flagged. Tracking "N consecutive syncs
  without seeing this permit" as a secondary, weaker signal would be a
  reasonable future enhancement if the Comptroller data ever shows this
  happening in practice.
- **New-permit detection is intentionally minimal.** `first_seen_at` is
  stored, but there is no new-business review workflow, per the spec's
  scoping to closures for V1.
- **A closure in a county with no resolved `districts` mapping produces an
  orphaned review.** If `resolve_district_id` can't find a matching
  `districts.slug` row for a monitored county, its `comptroller_closure_reviews`
  rows are created with `district_id = null` -- which means no district
  admin can see or update them via `/api/admin/comptroller/review-queue`
  (both endpoints filter/compare on `district_id`). This can't happen for
  Lubbock today (its `lubbock-cad` district is already seeded), but would
  silently affect a newly-added county whose CAD hasn't been onboarded yet.
  Confirm the district exists (or override `COMPTROLLER_DISTRICT_SLUG__<COUNTY>`)
  before monitoring a new county.
