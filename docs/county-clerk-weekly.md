# County Clerk leads in BPP Intelligence

County Clerk filings share the regular BPP Intelligence queue, claim ownership,
CAD candidate review, confirmation/rejection, resolutions, activity analytics,
and audit trail. Filter by County Clerk DBA. Full indexed owners, document/date,
expiry and a safe source link appear in the detail view. Abandonments are leads;
no official account is created, modified or closed from a filing alone.

The Lubbock captured-record importer is service-only and explicitly invoked.
It reparses original browser captures, checks identities, verifies all reported
results were captured, restricts the batch to 30 documents and validates dates.
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

## Weekly readiness check

The existing daily Render job invokes `python -m app.county_clerk.weekly` before
its regular intelligence commands, checking each configured county source each Monday (America/Chicago). It logs a structured BLOCKED result with
zero records retrieved and the overlapping recording-date window. Other days
are NOT_DUE. Districts without a source are NOT_CONFIGURED. This reuses the
existing daily Render cron without adding a paid service. A missed Monday is
checked the following Monday; this is a readiness check, not a reliable
collection scheduler. The weekly command returns a nonzero exit on errors. The shell separator keeps
regular intelligence processing independent; weekly failures are visible in logs.

**Unattended collection is not active.** The provider has a hard gate even if
configuration flags are changed. Public browsing has only been tested through
a bounded interactive browser prototype. No supported authorized Lubbock API,
export, agreement or unattended retrieval has been verified. Do not remove the
gate merely because public pages are reachable. Activation requires an approved
retrieval method and a tested ingestion path with complete pagination, delayed
record overlap, durable run history, retries, rate controls and reconciliation.
No email or permission request is sent by this feature.

To load the captured prototype, create private storage bucket
`county-clerk-records` (JSON only, 25 MB), then run:

```sh
python -m app.county_clerk.import_captured --captures /absolute/captures \
  --district-id UUID --start YYYY-MM-DD --end YYYY-MM-DD --env-file /private/.env
```

The default is a dry run. Add `--commit` to archive originals and create leads.
Do not commit captured source records, account exports or credentials to Git.
