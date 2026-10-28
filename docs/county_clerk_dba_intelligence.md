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

# V2 investigation: zero-touch daily ingestion

**Investigation date: October 28, 2026, America/Chicago. Status: not activated.**
The daily target is no appraiser searching, printing, downloading or uploading. PDF import remains an exceptional fallback. No end-to-end unattended collection has been demonstrated. No approved Lubbock API, export delivery or provider license was supplied or confirmed, so no direct collector or scheduled job has been enabled.

## What was actually verified

| Question | Direct observation | Limit |
|---|---|---|
| Search by filing type | The public Assumed Names form at `customSearch.jsp?pageId=AssumedNames` offers `ASSUMED NAME` (`ASN`) and `ASSUMED NAME ABANDONMENT` (`ABN`) in a multiselect. Unchecking Search All Types exposes the two selected types. | Browser form, not an integration agreement. |
| Recording-date range | Submitted January 1–December 31, 2026 through `RecDateIDStart` and `RecDateIDEnd`; the result page echoes the requested dates and both types. | Boundary semantics, corrections, and publication lag still need acceptance tests against a licensed feed. |
| Submission | The displayed form uses POST to `../eagleweb/docSearchPOST.jsp`. Name fields are `GrantorIDSearchString` (assumed name), `GranteeIDSearchString` (owner), and `DocNumID` (filing number). | Observed form attributes only; no private endpoint discovery or off-browser POST replay. |
| Pagination | Result URLs carry session search ID and `page`. Default size 100; visible choices 5, 10, 20, 30, 50, 100. Visited page 1 (1–100), page 2 (101–200), and page 13 (1,201–1,238). | Did not collect all 1,238 records or treat a session search ID as a stable API cursor. |
| Detail retrieval | Followed actual result links to `viewDoc.jsp?node=...`. | Public manual browser retrieval verified; unattended retrieval not verified. |
| Full owners and business address | Document 2026700102 (`DOC279S100`) returned two separate owner rows, a complete business street/city/state/ZIP, expiration and filing timestamp. Document 2026700001 was checked in Phase 1. | Values are labeled HTML groups/tables. No documented JSON/XML data API was exposed or tested. |
| Abandonment completeness | Document 2026700104 (`DOC279S122`) returned document number, timestamp, business name and full owner; no business address or expiration was displayed. | Do not manufacture the missing address or treat an abandonment as a closure fact. |
| Mailing addresses/additional parties | No separate mailing address was shown in the sampled pages; both owners of 2026700102 were visible. | Not verified for all filing subtypes. Ask for the complete field dictionary, owner roles and mailing-address semantics. |
| Images | Sample detail pages say images are available at the Clerk's office. | Did not bypass the image restriction. |
| Search completeness changes | Live results reported 1,238, versus 1,237 in the supplied PDF from earlier the same day. | This shows a changed result count, not proof of which filing was delayed or corrected. |
| Authorization | Public disclaimer acknowledgment and public session work. Public help describes ordinary searches. | No affirmative authorization for unattended product collection, rate limit, service credential, data-sharing agreement, API contract or export service was confirmed. Absence of a visible prohibition is not permission. |

