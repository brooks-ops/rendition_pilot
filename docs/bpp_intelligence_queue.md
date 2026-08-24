# BPP Intelligence Queue + New Business Detection

Generalizes RenditionPilot's Comptroller-derived alerts into a reusable
"intelligence item" model, and implements the first new detection source on
top of it: **New Business Detection** -- identifying newly-active Texas
Comptroller sales-tax locations that don't appear to already have a
RenditionPilot BPP account.

**Core principle:** this is a decision-support system, not an automation
system. Every intelligence item is evidence for a human appraiser to review.
Nothing in this feature writes to property value, appraisal status,
ownership, account status, BPP records, or exemption data -- only to this
queue's own status/resolution/notes/reviewer fields.

## What already existed and was reused (not rebuilt)

Per the existing architecture (see `docs/comptroller_closure_monitor.md`):

- **Comptroller ingestion**: `app/comptroller/client.py` and
  `app/comptroller/service.py::sync_county()` (the daily sales-tax sync) --
  New Business Detection reads `comptroller_permit_locations`, which that
  sync already keeps current. **No second Comptroller integration was
  built.**
- **Matching**: `app/comptroller/matching.py`'s name-similarity scorer
  (`score_candidate`, `match_closure_to_account`) is reused as-is (extended,
  not duplicated -- see "Matching enhancements" below) via a new
  jurisdiction-aware wrapper (`app/comptroller/cad_adapter.py`).
  `fetch_candidate_records()` is the same function both the closure monitor
  and New Business Detection call.
- **Database/ORM pattern**: raw PostgREST via `requests`, following
  `app/district_service.py`'s established `_request_json` convention -- no
  new persistence layer.
- **Auth**: `require_district_admin` (existing, unmodified) gates every new
  endpoint the same way CAD onboarding and the Comptroller admin endpoints
  already are.
- **Scheduling**: the same Render Cron Job pattern (`render.yaml`) as the
  sales-tax monitor -- see "Scheduling" below. **No cron job was enabled for
  this feature**, per the request; it's available to run manually first.
- **Migration conventions**: `gen_random_uuid()` PKs, `set_updated_at()`
  trigger, `add column if not exists`, matching every prior migration in
  this repo.

## What was NOT reused, and why: the sales-tax closure monitor's own table

`comptroller_closure_reviews` (the existing, live, cron-scheduled sales-tax
closure review queue) was **not** migrated onto the new generalized
`bpp_intelligence_items` table. That table's write path
(`app/comptroller/month_end.py`) is unmodified. Reasoning:

- It's a real, deployed, cron-scheduled feature with live production data
  and jobs currently running against it.
- Migrating it would mean moving production rows and rewriting a tested
  write path while its cron job is active, for an architectural win with
  real risk to a working feature.
- The explicit instruction for this pass was "do not rebuild or break that
  feature" and "protect the functioning closure monitor over architectural
  purity."

**Instead: a read-time merge.** `app/comptroller/intelligence.py` fetches
from both `bpp_intelligence_items` and `comptroller_closure_reviews`,
normalizes both into one `UnifiedIntelligenceItem` shape, and routes
investigate/resolve/dismiss actions back to whichever table an item actually
lives in (`source_table` field). The Intelligence Queue UI shows both signal
types together without either table knowing about the other.

**If a real schema migration is wanted later:** the cleanest path is (1) add
an `INSERT ... SELECT` migration copying `comptroller_closure_reviews` rows
into `bpp_intelligence_items` with `signal_type='sales_tax_inactive'`, (2)
switch `month_end.py`'s `_create_review`/`_find_review_by_status_event_id`
to target `bpp_intelligence_items` instead, (3) once verified stable for a
full month-end cycle, drop `comptroller_closure_reviews`. Not attempted here
because steps 2-3 touch the live write path directly.

## Multi-jurisdiction architecture (why this isn't Lubbock-only code)

RenditionPilot is meant to serve appraisal districts beyond Lubbock. Every
piece of Lubbock-specific configuration lives in **data** (a `jurisdictions`
row), not in New Business Detection's code:

- `app/comptroller/jurisdictions.py` -- the `Jurisdiction` model
  (`comptroller_county_code`, `capabilities`, `cad_field_mapping`, linked
  `district_id`) and `validate_capability()`, which distinguishes "cannot
  run at all" (missing required data) from "can run with reduced matching"
  (missing optional data) -- see spec item 11.
