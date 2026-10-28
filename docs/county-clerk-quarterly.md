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

## Automatic public guest collection

The owner explicitly requested automatic quarterly pulls on October 28, 2026.
The implemented route uses the public guest disclaimer and HTML search forms;
it is not an official API, county-approved integration or data-sharing agreement.
The published notice was reviewed and no automated-access prohibition was found.
No county or provider contact was made. The earlier application-wide hard block
is replaced by an explicit operator opt-in for the validated Lubbock route only.
Other counties remain disabled until their own route is validated.

Apply `20261028_county_clerk_public_collection.sql` before deployment. It keeps
RLS/service-only access and permits enabling only the exact configured Lubbock
source with an operator authorization timestamp and basis. `automation_authorized`
records that operator decision, not a representation of county endorsement.

The existing daily Render `run-intelligence` command now executes due quarterly
collections, rather than only creating plans. It catches a missed scheduled day,
claims one source/quarter with a renewable exclusive lease, and does nothing once
that quarter is complete. A dead worker can be reclaimed after two hours. Transient
failures retry at least 24 hours apart, at most three attempts. Access restrictions,
CAPTCHAs, robots disallow rules, changed notices or source-format failures stop the
run and disable the collector for review. No controls are bypassed. The daily cron
must remain enabled; no appraiser computer or open browser is needed.

Requests identify Rendition Pilot, use normal verified HTTPS and guest sessions,
follow only same-origin observed links, and wait at least two seconds between
requests (or the published robots crawl delay, if greater). All result pages must
reconcile to the same count, contiguous ranges and unique document identities.
Every result page is checked again before import. No partial result set is
published. The verified format supports up to 3,000 filings per YTD window; exceeding
that bound produces a visible failure and requires a collector update.

Each quarterly run rechecks the entire YTD index. Full certificate details already
archived with an identical current document identity, URL and normalized summary
are reused with their original capture timestamp. New or changed summaries fetch
and validate full details. This is certificate-detail caching, not reuse of saved
account matches. Existing imported filings preserve all appraiser decisions;
new filings use the current district BPP account export and existing matcher.
Changed details are retained as new source observations; the original filing and
appraiser decisions are not overwritten. An address-only change that does not
change the public summary is not detected by the detail cache.

A completed run requires a linked complete unattended import for the exact date
window, reconciled imported/duplicate counts and matching district/source IDs.
Only that finalization records a successful source-retrieval timestamp. Manual
backfills remain distinct in history. The BPP status banner shows actual coverage,
last success, next run and collection failures. The PDF importer remains a fallback.

Validation: Python tests cover source restrictions, notice changes, pagination,
cached-detail eligibility, quarterly boundaries, the collection pipeline and CAD
queue sorting. `tests/sql/county_clerk_public_collection.sql` uses disposable,
rolled-back fixtures for exclusive claims, stale leases, evidence validation,
retry cooldowns and service-only permissions. Live verification results are saved
outside Git in the task's outputs directory; do not infer live success from tests.

To load the captured prototype, create private storage bucket
`county-clerk-records` (JSON only, 25 MB), then run:

```sh
python -m app.county_clerk.import_captured --captures /absolute/captures \
  --district-id UUID --start YYYY-MM-DD --end YYYY-MM-DD --env-file /private/.env
```

The default is a dry run. Add `--commit` to archive originals and create leads.
Do not commit captured source records, account exports or credentials to Git.
