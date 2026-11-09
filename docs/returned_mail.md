# Returned-mail account folders

Prepared locally November 9, 2026. Not deployed; no production data or scans were changed.

The Windows scheduled-folder helper in `tools/returned_mail_watcher/` targets `T:\Public\Appraisal\BP\2027\Returned Mail` and reuses these APIs. Save exact-account PDFs directly in the root (`P105330.pdf`). The updated helper moves confirmed uploads into one shared `Uploaded` folder, retaining failed PDFs in the inbox and writing `_Upload status.csv`. This means evidence saved, not an approved address. It verifies the local copy before removing the inbox copy and never overwrites archived files. Existing saved login/progress survives a script-only update; older successful paths are reconfirmed through backend deduplication. See its README for recovery and Windows verification. Browser folder selection remains a manual fallback and leaves local originals unchanged. Backend ingestion is deployed; the archive update requires installation on the office PC.

## Staff workflow

1. Scan one returned envelope (front and back) into one PDF when practical. Save it inside its exact BPP account-number folder:

   ```text
   Returned Mail/
     P123456/
       2026-11-09-envelope.pdf
       2026-12-03-envelope.pdf
     P234567/
       2026-11-09-envelope.pdf
   ```

   JPEG/PNG files are also accepted. Separate front/back images remain separate scans; a file count is not a confirmed count of returned envelopes. Folder/filename dates are not assumed to be actual return dates.

2. In **Mailing Address Intelligence → Import returned-mail folders**, select the Returned Mail root or an individual account folder. Click **Import scanned folders**. Files must be directly inside the account folder. Browser folder selection is supported; this is not an automatic filesystem watcher. Original local files remain untouched.
3. The importer matches the exact folder account against eligible imported BPP accounts in the signed-in district. Unknown, ambiguous, real-property-only or out-of-scope accounts are reported as failures, not guessed or created. Numeric account numbers retain leading zeroes and require the existing BPP-only county configuration.
4. Newly saved files create/update returned-mail work in the **existing mailing queue**. Start review to reserve the account. Download the original scan, inspect extracted text and verify suggested return reasons or explicitly labeled forwarding-address text. Record a return date only when known; confirm or clear the forwarding lead and save the scan review.
5. Review/dismiss each pending scan, choose the existing mailing outcome, then **Mark as complete** or **Save & Next**. These actions do not modify CRS. If a scan is filed under the wrong account, dismiss it with the appropriate reason/notes and import its original file from the correct account folder. Historical evidence is retained rather than silently moved/deleted.
6. For the next batch, select the same folder again. SHA-256 deduplication within district/account skips the same file without adding another scan or reopening completed work. A genuinely new file arriving after completion creates new returned-mail work. Separate scans with identical bytes are indistinguishable from duplicate uploads; scan each actual returned envelope separately.

The returned-mail item displays all account scans in pages of 20, including historical scans belonging to completed items. Only scans attached to the current open returned-mail item are editable there; reopen the original item to amend an older review. Other mailing comparisons can display account scans as supporting evidence without changing their original workflow.

## Implementation and safeguards

- `app/returned_mail.py`: folder validation, exact imported account lookup, bounded PDF/image parsing, existing local Tesseract OCR, duplicate precheck, deterministic private-object upload and district-scoped retrieval.
- `frontend/returned-mail.js`: sequential folder-batch upload with per-file failures, retry-safe duplicates, session-change guards, original scan download, OCR text, review forms and unsaved-edit guards. Existing queue, permissions, account reservations and Save & Next remain in use.
- New service-only `returned_mail_documents` and append-only `returned_mail_review_events` tables preserve the original file reference, hash, uploader/time, OCR text/warnings/suggestions and human review history/version. Original evidence is in the private `returned-mail` storage bucket; users download through an authenticated district-scoped API, never a public URL.
- `returned_mail_commit` serializes imports per district/account and atomically attaches metadata to an open `source=returned_mail_scan`, `signal_type=mailing_address_change` queue item. It preserves assignment, notes and review status. A repeated import cannot create duplicate work.
- `returned_mail_review` checks document district, optimistic version, an active account reservation owned by the server-authenticated actor and an open source item. It saves audited return reason/date, raw forwarding lead, notes and review status. Clearing an OCR forwarding suggestion stays cleared after saving.
- `returned_mail_resolve` shares the import lock, checks pending scans and resolves the existing queue item atomically. A scan racing completion either blocks completion for review or creates a new item after completion. It cannot be attached as unreviewed evidence to a newly completed item.
- The Comptroller bulk link refresher explicitly excludes returned-mail folder links. Mailing queue ranking retains returned-mail items even when no CRS/Comptroller address difference is known.
- A detected printed P-account mismatch is a warning and Medium-priority identity review, not an automatic relink. Absence of a detected mismatch is not proof of recipient ownership. Only explicit forwarding-address labels produce raw OCR forwarding suggestions; sender addresses are not guessed as replacements. OCR cannot reliably distinguish every account number or USPS label.
- OCR failure does not discard a valid file: the file remains available for manual review with an unavailable/partial/unreadable reading status. No OCR/provider result is postal validation or an approved correction. No closure, address update, forwarding, mailing or re-mailing is automated.