- `app/comptroller/cad_adapter.py` -- the `CadAdapter` interface
  (`get_bpp_accounts`, `find_accounts_by_address`, `get_real_property`,
  etc.) and `NormalizedAccount`/`NormalizedProperty`. `RenditionPilotCadAdapter`
  is the one real implementation today (RenditionPilot's own Supabase
  project, reused via `matching.fetch_candidate_records`); its
  `find_accounts_by_address`/`get_real_property`/`find_property_by_situs`
  return empty/`None` rather than raising, since RenditionPilot has no
  address or CRS/property data yet (see "Known limitations" in
  `docs/comptroller_closure_monitor.md`) -- documented gaps, not crashes.
- `app/comptroller/new_business.py::run_new_business_detection(jurisdiction_id)`
  -- takes a jurisdiction, never a county name or Comptroller code literal.
  `python -m app.comptroller.cli detect-new-business --jurisdiction lubbock`,
  not a Lubbock-specific command.

**What deliberately was NOT touched:** `app/comptroller/counties.py`'s
`TEXAS_COUNTY_CODES`/`COMPTROLLER_MONITORED_COUNTIES` (env-var-based) config
still powers the *existing* sales-tax sync/month-end commands unchanged --
migrating that onto the jurisdictions table is a safe, small follow-up, but
wasn't required to keep New Business Detection jurisdiction-native, and
touching it wasn't necessary for this pass.

**Onboarding a second Texas county**, with today's single adapter, is:
insert one `jurisdictions` row (name, `comptroller_county_code`, linked
`district_id`, `capabilities`), no code change. If that county's account
data lived somewhere differently shaped, the only additional work is a new
`CadAdapter` implementation -- the detection/matching/classification logic
in `new_business.py` would not change.

## Data model

New migration: `supabase/migrations/20260822_bpp_intelligence_queue.sql`.

- **`jurisdictions`** -- one row per configured appraisal district/county.
  Lubbock is seeded (`slug='lubbock'`, `comptroller_county_code='152'`,
  linked to the existing `lubbock-cad` district), not hardcoded as a special
  case.
- **`bpp_intelligence_items`** -- the generalized review-item model.
  `signal_type` (`sales_tax_inactive` | `new_business`, extensible via a
  small migration to the CHECK constraint), `status` (`NEW` / `IN_REVIEW` /
  `RESOLVED` / `DISMISSED`), `classification`
  (`EXISTING_ACCOUNT_HIGH_CONFIDENCE` / `POSSIBLE_EXISTING_ACCOUNT` /
  `NO_ACCOUNT_FOUND` / `AMBIGUOUS`), `priority`, the Comptroller evidence
  fields, `match_signals` (jsonb per-signal breakdown), `matched_address`/
  `property_account_number` (present in schema, null today -- ready for when
  address/CRS data exists), `resolution` + `resolution_notes` + `reviewed_by`
  + `reviewed_at`. Unique on `(signal_type, source, source_record_id)` --
  the dedup key -- across every status, including resolved/dismissed, so a
  decision already made is never silently recreated.
