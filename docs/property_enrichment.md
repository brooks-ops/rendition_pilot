# Property Enrichment

Answers, for any jurisdiction: *given a business/BPP address, what real-property
record is most likely associated with it?* Built as shared infrastructure to
strengthen the [BPP Intelligence Queue](bpp_intelligence_queue.md) -- Lubbock
is the first *configured* jurisdiction, not a special case baked into the
matching engine.

## Why this exists

New Business Detection's matcher (`app/comptroller/matching.py`) has exactly
one signal today: business-name similarity. `EXISTING_ACCOUNT_HIGH_CONFIDENCE`
was, by design, structurally unreachable -- a single uncorroborated name match
is never "confirmed." Property Enrichment adds a genuinely independent second
signal (address) from a source RenditionPilot's own account data has never
had: a county property/CRS export.

## What's real vs. what's a documented limitation

**No real Lubbock property/CRS data exists in this codebase or in production
today.** `real_property_records` is created empty by this migration and stays
empty until a real Lubbock export is loaded via
`python -m app.comptroller.cli property-import`. Every jurisdiction's
`real_property_linkage` capability defaults to disabled for the same reason
New Business Detection's cron was never auto-enabled: this ships as working,
tested infrastructure, not a live production signal, until someone loads real
data and flips the flag on purpose.

A real, unrelated production data gap was found and fixed while validating
this: the Texas Comptroller dataset splits a permit's street number
(`address_number`, e.g. `"3612"`) from its street name (`address_text`, e.g.
`"122ND ST"`) into two separate fields. `app/comptroller/client.py` had only
ever read `address_text` -- harmless while address was never used for
matching (name-only matching never needed it), but it would have silently
capped every Property Enrichment match at "street name matches, street number
unknown" forever. Fixed to recombine both fields (`client.py::_parse_row`),
with a regression test (`tests/test_comptroller_client.py`) covering both the
combined and no-address-number cases.

## Architecture

```
Business / BPP Address
        v
Normalized Situs Address     (address_normalizer.py)
        v
Real-Property Record         (property_adapter.py, real_property_records table)
        v
PropertyID / Real Account Number
        v
TUG / Neighborhood / Map ID
        v
Suggested Property Link      (advisory only -- never auto-applied)
```

### Normalized property model (`app/comptroller/property_adapter.py`)