### Limits

- Each file: 12 MiB; PDF: 1–10 unencrypted pages; JPEG/PNG: one image up to 20 megapixels. PDF rasterization is bounded to 4 million pixels per page; images are scaled before OCR, which has a per-page timeout.
- Up to 1,000 supported files per chosen batch; uploads run sequentially and share the existing server document-job limiter. Hidden macOS resource files are ignored by the picker flow.
- Supported account-folder names are a leading optional letter plus digits. Imported account/type eligibility still applies. This is a BPP workflow, not commercial/real-property mail support.
- Existing records can lack CRS mailing fields; those are shown as unavailable rather than inferred. Import the real CRS mailing fields through the previously prepared phase-one importer for fuller comparison evidence.
- OCR quality on real scanned/rotated/poor-quality envelopes, deployed storage policies and request/runtime behavior require staging verification with actual office scans. Photos and scans can require manual reading. There is no automatic OCR reprocessing of a duplicate file.

## Migration and activation — not performed

1. Apply the previously prepared `20261102_mailing_crs_comparisons.sql` and its worker/frontend rollout requirements first; this returned-mail feature builds on those local changes.
2. Apply `supabase/migrations/20261109_returned_mail.sql` once through the migration owner. It provisions the private bucket, evidence/review-audit tables and three service-only RPCs. Verify that no pre-existing global storage policy gives browser users broad access to the new bucket. Direct browser-table access is denied; backend service credentials remain server-side.
3. Deploy the prepared backend, frontend (including `returned-mail.js`) and worker changes together. Existing Docker/Tesseract support is reused; no new OCR provider, mail service or API key is needed. If OCR is unavailable, imports remain usable with manual review.
4. With synthetic accounts/scans in staging, verify ordinary BPP appraiser import/review/download, cross-district denial, duplicate reimport, multiple returns, wrong-folder handling, failed OCR, stale review versions, new imports during completion, reopening old reviews, and Save & Next. Run an import/match refresh and confirm it retains returned-mail account links.
5. Test an actual front/back office scan and a modest batch before enabling staff use. Check upload/OCR latency, memory and existing Render request limits. Folder contents are not fetched automatically; staff select each batch in the browser.

Storage and database metadata cannot share one transaction. The deterministic object is uploaded before metadata commit. If metadata commit fails, a private unreferenced object can remain; retrying the same file reuses that path and completes linking. Do not delete shared deterministic objects as failure cleanup, because a concurrent import may have committed them. Any orphan cleanup should reconcile DB references first. The existing backup collector discovers private buckets/tables automatically; validate a backup containing the new evidence after activation.

Rollback by disabling the returned-mail UI/endpoints and retaining evidence/history; do not drop the bucket or audit tables to roll back code.

## Local verification

Full local suite: 903 Python tests passed, one pre-existing skip; 33 frontend tests passed. The PostgreSQL tests include real concurrent duplicate imports and scans racing completion. No production scans or deployed storage operations were used.

- Python unit/API regressions cover folder paths, actual PDF text parsing, input limits, OCR-unavailable fallback, duplicate/no-force matching, server-derived district/actor, reservation checks, private downloads and atomic completion dispatch.
- `tests/sql/returned_mail.sql` exercises real PostgreSQL dedup, repeat evidence, preserved completed history, pending-scan guards, actor/district/version checks, audit and private bucket/RPC privileges.
- `tests/test_returned_mail_concurrency.py` exercises concurrent duplicate uploads and a new scan racing completion in a disposable local PostgreSQL database.
- Frontend tests cover folder paths, escaped source content, readonly reviews and cleared forwarding text. The existing login, queue and Save & Next tests remain in place.
- Local OCR was checked against a synthetic raster envelope using the installed Tesseract engine. A browser fixture uses the real frontend module with mocked API responses to verify folder selection, import/reimport, review save and OCR text display. Neither check establishes production integration or real-envelope accuracy.

For a disposable local DB already prepared with `tests/sql/mailing_phase1_fixture.sql` and the phase-one migration:

