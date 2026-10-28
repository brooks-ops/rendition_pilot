# County Clerk leads in BPP Intelligence

County Clerk filings share the regular BPP Intelligence queue, claim ownership,
CAD candidate review, confirmation/rejection, resolutions, activity analytics,
and audit trail. Filter by County Clerk DBA. Full indexed owners, document/date,
expiry and a safe source link appear in the detail view. Abandonments are leads;
no official account is created, modified or closed from a filing alone.

The Lubbock captured-record importer is service-only and explicitly invoked.
It reparses original browser captures, checks identities, verifies all reported
results were captured, validates all pages, caps the batch at 3,000 filings, and validates dates.
It uses the existing address/name matcher for each full owner separately.
Only unambiguous HIGH matches are linked automatically; other suggestions need
review. Other counties require their own validated source format/configuration.
The existing PDF import backend remains a fallback.

Original captures are stored in private `county-clerk-records` JSON storage;
PDF originals remain in private `county-clerk-dba`. Import metadata identifies
the storage bucket. Both use district-checked original-download endpoints.
The existing atomic `dba_commit_import` RPC deduplicates county/document/type,
preserves observations and leaves already reviewed queue items untouched.
Captured imports are not recorded as successful unattended synchronization.

## Quarterly year-to-date collection plan

The schedule is January 1, April 1, July 1 and October 1, using America/Chicago.
Each run covers January 1 through the day before its scheduled date:

| Run date | Recording-date coverage |
| --- | --- |
| January 1 | January 1–December 31 of the prior year |
| April 1 | January 1–March 31 |
| July 1 | January 1–June 30 |
| October 1 | January 1–September 30 |

The October run prepares leads for November/December appraisal work. Re-reading
the full year catches delayed recordings in earlier quarters. Existing county,
document-number and filing-type deduplication prevents duplicate queue items.
Original observations and resolved/dismissed reviews are preserved. Matching
uses the existing district BPP account adapter and full-owner/name/address
matcher against the currently loaded account export, not the prototype report's
saved account matches. A quarter's account matches are not official account edits.

The existing daily Render `run-intelligence` command invokes quarterly planning
before county processing. It creates one durable collection-plan row per source
and scheduled date, using the latest due quarter if a scheduled day was missed.
Repeated daily or simultaneous county invocations return the same row and do
not overwrite status or evidence. The legacy `weekly` module delegates to this
quarterly planner; there is no remaining weekly collection schedule.

Run records are in `county_clerk_collection_runs` with RLS and service-only
access. The planning RPC can only create BLOCKED plans and cannot activate
retrieval. Unattempted retrieval counts and completion timestamps remain NULL.
The BPP collection-status endpoint separates the schedule, pending window,
October coverage readiness, last successful source retrieval and plan history.

**Actual unattended quarterly collection remains blocked.** Published EagleWeb
notices reviewed on October 28 do not settle permission for automation. No
approved retrieval method or complete unattended collection has been verified.
A complete browser-assisted backfill can fulfill October coverage without
activating unattended collection. Its linked import must show complete
January–September coverage and reconciled counts. The original 28 prototype
filings cover October 19–28 and cannot fulfill the October run by themselves.
See `eagleweb-terms-review-2026-10-28.md` for the exact findings and limitations.

A future collector must implement approved access, complete pagination, full
filing details, durable run claims/retries, original-source archival and
reconciliation before any run can become COMPLETED. It must feed the existing
atomic import and matching path and preserve human decisions. Do not enable
collection merely by changing a flag or call planning a successful pull.
No Clerk/provider message is sent by this feature.

Apply `20261028_county_clerk_quarterly_plans.sql` before deploying the planner.
Use `python -m app.county_clerk.quarterly` to plan the latest due run. This command
performs application database operations only; it does not contact EagleWeb.

To load the captured prototype, create private storage bucket
`county-clerk-records` (JSON only, 25 MB), then run:

```sh
python -m app.county_clerk.import_captured --captures /absolute/captures \
  --district-id UUID --start YYYY-MM-DD --end YYYY-MM-DD --env-file /private/.env
```

The default is a dry run. Add `--commit` to archive originals and create leads.
Do not commit captured source records, account exports or credentials to Git.
