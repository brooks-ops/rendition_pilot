# County Clerk DBA Intelligence

## Manual PDF workflow

Open **BPP Intelligence Queue → County Clerk DBA — import PDF**. Choose an EagleWeb printable search-results PDF (searchable text, up to 25 MB / 250 printed pages). Preview before importing. The server reparses the original file and checks its SHA-256; preview fields cannot be submitted as trusted filings. Partial or unverified search coverage requires explicit acknowledgment. Import any additional search-result windows to finish the range. Repeat a date range or the same PDF safely: county/state, document number and filing type identify a filing, and existing review work is preserved.

The supplied `DBA list.pdf` was tested: eight physical pages, 100 unique filings, documents 2026700001–2026700100, recorded January 2–30, 2026. Its search requested January 1–December 31, 2026 and reports **1,237 results, displaying 1–100**. Eight printed pages are not eight EagleWeb result windows. All 100 owner excerpts are clipped at the right edge; they are retained verbatim but excluded from matching. Missing addresses, owner names and additional parties remain unavailable. The PDF contains individual document links, which are preserved.

A filing does not prove a business opened, and an abandonment does not prove it closed. The importer never creates, modifies or closes a taxable account.

## Reused architecture

- Existing `bpp_intelligence_items` queue, district-scoped BPP authentication, statuses and actions.
- Existing `real_property_records` CRS adapter, configured county account types, active-account exclusions and name similarity.
- Existing `cad_match_decisions` history extended with a filing foreign key, including manual account search and human confirmation/rejection.
- Existing account work claims, investigation assignments, Mark complete, dismiss and activity analytics.
- Existing sales-tax permit locations supply separate supporting evidence. Their addresses are never presented as filing addresses.
- Existing CAD export matching refresh includes open DBA items. Completed reviews remain completed.

New `county_clerk_sources`, `county_clerk_dba_imports`, immutable `county_clerk_dba_filings`, per-import observations and review events preserve source evidence and history. Original PDFs use a private `county-clerk-dba` Supabase Storage bucket, downloaded through a district-checked POST endpoint. Tokens never appear in source/download URLs. RLS and privilege revocation prohibit browser keys from reading the new tables or executing import/matching RPCs. Server service credentials remain server-only.

## Matching and review

Indexed normalized business/DBA/owner names retrieve current district CRS candidates, sales-tax context and earlier filings. The existing name scorer ranks candidates. Real-property records and accounts excluded by the active DBA export are ineligible. Annual rows of one account count as one candidate. Name-only probable matches are MEDIUM; HIGH requires an appraiser-confirmed account. Ambiguity, clipped owners, missing location evidence or truncated candidate results produce conservative review findings. Original excerpts remain visible as excerpts.

Classifications: potential new business, probable existing business, possible business change, potential closure lead, needs review. Review filing evidence, start review to reserve the account, use possible CRS matches or search by account number, record the decision, investigate/dismiss or Mark complete. Opening/closure outcomes require independent-evidence notes. All outcomes remain advisory queue changes. Filter by signal, status, confidence, recording date range, filing type and assigned appraiser. Original PDFs and review changes are available in the item detail.

## EagleWeb investigation — tested October 28, 2026

Primary source: <https://erecord.lubbockcounty.gov/recorder/web/splash.jsp>.
Official help: <https://erecord.lubbockcounty.gov/recorder/web/help.jsp>.
Provider identification: <https://erecord.lubbockcounty.gov/recorder/web/about.jsp>.

Tested normal browser access after acknowledging the public disclaimer. The interface exposes document/date/name search and a public session. Opened **one actual embedded PDF reference**, `https://erecord.lubbockcounty.gov/recorder/eagleweb/viewDoc.jsp?node=DOC275S97`, for document 2026700001. It returned the full owner OLSON NEAL, business address 15719 N COUNTY ROAD 2000, LUBBOCK, TEXAS 79415, recording/expiration dates and business organization. Its image section says images are available at the County Clerk’s office. This is evidence that individual public manual review works, not a tested automated connector. These detail fields were not bulk collected or silently added to the results PDF.

No authorized API, structured export, bulk delivery contract or reliable unattended retrieval method was confirmed. Practical options are (1) current manual printable-PDF import, (2) manual individual-record review, or (3) request Clerk/Tyler-approved structured export or documented API credentials and use limits. Do not infer authorization from public accessibility. Unattended retrieval explicitly raises `RetrievalNotAuthorized`; a database constraint also forbids enabling automation. No scheduler was added or enabled. Authentication, access restrictions, rate limits and image restrictions must be respected.

## Configuration and processing

Apply `supabase/migrations/20261028_county_clerk_dba.sql` before deploying app changes. It seeds Lubbock only, based on its existing jurisdiction. Additional Texas counties require an explicitly configured source row linked to their own district/jurisdiction and their own source URL; no fallback to Lubbock records. PDFs referencing another recorder host are rejected. PDFs without verifiable source URLs require acknowledgment of unknown coverage. The format must be tested for each county before enabling its manual import.

Each import records status, parser version, source hash, requested range, coverage, warnings, rejected rows, counts, actor and original storage path. A private storage failure or matching failure leaves a visible FAILED import; the filing/queue commit is one PostgreSQL transaction. If a response times out after commit, the server checks durable status and never overwrites success. If status cannot be checked, consult history before retrying. PROCESSING with no completion requires operator review; no unattended retry worker exists. Missing originals remain a visible download failure.

`last_successful_import_at` tracks successful manual processing. `last_successful_retrieval_at` is reserved for a future approved connector and is not advanced by a PDF upload. Only a COMPLETE import advances `last_complete_recording_date`; partial windows never masquerade as full range coverage. `next_window` proposes a configurable overlap (default seven days) from the last complete recording date; initial ingestion requires an explicit range. Manual reprocessing uses the same safe importer. Future automation requires a new reviewed migration removing the hard disabled gate, authorization evidence, pagination/completeness tests, failure reporting and scheduling; setting a flag alone cannot enable it.

## Verification

```
RPD_DBA_SAMPLE_PDF='/path/to/DBA list.pdf' python3 -m pytest tests/test_county_clerk_dba.py tests/test_backend_intelligence_queue.py tests/test_comptroller_intelligence.py tests/test_cad_review.py tests/test_bpp_work_claims.py -q
psql -v ON_ERROR_STOP=1 -d disposable_database -f tests/sql/county_clerk_dba.sql
```

The SQL checks require the migration and a configured source in a disposable database. They roll back test fixtures and verify atomicity, overlapping-import deduplication, preserved completed reviews, per-import observations, audit events and restricted privileges. The original supplied PDF is not committed into the repository. For deployment rollback, revert application code first and retain filing/import history and private originals; do not drop evidence tables as a routine rollback.