Primary pages tested: [EagleWeb entry](https://erecord.lubbockcounty.gov/recorder/web/splash.jsp), [Assumed Names search](https://erecord.lubbockcounty.gov/recorder/eagleweb/customSearch.jsp?pageId=AssumedNames), [search help](https://erecord.lubbockcounty.gov/recorder/web/help.jsp), [multi-owner filing](https://erecord.lubbockcounty.gov/recorder/eagleweb/viewDoc.jsp?node=DOC279S100), [abandonment filing](https://erecord.lubbockcounty.gov/recorder/eagleweb/viewDoc.jsp?node=DOC279S122). Robots information was not retrievable through the web tool; it was not treated as authorization. The county's separate main website returned 403 during research; that restriction was not bypassed.

## Ranked activation routes

The rankings below are engineering judgments based on the cited evidence, not vendor commitments or quotes. **No current price for a Lubbock machine feed was verified.** A free search or consumer subscription is not a feed license.

| Rank / route | Verified basis and feasibility | Cost | Completeness to require | Daily human intervention after setup |
|---|---|---|---|---|
| 1. Clerk-approved nightly CSV/XML/JSON export to a private delivery location | Preferred first request: authoritative source with fewer moving parts than browser sessions. Lubbock's ability and willingness to schedule this export are unconfirmed. | Unknown; request setup and recurring quote. Likely less engineering maintenance than browser scraping, but not a verified price comparison. | Both types, all owners, business/mailing addresses when held, external document number, updated timestamp and full manifest. | None if the Clerk schedules delivery; a staff member exporting it each day does **not** meet the goal. |
| 2. County/Tyler-supported Eagle Recorder Web Services | Strong product-family precedent: a county-hosted Navarro proposal explicitly specifies Eagle Recorder Web Services with XML data export/ingest. A Tulsa county-hosted customization describes web-service/data-export assessor integration. Neither proves the module is licensed or enabled in Lubbock. | Unknown; county/Tyler licensing and implementation quote required. Historical contracts are not Lubbock pricing. | Confirm exact version, read endpoints, DBA/abandonment schema, credentials, paging, updated-since cursor and corrections. | None after a supported connection and scheduler are verified. |
| 3. Existing Daily Legal Report provider's licensed daily feed | The office already receives a report, but the supplier, machine-feed offering, data dictionary and licensing were not established from an actual supplier sample or agreement. | Unknown; ask whether CSV/XML delivery is included or an add-on and whether application use is licensed. | Must include abandonments and source document references, all owners/addresses available, corrections and a daily completeness manifest. A curated news list may omit filings. | None if delivered automatically; otherwise fails the goal. |
| 4. Licensed county-record data provider (TexasFile candidate) | TexasFile advertises county records including DBAs and a Lubbock index. The inspected coverage page listed its full index through October 21, 2026; no daily DBA-feed SLA was verified. Its website terms explicitly prohibit automated retrieval. Only a separate approved data-feed agreement could make this route suitable. | Unknown enterprise/feed quote. Consumer search/PDF prices are not ingestion prices. | Verify separate assumed-name series and abandonments, publication lag, complete owners/addresses, redistribution and multi-tenant application rights. | None if a licensed scheduled feed exists; not verified. |
| 5. Another local publisher's licensed structured feed | Lubbock Lights publishes Daily Legal with Business Names, PDF and search-alert features. It identifies its index as licensed. This is a separate publisher, not a verified identification of the office's existing Daily Legal Report supplier. No machine-feed contract or complete abandonment coverage was verified. | Unknown; public subscriptions do not establish machine-use rights. | Sample against official counts and verify missing address/owner fields, filing types and rights. | None only under an agreed automated delivery arrangement. |
| 6. Explicitly approved EagleWeb browser connector | Basic manual search, page navigation and detail HTML are demonstrated. Written permission for this retrieval method, supported limits, and reliability testing remain absent. Most fragile route because forms, sessions and HTML can change. | Public viewing does not establish automation price; engineering/support cost is likely higher than a stable export (judgment). | Reconcile every page and detail; stop on authentication, throttling, format changes, inconsistent totals or missing pages. | Intended none, but access/session changes could require operator recovery; not yet demonstrated. |

Evidence for supported integration approaches:

- [Navarro County-hosted daily indexing proposal, August 21, 2020, page 3](https://navarro.easydocs.us/minutes/LinkedDir/2020/Links%202020-08-31-Special/06%20Proposal%20US%20Imgaing%20Inc.pdf): specifies Eagle Recorder Web Services with XML data. This is a documented proposed county integration, not a tested Lubbock API.
- [Tulsa County-hosted Eagle Recorder customization](https://www2.tulsacounty.org/media/u2md5upv/county-clerk-tyler-technologies-cmf-232739.pdf): describes an assessor integration using web services or data export. Search-index content was available; direct document retrieval returned 403. No Lubbock compatibility or pricing was inferred.
- [TexasFile data features](https://www.texasfile.com/texas-land-records-data-features/), [Lubbock coverage](https://www.texasfile.com/texas-land-records-coverage/lubbock-county-clerk/), and [published terms](https://www.texasfile.com/about/tos/): county-record coverage is advertised, but website automation is prohibited and an ingestion license was not obtained. Its [Monthly Filings](https://www.texasfile.com/texas-land-records-data-features/monthly-filings/) is a browser search feature, not evidence of a daily delivery API.
- [Lubbock Lights Daily Legal](https://lubbocklights.com/legal/) and [publisher service description](https://lubbocklights.com/our-stuff/): digital reporting/alerts exist; complete structured delivery and a Rendition Pilot license remain unverified.

## What must be supplied to activate zero-touch collection

1. **An affirmative source agreement:** Clerk/Tyler or licensed provider approval of the exact unattended method and Rendition Pilot processing/storage/use. Clarify whether a county data-sharing agreement, commercial license, service account, allowlisted IP, or other approval is required. No such requirement was confirmed as a Lubbock legal determination; these are questions for the source owner.
2. **An actual supported delivery contract:** scheduled export location or documented read API, test credentials, schema/sample, scope, retention, supported limits, publication time and failure contact. Do not send production credentials in ordinary email; arrange a secure handoff.
3. **Complete data:** county and external document number; filing type; recording date/time with timezone; assumed name; owner array and roles; business address; mailing address only when supplied; expiry; source reference; source update/version; correction/deletion markers if available. Missing fields must be explicit nulls, not synthesized from owner addresses.
4. **Reliable coverage:** daily manifests even for zero-result days, totals and checksums, stable pagination or immutable export snapshots, overlap by recording date, updated-since support for corrections, backfill and reprocessing. Default seven-day overlap catches some late indexing but cannot guarantee arbitrarily delayed records; use the provider's update cursor plus periodic wider reconciliation where permitted.
5. **Prove unattended operation:** initial bounded backfill; controlled multi-page test; consecutive scheduled runs including a zero-change day; delayed/corrected record fixture; duplicate replay; authentication expiry, 429 and server failure; partial-page rejection; reconciliation to official totals. Never advance the successful-retrieval watermark for incomplete runs.
6. **Shared ingestion, not a second queue:** normalize the approved provider's records into the existing filing/observation model and CRS evidence matcher, retain the original response/export and run ID, atomically commit filings and existing BPP queue items, preserve human decisions and claims, and log every collection run. Existing identity is state/county/document number/filing type; also reconcile document-number identity across type corrections without deleting original filing evidence.
7. **Only then activate scheduling:** scheduled collector uses server-held credentials, district-scoped configuration, retries with bounded backoff/Retry-After and one active run per county. An admin-only Sync now action uses that same collector; appraisers only review findings. No browser scripts or cron collection were enabled as part of this investigation.

## Dashboard changes delivered in this pass

The existing BPP Intelligence page now has a County Clerk dashboard above the collapsed PDF fallback. It reports last successful direct sync separately from the last successful manual import, new-name filing and abandonment totals, matched accounts, open items requiring investigation, missing owners/addresses and fallback import failures/partial coverage. Counts are explicitly labeled **all imported county filings**, not automated daily discoveries. The status says awaiting an approved connection, and Sync now stays disabled. No collection runs have occurred, so the dashboard does not fabricate a run history, successful synchronization, provider error or schedule. Real run logging and the functional admin sync action remain dependent on the supported adapter described above.

The district-scoped `/api/county-clerk/sync-status` endpoint is read-only and uses bounded count queries. An unconfigured district does not fall back to Lubbock. An unavailable count is an error, not a fabricated zero. The PDF importer, CRS matching, existing Intelligence Queue and complete/dismiss workflow remain available.