```sh
psql -h /tmp -d rp_mailing_phase1_test_local -v ON_ERROR_STOP=1 -f tests/sql/returned_mail_fixture.sql
psql -h /tmp -d rp_mailing_phase1_test_local -v ON_ERROR_STOP=1 -f supabase/migrations/20261109_returned_mail.sql
psql -h /tmp -d rp_mailing_phase1_test_local -v ON_ERROR_STOP=1 -f tests/sql/returned_mail.sql
MAILING_TEST_DATABASE=rp_mailing_phase1_test_local python -m pytest -q
node --test tests/frontend/*.test.cjs
```

## Automatic mailing research (2026-11-09)

`app/returned_mail_research.py` adds evidence to each pending scan without changing CRS, taxable accounts, queue assignments, reservations, review versions, or completion outcomes. The original OCR/forwarding label is retained as an unverified lead. Current CRS mailing fields remain separate from situs. Related-account lookups use exact recipient/owner names within the jurisdiction, then re-fetch the preferred current account record; shared names remain LOW confidence and historical names alone are excluded. At most 20 related accounts are shown and capped searches are disclosed.

Comptroller research starts with existing account-linked taxpayer IDs. If unavailable, exact business/DBA names discover candidates in the county; the existing situs/name matching and human CAD decisions must reconfirm the same account before mailing data is exposed. No thresholds are lowered. Multiple/incomplete source addresses remain visible as incomplete research evidence, never completed with inferred fields. A complete differing linked Comptroller address avoids an unnecessary web search. Otherwise the hosted OpenAI Responses web-search tool researches public business contact pages. Only business name and situs context are sent, not scans, OCR, private recipient mailing details, account numbers, taxpayer IDs, or owner-only identities. Output requires an executed search and cited HTTP(S) sources; it stays unverified narrative, not a parsed correction. Business location versus mailing purpose must be distinguished. Public links are clickable and all source text is escaped. No postal validation is performed.

Apply `supabase/migrations/20261109_returned_mail_research.sql` **before** deploying this version. It adds service-only research jobs/run history, an enqueue trigger and an open-pending-scan backfill. Completed scans/reviews are not backfilled. Duplicate uploads reuse the document/job. Upload responses schedule a background attempt only after evidence is saved. An interrupted process cannot lose the job. Claims use `SKIP LOCKED`, per-attempt tokens and 10-minute leases; expired/old workers cannot finish someone else's attempt. Failed/partial jobs retry daily, at most five attempts, with visible exhaustion. A mismatch or missing/ambiguous imported account skips research for manual identity review. Completed/manual scan reviews are not claimed for further attempts. An already-running research task may finish after staff completes review, but cannot modify that review.

Configuration: the **web service's** existing `OPENAI_API_KEY` is reused, or set `MAILING_RESEARCH_API_KEY`; `MAILING_RESEARCH_MODEL` defaults to `gpt-5-mini`. Missing configuration, provider errors and missing business identity are visibly distinct from a successful search with no cited source. Provider/model access and actual search responses require deployed verification. Each new scan normally makes at most one web request per attempt; duplicates do not create new jobs. Web search incurs the configured provider's usage charges.

The existing daily `run-intelligence` cron dispatches a retry to the web service through `/api/internal/returned-mail/research`. Its existing service-role credential authenticates the request; it is never sent to OpenAI. The endpoint returns 202 and schedules durable pending work for that district, rather than making cron hold a second AI key. `MAILING_RESEARCH_APP_URL` may override the default `https://www.renditionpilot.com` when deploying elsewhere; it must be the trusted application's HTTPS origin. Redirects are rejected. No new cron service or Windows update is necessary. Cron dispatch failure is logged and sets a failing exit status; per-document results are visible in review and retained in `returned_mail_research_runs`. The retry batch is capped at 25 jobs per district per day; larger interrupted backlogs need subsequent runs. New uploads are attempted immediately.

Appraisers open the existing mailing review, see **Mailing research**, and use **Refresh research** to fetch only research status/evidence while preserving unsaved review fields. Existing Save & Next and scan approval/completion are unchanged. Research does not approve an address or re-open resolved work. No manual re-search button or address export is included. For a terminally failed job, an administrator should diagnose the source/provider issue before explicitly re-queuing that pending job; retain its run history.

Local verification: Python regressions cover evidence boundaries, old recipients, missing/ambiguous accounts, preserved partial results, source failures, county scoping, high-confidence match gates, incomplete addresses, provider completion/citations, no-secret prompts and retry dispatch authentication. Local PostgreSQL checks cover job enqueue/dedup, concurrent claims, expiring leases, stale-token rejection, capped retries, review preservation and private access. Frontend regressions cover escaped evidence, incomplete/weak match labels and safe source links. These tests use simulated provider responses, not a claim that a live search has succeeded.
