# County Clerk browser-assisted backfill

A requested one-time interactive browser collection is separate from unattended
quarterly collection. The latter remains disabled until an authorized method
is confirmed. Do not report a successful manual backfill as an automated sync.

Use the public assumed-name search with the requested recording-date range,
both ASSUMED NAME and ASSUMED NAME ABANDONMENT, and the site's pagination.
Capture the visible results table and every individual indexed record through
normal browser navigation. Stop on access restrictions, authentication changes,
rate-limit responses or incomplete result pages. Do not retrieve unavailable
images or infer missing fields. Original details and result-page snapshots stay
in private source storage, not in Git.

The `import_captured` service-only command accepts a complete single-page or
paginated capture set of at most 3,000 filings and a 25 MB archive. It verifies
source counts, contiguous result ranges, identical index/page identities,
unique county/document/type identities, detail identity and recording dates.
Source-count changes, skipped pages, missing details and duplicate identities
reject the set. This replaces the original 30-record prototype restriction.

Match fresh captures with the existing district BPP account matcher, treating
each full owner separately. Only exact normalized name/owner plus situs, unit
and ZIP evidence on a linked account gets HIGH priority. Possible account/name
matches are MEDIUM; no reliable match is LOW. A filing or abandonment alone is
never proof of opening or closure. The regular Intelligence queue sorts HIGH,
then MEDIUM, then LOW across all signals, retaining evidence rank and recency
within each priority tier.

Archive and commit using the existing `dba_commit_import` transaction. It
preserves immutable filings and observations, deduplicates county/document/type
and does not overwrite prior review outcomes. `commit_prepared` is an internal
service helper for server-generated classifications, not an upload endpoint.
Verify the current BPP export has not changed before committing precomputed
classifications. Record its import identifier with the backfill result.

After a complete January–September import, the matching quarterly run can be
fulfilled with the real import ID and counts, labeled as browser-assisted.
The status endpoint verifies the linked import's complete coverage and counts
before showing October coverage as complete. It continues to report
`automation_enabled=false` and preserves the separate automated-sync timestamp.

Large original captures are stored losslessly as small private JSON parts plus
an integrity-checked manifest. Original downloads reconstruct the exact source
bytes with SHA-256 validation and a 25 MB decompression limit. Filing payloads
are staged in service-only batches of at most 25 records and approximately
60 KB. Finalization verifies that every batch and expected record is present,
then invokes the existing atomic import transaction. Failed staging cannot
publish a partial Intelligence Queue set or overwrite appraiser decisions.

Apply `20261028_county_clerk_staged_import.sql` before using this transport.
Run `tests/sql/county_clerk_staged.sql` only in a disposable test database;
its fixtures roll back.
