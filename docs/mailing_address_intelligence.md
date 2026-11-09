# Mailing address intelligence — CRS comparison phase

Implementation prepared November 2, 2026. **Not deployed; no production migration, import, scan, or review writes were performed for this change.** This document supersedes the earlier taxpayer-history-only design.

A subsequent local returned-mail folder import is documented in [returned_mail.md](returned_mail.md). It remains a separate prepared migration and has not been deployed.

## Current implementation

The existing CAD CSV/Excel importer, `real_property_records`, BPP queue, permissions, account reservations, completion outcomes, resolved folder, reopening, and Save & Next remain in use. The existing `mailing-address-scan` CLI and scheduled `run-intelligence` command now call `mailing_comparisons.run_scan`.

The scan compares a reliably linked account's **imported CRS mailing** with its current cached Comptroller mailing on every run, including the first comparison. It does not require a recent Comptroller change. Neither side is presumed correct, and resolving an item does not update CRS.

Scope is active Comptroller taxpayers in the configured county and their reliably linked BPP accounts. This is not a scan of every CRS recipient: accounts without a corresponding active permit/reliable match are outside this comparison source. The Comptroller sync must run separately, as it already does.

### Import fields and preservation

Optional mapped fields:

| Normalized field | Common suggested spreadsheet headers |
|---|---|
| `mailing_recipient` | MailingRecipient, MailingName, MailName, MailToName |
| `mailing_address_line1` | MailingAddress1, MailingAddressLine1, MailAddress1, MailingAddress |
| `mailing_address_line2` | MailingAddress2, MailingAddressLine2, MailAddress2 |
| `mailing_unit` | MailingUnit, MailSuite |
| `mailing_city` | MailingCity, MailCity |
| `mailing_state` | MailingState, MailState |
| `mailing_zip` | MailingZip, MailingZipCode, MailZip |

These are separate from owner/business/situs fields. Do not map the owner to the mailing recipient without checking the export's actual meaning. No address is inferred from a name. Line 1 is an address line, not a combined city/state/ZIP field; combined fields require a reviewed source mapping, not guessed parsing.

Existing account-ID/situs import requirements remain. Missing/unmapped mailing columns do not overwrite mailing data or its source metadata, even if an older saved mapping mentions those columns. **Blank cells in a present, mapped mailing column explicitly clear that field** (needed to remove an old unit or second line). Preview/import warnings explain this; omit the mapping to preserve a field. Other import semantics are unchanged.

Each supplied mailing field records import ID, import time, source and export as-of date. A merge trigger retains older provenance for omitted fields, so a later partial import does not make all address fields appear newly supplied. The review includes per-field source dates. Missing export as-of dates remain unknown. CSV identifiers/ZIPs remain text; Excel text values and explicit zero-mask number formats are preserved. General-formatted numeric cells cannot recover leading zeroes or precision already lost in Excel; prefer text columns.

### Classification and evidence

- `DISCREPANCY`: usable components differ. High queue priority means a reliable account link and a discrepancy worth researching, not confidence in a proposed correction.
- `MISSING_CRS_MAILING`: linked account has no imported CRS street/PO box. Medium review gap; no inferred current address.
- `INCOMPLETE_DATA`: missing/malformed street, city, state or ZIP; conflicting units; or conflicting taxpayer mailing values across active locations. Medium review gap, never a supported correction.
- `NO_CURRENT_DISCREPANCY`: normalized sources agree. No new flag. An already-open review remains open with refreshed evidence for the appraiser to finish.
- `ACCOUNT_MATCH_NEEDS_REVIEW`: a previously linked open comparison no longer has the same reliable match. Retain its account/reservation and staff notes, downgrade the badge, and require identity research before acting.

Formatting suppression covers case, punctuation, recognized suffixes/directions, PO-box spelling, suite marker variants and equivalent placement of a suite in line 1, line 2 or a unit column. Street/PO-box number, unit additions/removals, city, state, ZIP5 and meaningful second-line changes are retained. ZIP+4 addition/removal alone is suppressed; two supplied, different ZIP+4 extensions are reviewable. Additional mailing lines remain preserved raw and compared; there is no address inference from care-of names.

The detail view displays both recipients and raw addresses, source/import/as-of/retrieval dates, discrepancy reasons, and the underlying account matcher reasons/signals. Identity confidence, source dates and postal validation are separate. A retrieval date is not an address effective date. Postal validation is always **NOT_PERFORMED** in this phase. Format completeness checks do not establish USPS deliverability or intended-recipient ownership. Non-US/incomplete formats are sent to review rather than treated as correction evidence.

## Correctness, review preservation and legacy coexistence