- **`comptroller_permit_locations`** gains two nullable, additive columns:
  `jurisdiction_id` (backfilled once for existing rows; **not** written by
  `sync_county()`, which is intentionally left untouched -- see "New
  Business Detection queries by county name, not jurisdiction_id" below) and
  `new_business_evaluated_at` (marks a permit as already assessed, so
  repeated runs don't re-fetch/re-match it forever).

### New Business Detection queries by county name, not jurisdiction_id

`get_new_business_candidates()` filters `comptroller_permit_locations` by
`county` (text), not `jurisdiction_id`, even though the column exists.
Reason: `county` has reliably held the jurisdiction's name since that
table's creation and needs no backfill-timing dependency; `jurisdiction_id`
would only be correct once every future write to that table populates it,
which would mean modifying `sync_county()` -- the live, already-running
write path this feature deliberately does not touch. `jurisdiction_id` is
still backfilled and stored on every `bpp_intelligence_items` row, so
direct FK-based queries/reporting work today.

## Defining "new business" without a reliable "opened date"

The Comptroller dataset has no explicit, trustworthy "date this business
opened" field. `permit_date` (stored as `permit_start_date`) is when the
Comptroller issued the sales-tax permit -- usually close to when a business
started, but not guaranteed, and sometimes historical/backfilled.

**Detection signal used instead:** a permit is a new-business candidate when
RenditionPilot's own ingestion first observed it (`first_seen_at`) during a
**non-baseline** sync (`is_baseline = false`) and it is currently `ACTIVE`.
This is RenditionPilot's own recorded fact from comparing successive daily
snapshots -- not a Comptroller-supplied date taken on faith. `permit_start_date`
is preserved on every intelligence item as corroborating evidence, never as
the sole detection signal.

**Documented limitation:** a permit that quietly existed for years but only
recently came into scope for some unrelated reason (a data correction, a
county-boundary reassignment) would be indistinguishable from a genuine new
business under this method. Lubbock County has been fully baselined, making
this unlikely in practice, but it's a real limitation of "first observed by
us," inherent to the source data not providing a trustworthy opened-date --
not silently ignored.

**Idempotency:** `new_business_evaluated_at` marks a permit as already
assessed (set regardless of outcome) so repeated runs don't re-evaluate it
forever. Unlike sales-tax status (re-checked every sync because it changes
over time), "was this a new business" is a one-time determination for V1 --
relocation/ownership-change *over time* are separate, future signal types.

## Matching enhancements (shared with the sales-tax closure monitor)

`app/comptroller/matching.py` gained two additions, used by both detection
modules:

- **`MatchResult.signals`** -- a per-signal breakdown (`address`, `zip`,
  `suite_unit`, `property_account`, `business_dba_name`, `legal_entity_name`,
  `existing_rendition_record`) shown verbatim in the Intelligence Queue's
  detail view. Signals RenditionPilot has no data for are explicitly
  `"NOT AVAILABLE (...)"`, never silently omitted or reported as a false
  `"NO MATCH"` -- avoiding the "unexplained black-box score" the spec calls
  out.
- **`MatchResult.name_signals_diverge`** -- true when the DBA/business name
  matched strongly but the legal/taxpayer name didn't (or vice versa). This
  is the practical, implementable version of "ownership change" detection
  given RenditionPilot stores only one name field per rendition record: it
  can't *confirm* an ownership change, but it can flag "this match came from
  only one of the two Comptroller name fields, the other disagrees" so a
  human notices instead of the system silently treating a strong DBA match
  as equivalent to a full identity match.

**HIGH confidence remains unreachable** (unchanged from the closure monitor
-- see that module's docstring): a single uncorroborated name-similarity
signal is never "confirmed," only "maybe." This means
`EXISTING_ACCOUNT_HIGH_CONFIDENCE` (which would suppress an alert by
default) **never actually fires today** -- even an exact name match lands in
`POSSIBLE_EXISTING_ACCOUNT` and gets an intelligence item, not a silent
clear. `classify_match()`'s HIGH-confidence branch is real, tested code,
ready for the day RenditionPilot has a second corroborating signal (address,
cross-referenced ID) to actually reach HIGH -- see the real validation
results below for how often this matters in practice.

## Classification

| `confidence` / `ambiguous` | `classification` | Alert created? | `priority` |
|---|---|---|---|
| `HIGH`, not ambiguous | `EXISTING_ACCOUNT_HIGH_CONFIDENCE` | No (unreachable today -- see above) | `LOW` |
| `MEDIUM` or `LOW` | `POSSIBLE_EXISTING_ACCOUNT` | Yes | `MEDIUM` |
| `UNMATCHED` | `NO_ACCOUNT_FOUND` | Yes | `HIGH` |
| any tier, `ambiguous=true` | `AMBIGUOUS` (overrides the tier) | Yes | `MEDIUM` |

"Do not create an alert for a high-confidence match unless there's another
meaningful discrepancy" (spec item 7): there is currently no second signal
(e.g. address-based relocation detection) available to check for such a
discrepancy, so this branch is a documented no-op today, not a missing
feature silently dropped.

## Review lifecycle & safety

`NEW -> IN_REVIEW -> RESOLVED | DISMISSED`. Resolutions:
`ACCOUNT_EXISTS`, `NEW_ACCOUNT_NEEDED`, `BUSINESS_CLOSED` (sales-tax items
only), `RELOCATION`, `OWNERSHIP_CHANGE`, `DUPLICATE`, `NO_TAXABLE_BPP` (new
business items only), `FALSE_MATCH`, `OTHER`.

`app/comptroller/intelligence.py::investigate_item/resolve_item/dismiss_item`
are the **only** write paths these actions use, and they only ever touch
`bpp_intelligence_items`'/`comptroller_closure_reviews`' own status/
resolution/notes/reviewer columns -- verified by
`tests/test_comptroller_intelligence.py::test_resolving_never_touches_appraisal_fields`,
which asserts every PATCH payload sent is a subset of
`{status, resolution, resolution_notes, reviewed_by, reviewed_at, assigned_to}`.
No property value, appraisal status, ownership, account status, BPP record,
or exemption field is ever in scope of these functions.

## Intelligence Queue UI

`frontend/index.html`, new **BPP Intelligence** tab (admin-gated, same as
the existing Admin tab): summary tiles (New / High Priority / Needs Review /
Resolved), filters (signal type, status, confidence, city), a card list
(business name, address, detected date, match summary, priority), and a
detail panel showing the Comptroller evidence, the matched RenditionPilot
record (if any), the full per-signal breakdown, and
Investigate/Resolve/Dismiss actions with a resolution picker and notes
field. No page for editing appraisal data exists here or is reachable from
here.

## API

All gated by `require_district_admin`, scoped to the caller's own district
(`403` on cross-district access -- see
`tests/test_backend_intelligence_queue.py`):

- `POST /api/admin/intelligence/summary`
- `POST /api/admin/intelligence/queue` (filters: `signal_type`, `status`,
  `confidence`, `city`)
- `POST /api/admin/intelligence/item` (`source_table`, `item_id`)
- `POST /api/admin/intelligence/investigate`
- `POST /api/admin/intelligence/resolve` (+ `resolution`, `resolution_notes`)
- `POST /api/admin/intelligence/dismiss` (+ `resolution_notes`)

## Manual commands (also runs automatically -- see below)

```bash
# Dry run: report classification counts, write nothing
python -m app.comptroller.cli detect-new-business --jurisdiction lubbock --dry-run

# Real run: create/update intelligence items
python -m app.comptroller.cli detect-new-business --jurisdiction lubbock

# Re-run against already-evaluated permits too (e.g. after a matching fix)
python -m app.comptroller.cli detect-new-business --jurisdiction lubbock --reevaluate

# Dispatcher: runs every intelligence module a jurisdiction has enabled
# (today: just new_business_detection; the extension point for future modules)
python -m app.comptroller.cli run-intelligence --jurisdiction lubbock --dry-run
```

**A Render Cron Job (`bpp-intelligence-daily`) runs `run-intelligence` for
Lubbock automatically every day, as of 2026-08-24** -- scheduled 30 minutes
after `comptroller-daily-sync` so it evaluates that day's freshly-synced
permit data. Idempotent by design (`new_business_evaluated_at` marks
already-evaluated permits), so it's safe if it ever overlaps a manual run.
The existing sales-tax `comptroller-daily-sync`/`comptroller-month-end` cron
jobs are unaffected. See `render.yaml` for the exact schedule/command.

## Tests

`tests/test_comptroller_jurisdictions.py`, `test_comptroller_cad_adapter.py`,
`test_comptroller_new_business.py` (the core detection/classification/dedup
logic), `test_comptroller_intelligence.py` (unified queue + actions),
`test_backend_intelligence_queue.py` (authorization/cross-district
isolation), plus additions to `test_comptroller_matching.py` for the signal
breakdown and name-divergence features. All mocked -- no live network/DB
calls in the suite, following this repo's established convention.

## Known limitations

- **HIGH confidence, and therefore `EXISTING_ACCOUNT_HIGH_CONFIDENCE`
  suppression, is unreachable** without a second corroborating signal
  RenditionPilot doesn't have yet (address, cross-referenced ID). Every
  matched new-business candidate becomes a reviewable item today, even an
  exact name match -- more items than the spec's ideal envisioned, directly
  caused by the address-data gap.
- **Relocation and ownership-change are not distinct signal types in this
  pass.** `name_signals_diverge` is a real, implemented hint, but confirming
  either requires address/property data this app doesn't have. Both remain
  future signal types per the roadmap.
- **No real property/CRS data exists anywhere in RenditionPilot.**
  `find_property_by_situs`/`get_real_property` always return empty/`None`.
  The example in the spec (Comptroller address -> CRS -> R-account -> BPP
  search) cannot be run against real data; the adapter interface exists so
  it can be wired in once such data exists.
- **New Business Detection has nothing to match against in production
  today**, for the same reason as the sales-tax closure monitor:
  `parsed_rendition_results` has zero real rows (see
  `docs/comptroller_closure_monitor.md`). Every real candidate is
  `NO_ACCOUNT_FOUND` until that separate persistence gap is closed.