`NormalizedRealProperty`: `property_id`, `jurisdiction_id`, `source_property_id`,
`real_account_number`, `situs_address_raw`/`situs_address_normalized`,
`situs_city`/`situs_state`/`situs_zip`, `owner_name`, `tug`, `neighborhood`,
`map_id`, `latitude`/`longitude`, `source_system`, `source_import_id`,
`source_updated_at`. Every field but `property_id`/`jurisdiction_id`/
`source_property_id` is nullable -- a county whose export has no TUG column
still works, it just never populates that field (spec item 17: "the entire
feature must still work" without Lubbock-specific fields).

### Adapter pattern (`property_adapter.py`)

`ImportedPropertyAdapter` is the one real adapter, mirroring
`cad_adapter.RenditionPilotCadAdapter`'s "one adapter, config not code"
pattern: `get_property_by_id`, `find_properties_by_address`,
`find_properties_by_real_account`, `search_properties`. County-specific raw
column names (`PropertyID`, `QuickRefID`, `TUG`... for Lubbock; anything else
for another county) are read in exactly one place --
`normalize_source_record()` -- via `jurisdiction.property_field_mapping`.
Nothing downstream (matching, intelligence, UI, CLI) ever sees a raw column
name. `get_property_adapter(jurisdiction)` is the factory/extension point for
a future jurisdiction with a live CRS database connection instead of a file
export.

### File import, not a live CRS connection (`property_import.py`)

RenditionPilot has no live database access to any CAD's CRS system --
`import_property_csv()` reads a CSV export, maps it via
`property_field_mapping`, and upserts into `real_property_records`, versioned
by a `property_source_imports` row (`source_as_of_date`, `imported_at`,
`row_count`). Onboarding a second county's property data means: (1) supply an
export, (2) write its `property_field_mapping`, (3)
`validate_capability(..., "real_property_linkage", ...)` confirms the mapping
is usable, (4) run the import. No new adapter class, no code change --
proven by `tests/test_property_portability.py`'s second mock jurisdiction
(`ParcelKey`/`AccountRef`/`PhysicalAddress`/... column names) reusing the
identical `normalize_source_record()`/`ImportedPropertyAdapter` unchanged.

### Address normalization (`address_normalizer.py`)

One shared normalizer (new -- `new_business.py`/`matching.py` never had one,
since name-only matching didn't need it). Expands street-suffix
(`ST`→`STREET`, `RD`→`ROAD`, `AVE`→`AVENUE`, `BLVD`→`BOULEVARD`, `DR`→`DRIVE`,
`LN`→`LANE`, `HWY`→`HIGHWAY`, `FM`→`FARM TO MARKET ROAD`, ...) and directional
(`N`→`NORTH`, ...) abbreviations, strips punctuation/whitespace, splits a
trailing ZIP(+4), and separates a suite/unit marker (`STE`/`SUITE`/`UNIT`/
`APT`/`#`/...) from the base address. `NormalizedAddress` keeps both `raw`
and `normalized` -- the original string is never discarded.

### Matching (`property_matching.py`)

`score_property_candidate()` scores street number (exact), street name
(exact/normalized/partial via `difflib`), ZIP, and suite signals
independently; `match_property()` classifies:

| Classification | Requires |
|---|---|
| `EXACT_PROPERTY_MATCH` | exact street number + exact street name + (ZIP matches or unavailable) + no suite conflict |
| `STRONG_PROPERTY_MATCH` | exact street number + exact/normalized street name, ZIP not contradicting |
| `POSSIBLE_PROPERTY_MATCH` | partial street match, or an exact match downgraded by a suite conflict |
| `AMBIGUOUS_PROPERTY_MATCH` | two or more candidates score within 0.05 of each other -- never auto-picked |
| `NO_PROPERTY_MATCH` | nothing scored close enough |

A suite conflict (`123 MAIN ST STE 100` vs. a permit at `STE 200`) always
caps the result at `POSSIBLE`, never `EXACT`/`STRONG`, regardless of how well
everything else matches (spec item 9). A missing suite on one side (a
single-tenant building, or a permit that just didn't list one) is treated as
"can't fully corroborate" rather than "conflict."

## Making HIGH confidence reachable (spec item 14)

`match_closure_to_account()` (`matching.py`) gained one optional parameter,
`property_match`. Confidence is upgraded from MEDIUM to **HIGH** only when
**all** of the following hold:

1. The name match is unambiguous and strong (existing MEDIUM-tier logic,
   unchanged).
2. Property Enrichment resolved the permit's address to `EXACT_PROPERTY_MATCH`
   or `STRONG_PROPERTY_MATCH`.
3. That property record's `real_account_number` equals the matched
   RenditionPilot candidate's `account_number`.

Address match alone is never enough; name match alone is never enough. A
jurisdiction with `property_match=None` (the default -- no property data
loaded) gets byte-for-byte the same unreachable-HIGH behavior as before this
feature existed. See `tests/test_new_business_property_integration.py` for
all four combinations (both signals agree -> HIGH; property matches but name
doesn't -> not HIGH; name matches but no property data -> not HIGH; both
"match" but the account numbers disagree -> not HIGH).

## Integration with New Business Detection (spec item 13)

`run_new_business_detection()` checks `jurisdiction.has_capability("real_property_linkage")`
and `validate_capability()` before doing anything else; if either fails, it
silently falls back to the exact pre-existing name-only behavior -- Property
Enrichment can never break or change behavior for a jurisdiction that hasn't
configured it. When enabled, every candidate with a Comptroller address is
run through `property_enrichment.run_property_enrichment()`, and the result
is threaded into `match_closure_to_account(property_match=...)`. Property
evidence (`property_match_status`, `matched_address`, `property_account_number`,
`tug`, `neighborhood`, `map_id`) is stored on the `bpp_intelligence_items`
row so a reviewer sees exactly what corroborated (or didn't) -- reusing the
two columns (`matched_address`, `property_account_number`) already reserved,
unused, on that table since the BPP Intelligence Queue migration.

## Same-property BPP account cross-reference (spec item 15)

`property_enrichment.same_property_accounts()` cross-references a matched
property's `real_account_number` against every RenditionPilot `MatchCandidate.account_number`
in a jurisdiction (loose, punctuation/case-insensitive comparison). This is
the shared primitive future relocation/duplicate-detection modules would call
-- not wired into a UI action in this pass, since only New Business Detection
consumes Property Enrichment today.

## Caching, versioning, and staleness (spec items 22/23)

`property_enrichment_results` holds one row per subject
(`jurisdiction_id, subject_type, subject_id`). A re-run reuses the cached
result when the normalized input address is unchanged **and** no newer
`property_source_imports` row exists for the jurisdiction; otherwise it
recomputes and updates the row in place. A refresh never overwrites
`review_status` (a human's ACCEPTED/REJECTED decision, once that workflow is
wired up) -- the upsert payload deliberately omits that column so PostgREST's
merge-duplicates resolution leaves it untouched.

## Intelligence Queue + Property Lookup UI (spec items 18/19)

- The BPP Intelligence detail view (`frontend/index.html`) gained a fourth
  "Property Enrichment" panel showing the same match/confidence/TUG/
  neighborhood/map fields, with explicit `NONE`/`AMBIGUOUS` states -- never
  silently blank.
- A new standalone **Property Lookup** tab (`BPP > Property Lookup`,
  `/api/admin/property/lookup`) lets staff test an address independently of
  any intelligence item -- the diagnostic tool spec item 19 asks for.

## CLI (spec item 20)

```bash
python -m app.comptroller.cli property-import --jurisdiction lubbock --file lubbock_properties.csv
python -m app.comptroller.cli property-import --jurisdiction lubbock --file lubbock_properties.csv --dry-run
python -m app.comptroller.cli property-enrich --jurisdiction lubbock --address "5807 88TH PL" --zip 79424
```

Batch enrichment across New Business candidates already happens automatically
inside `detect-new-business` (one property fetch, reused for every candidate,
mirroring the existing BPP-account caching pattern) -- a separate generic
"batch-enrich arbitrary record type" CLI was not built in this pass, since
`NEW_BUSINESS_CANDIDATE` is the only subject type with a real caller today;
`BPP_ACCOUNT`/`RENDITION` batch enrichment has no data to run against until
RenditionPilot's own accounts carry addresses (still zero real rows in
production, per `matching.py`'s module docstring).

## Portability proof (spec item 29, mandatory)

`tests/test_property_portability.py` defines a second mock jurisdiction with
completely different raw column names (`ParcelKey`, `AccountRef`,
`PhysicalAddress`, `TaxArea`, `NeighborhoodCode`, `MapNumber`) and proves:
the same `normalize_source_record()` function, the same
`ImportedPropertyAdapter` class, and the same `validate_capability()` logic
all work unchanged -- only the `property_field_mapping` config differs. The
second jurisdiction's export also deliberately omits an optional field
(`situs_zip`) to prove graceful degradation.

## Safety

Property Enrichment reads Comptroller data and a county's own property
export, normalizes, matches, and writes only to
`property_enrichment_results` (and, via New Business Detection, evidence
columns on `bpp_intelligence_items`). It never writes to `real_property_records`
outside of `property_import.py`'s own import path, and never touches CAD
ownership, situs, mailing address, property classification, BPP status,
appraisal value, R-account linkage, TUG, neighborhood, or map assignment on
any official record. A "Suggested Property Link" is advisory review data --
applying it to a real BPP account remains a human decision through
RenditionPilot's normal tools.

## New Account Enrichment (the final leg of the pipeline)

`app/comptroller/new_account_enrichment.py` completes the pipeline described
at the top of this doc with the two pieces that didn't already exist:

- **Appraiser assignment** (`assign_appraiser()`): a jurisdiction-configurable
  `appraiser_assignment_rules` jsonb mapping (`by_tug`, `by_neighborhood`,
  `default`), checked in that precedence order -- TUG is the more specific
  unit in the pipeline (PropertyID -> R account -> TUG -> Neighborhood ->
  Map), so it's checked first. No real Lubbock assignment rules exist yet
  (same honesty as `property_field_mapping`), so every card shows
  `basis="unassigned"` until a CAD supplies real rules.
- **Account card** (`build_account_card()`/`generate_account_card()`): bundles
  business identity, the Property Enrichment result, and the appraiser
  assignment into one staff-facing summary. This is a **report, not a new
  database entity** -- everything in it is reconstructible on demand from the
  intelligence item, `real_property_records`, and the appraiser mapping, so
  there's nothing to go stale or need its own audit trail beyond
  `bpp_intelligence_items.account_card_generated_at` (an additive, nullable
  timestamp marking when a card was last generated for that item).

Only applies to `new_business` signal-type items -- `NEW_ACCOUNT_NEEDED` is a
resolution outcome specific to New Business Detection
(`RESOLUTION_OPTIONS_BY_SIGNAL_TYPE` in `intelligence.py`), not something the
sales-tax closure monitor produces; `build_account_card()` raises for any
other signal type rather than fabricating a card for a request that doesn't
make sense.

**Never creates a BPP account.** The card is advisory output for a human to
use when manually creating the account in the CAD's real system -- exactly
like Property Enrichment's "Suggested Property Link." Exposed via
`python -m app.comptroller.cli account-card --jurisdiction lubbock --item-id <id>`,
`POST /api/admin/intelligence/account-card`, and a "Generate Account Card"
button in the Intelligence detail view (shown only for `new_business` items).

## Known limitations

- No real property/CRS data is loaded for any jurisdiction as of this
  writing -- everything above is validated against synthetic, clearly-labeled
  fixtures (see the deliverables report's Real Lubbock Validation section)
  until a real export is provided.
- `find_properties_by_address` does a prefix (`ilike`) match on the
  normalized base address; a genuinely malformed or wildly misspelled input
  address that doesn't share a prefix with its real property record won't be
  found. Fuzzy string-distance search across the whole property table (like
  name matching does) was not built in this pass -- a possible future
  refinement if this proves to matter once real data exists.
- HIGH confidence corroboration requires the RenditionPilot candidate's own
  `account_number` field to actually be a real CAD account number (populated
  from OCR of the printed rendition, per `pipeline.py::_extract_metadata`) --
  if that field is ever blank or non-standard for a given rendition, the
  corroboration path can't fire for that record, same as any other missing
  signal.