`mailing_record_observation` serializes by county, identity and source. Consecutive identical values do not add rows; A → B → A adds three chronological observations. Historical rows are retained. Previously suppressed historical transitions cannot be reconstructed from the old distinct-address table without another source; no historical dates are fabricated.

`mailing_save_comparison` serializes by county and taxpayer/account key. A partial unique index permits only one open version-two comparison per key. Repeated scans refresh evidence without changing status, assignment, notes, reviewer, completion dates or reservations. Review rows are locked while updating evidence to avoid overwriting concurrent completion.

A fingerprint tracks address facts and completeness, excluding import/retrieval timestamps. An unchanged resolved comparison remains resolved. Changed facts after completion create a new event. A → B → A, including an intervening agreement, is tracked through comparison state rather than destination-address deduplication.

One open, linked legacy flag can be adopted in place, retaining its ID/source key, reservation, assignment and notes. Its original address evidence is copied into `evidence.legacy_snapshot` and retained on subsequent refreshes. Resolved/dismissed legacy flags are never converted or reset. Extra pre-existing legacy flags are not automatically dismissed; they remain explicitly labeled historical Comptroller observations. Unlinked legacy flags remain stored and excluded from the matched-account queue. The old history-only scanner no longer creates new flags.

The shared queue unique index now includes jurisdiction. New mailing lookups, comparison state and RPCs are county-scoped. Existing non-mailing source keys and review behavior remain unchanged.

The account matcher retains its existing confidence/name/ambiguity thresholds. Mailing addresses are never substitute situs evidence. Only active locations supply current account-link evidence. Candidate address lookups now paginate past server row caps and repeated annual rows; incomplete candidate sets must not appear unambiguous. Completed/new-comparison links are excluded from the older bulk legacy-link refresher.

## Durable run ledger

`mailing_scan_runs` records county/district/source scope, start/heartbeat/end, RUNNING/SUCCESS/FAILED, taxpayers examined, observations added, flags created/refreshed, unlinked count, missing CRS count, incomplete source count and sanitized error categories. Successful zero-change/zero-record scans are recorded. Per-record failures allow remaining records to be examined, then mark the run FAILED and return a nonzero CLI result. Fatal fetch failures also finish the run as FAILED when the database is reachable.

Heartbeat counts are saved every 25 taxpayers. A two-hour expired heartbeat can be reclaimed on the next run; the prior run is marked FAILED. RPCs fence expired workers so they cannot later overwrite a newer run's evidence. A fresh RUNNING lease rejects another run. A process/database outage can leave RUNNING until the next run reclaims it; this is visible, not a false success. Database unavailability can prevent ledger writes entirely and must still be monitored through worker/cron logs.

The existing mailing queue response exposes only the authenticated district's latest run. Its status/counters appear above the queue for ordinary BPP staff and admins. Run errors contain exception categories and hashed record keys, not recipient names, addresses or raw provider responses. Full-county runtime and queue volume still need staging/production verification; local fixtures do not establish operating performance.

## Read-only investigation of the 90 unlinked flags

November 2 read-only inspection found **90 flags for 88 distinct taxpayers**. No links were written, thresholds lowered, or account assignments forced.

| Reason | Flags with original candidate lookup | Flags after local pagination fix, using read-only production inputs |
|---|---:|---:|
| No eligible situs/name candidate | 35 | 36 |
| Strong names but missing required unit evidence | 10 | 10 |
| Only partial name evidence | 4 | 4 |
| Situs-only evidence without reliable names | 9 | 9 |
| Ambiguous location match | 25 | 24 |
| More than one reliably matched account | 5 | 5 |
| No active permit location | 2 | 2 |
| **Total** | **90** | **90** |

For multi-location taxpayers, the strongest blocker determines this aggregate category; it is not a count of individual locations. No eligible candidate is not proof no CRS account exists: source situs, account eligibility, current-year data and imported names/units can all limit coverage. Only actual matcher evidence was classified; correct recipient ownership was not independently verified.

Defects identified: historical/inactive locations were eligible inputs to the old mailing linker; detailed match reasons were discarded; candidate queries could silently stop at a database cap before collapsing annual rows; the bulk refresher could rewrite completed links. These paths are corrected locally. The five multi-account taxpayers genuinely had more than one distinct account, not merely duplicate property IDs for one account. Ambiguity and missing unit/name evidence remain intentional safeguards. Reconcile source fields or use the existing source-permit account review workflow after independent research; do not remove those safeguards to improve a match-rate metric.

The two extra flags beyond distinct taxpayers are pre-existing multiple history events. They are preserved. At most one new open comparison per taxpayer/account/county is allowed; the historical data is not rewritten to manufacture that guarantee retroactively.

## Rollout and backfill — NOT executed

1. Stop/pause the intelligence worker and any manual mailing scans. Confirm no old worker is still running: old code uses the removed destination-only observation conflict target and must not run after this migration. Take the normal backup.
2. Apply `supabase/migrations/20261102_mailing_crs_comparisons.sql` using the migration owner. It adds mailing/provenance fields, comparison state, run ledger, county-scoped uniqueness, field-provenance trigger and service-only RPCs. No existing observations/reviews are deleted. Check schema/RPC discovery and retain current RLS/permissions. The migration is additive except replacement of the two dedup indexes.
3. Deploy the prepared backend/frontend/worker code together while the old worker is stopped. Then refresh the client's assets. No new mail provider, postal API key, or external connector is required.
4. Obtain an actual LCAD account export containing mailing fields. Use the existing preview/mapping screen, keep situs separate, check a sample of recipients/PO boxes/units/ZIPs and blank-clearing warnings, and enter the export's actual as-of date. No specific LCAD mailing column names were verified in this task. Existing configured LCAD mapping currently lacks mailing fields; they must be mapped from the real export.
5. Import the reviewed export. Existing no-mailing imports remain valid. A new tax-year row is a separate account version: the code does not copy a prior year's mailing into an unsupplied new-year row. Review field-level dates when using partial updates. Wait for the existing match refresh to finish; it does not itself run this new comparison scan.
6. In staging first, run `python -m app.comptroller.cli mailing-address-scan --jurisdiction lubbock --dry-run` and inspect discrepancy/missing/unlinked counts. Dry runs make **no** ledger, observation, queue or comparison-state writes. They do not predict exact insert counts. Then run the same command without `--dry-run` for the comparison backfill, with a current Comptroller cache. Importing alone does not complete backfill.
7. Verify the run is SUCCESS (including zero-change success), sample both directions of disagreement, recipient/line-2/unit/ZIP handling, legacy adoption, and a missing-CRS item. Use a staff test account to verify reservation → outcome → Save & Next and admin/member county isolation. Complete one test review, rerun, and verify it is not reopened; refresh a CRS export and verify new facts are detected. Check the expected number of run/flag records and benchmark full-county runtime/memory before restoring the normal worker schedule.
8. Existing `bpp-intelligence-daily`/`run-intelligence` wiring runs this implementation when `mailing_address_monitoring` is enabled. Do not create another scheduler. The migration, backfill, actual import mapping, full-county scan and deployed browser behavior remain unverified until these steps are performed.

Rollback: stop the new worker first. Do not simply deploy old mailing code against the new chronology schema: the old `on_conflict` target no longer exists, and restoring destination-only uniqueness would destroy recurrent-address history. Prefer a forward fix or temporarily disable mailing monitoring while retaining the additive schema/data. Do not reset staff reviews.

## Local verification

Python regressions cover importer preservation and provenance, first comparison, unchanged source/new CRS, formatting, line 2, unit conflicts, malformed/incomplete data, A → B → A, resolved-review preservation, no forced links, zero-change/failing runs, authenticated-district run status and later loss of match confidence. Frontend tests cover evidence rendering/escaping and preserve the existing Save & Next/reservation suite.

Disposable PostgreSQL tests exercise the actual migration/RPCs: monotonic transition history, county isolation, one open comparison, resolved states, legacy evidence adoption, source-provenance merge, zero-change ledger, expired leases and RPC permissions. Optional concurrent PostgreSQL tests prove simultaneous writes create one flag/observation. These are not a substitute for a Supabase/PostgREST staging test with the actual LCAD mailing export.

To reproduce SQL checks on a **new local disposable database only**, create the Supabase roles locally if absent, then:

```sh
createdb -h /tmp rp_mailing_phase1_test_local
psql -h /tmp -d rp_mailing_phase1_test_local -v ON_ERROR_STOP=1 -f tests/sql/mailing_phase1_fixture.sql
psql -h /tmp -d rp_mailing_phase1_test_local -v ON_ERROR_STOP=1 -f supabase/migrations/20261102_mailing_crs_comparisons.sql
psql -h /tmp -d rp_mailing_phase1_test_local -v ON_ERROR_STOP=1 -f tests/sql/mailing_phase1.sql
MAILING_TEST_DATABASE=rp_mailing_phase1_test_local python -m pytest -q tests/test_mailing_sql_concurrency.py
python -m pytest -q
node --test tests/frontend/*.test.cjs
```

Correction exports, returned envelopes/reasons/forwarding, re-mailing, automatic CRS updates, postal-provider validation and broader government-office generalization are not implemented in this phase.
