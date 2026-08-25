# Mailing Address Intelligence

Answers, for any jurisdiction: *has this taxpayer's mailing address probably
changed, what evidence supports that, and does a human need to update the CAD
record?* Built as a fourth signal type on the existing generalized
[BPP Intelligence Queue](bpp_intelligence_queue.md) -- not a new review
system, and not a Lubbock-specific checker.

RenditionPilot never says "these two address strings are different." It says:
this account appears to have a new mailing address, here is the old one, here
is the new one, where the new information came from, what materially changed,
how confident RenditionPilot is that it belongs to the same account, and why
it deserves review.

## Situs vs. mailing address -- kept architecturally separate

Property Enrichment's `NormalizedAddress` (`address_normalizer.py`) matches
*situs* addresses -- it deliberately drops everything after the first comma,
which is correct for "is this the same physical parcel" but would silently
discard a mailing address's load-bearing city/state. Mailing address
intelligence uses a genuinely separate normalizer,
`NormalizedMailingAddress`/`normalize_mailing_address()`, in the same file.
The two are never unified and never substitute for each other -- a business's
situs address is never treated as evidence about its mailing address.

`NormalizedMailingAddress` understands PO Box variants (`PO Box`, `P.O. Box`,
`P O BOX #123` all fold to one canonical form), street-suffix/directional
abbreviations (reusing the same expansion tables as the situs normalizer),
suite/unit markers, and ZIP vs. ZIP+4 -- and keeps city/state/zip as
first-class fields rather than folding them into one opaque string, since
"same street, different state" must never be scored as a formatting
difference.

## What mailing-address evidence exists today

Verified by live query against the Texas Comptroller dataset (`3kx8-uryv`),
not assumed: a permit record carries **two independently-sourced addresses**
-- `tp_address`/`tp_city`/`tp_state`/`tp_zip`/`tp_zip4` (the taxpayer's
mailing address, TAXPAYER-level, shared across every outlet/location that
taxpayer has) and `address_number`+`address_text`/`loc_city`/`loc_state`/
`loc_zip` (the outlet's SITUS address, already consumed by Property
Enrichment). These genuinely differ in real data -- e.g. Foot Locker, The
Buckle, Denny's, and Redbox all show an out-of-state PO Box mailing address
against a Lubbock street situs address. `client.py::_parse_row()` previously
discarded `tp_address` entirely; it's now parsed into `PermitRecord.mailing_*`
fields and threaded through `service.py`'s batch payload.

**Rendition OCR does not extract a mailing address today.** The literal
string "mailing address" is used only as a noise-filter term and a
page-classification signal in the extraction pipeline -- never captured as a
value. Adding real extraction was out of scope for this pass: it would touch
the production OCR path real appraisers depend on today, with no real
rendition PDFs available to validate a new regex against, and the original
spec itself frames rendition-driven detection as groundwork for *next*
season, not this one. The comparison engine and orchestration module are
built to support it (`account_identifier_type="bpp_account"` is a first-class
option throughout), but nothing calls that path yet. This is a deliberate,
flagged scope decision, not an oversight -- see Known Limitations.

**No other mailing-address source exists yet** (no returned-mail feed, no
county-clerk DBA data, no manual import tool).

## Comparison engine (`app/comptroller/mailing_address_matching.py`)

Pure function, no I/O: `compare_mailing_addresses(current_raw, ..., observed_raw, ...) -> MailingAddressComparison`.
Classifies into:

| Classification | Meaning |
|---|---|
| `SAME_ADDRESS` | Normalized forms are identical |
| `FORMAT_ONLY_DIFFERENCE` | Only punctuation/case/suffix spelling/ZIP+4-only differs -- never alert on this alone |
| `POSSIBLE_CHANGE` | A component differs but only on one side (e.g. a suite present in one observation, absent in the other) |
| `LIKELY_CHANGE` | A component genuinely differs on both sides (street, city, state, ZIP5, PO Box number, suite number) |
| `INSUFFICIENT_DATA` | Not enough data on one or both sides to compare at all |

Every result carries a component-level `differences` breakdown (street
number, name, suffix, directional, unit/suite, PO box, city, state, ZIP,
ZIP+4) and a plain-English `reasons` list -- never just a similarity score.

The unit/suite component has its own dedicated diff helper
(`_unit_diff`), separate from the generic per-field diff, because "no suite"
(`None`) is a real, comparable value there -- unlike a missing city or ZIP,
which means "unknown," a missing suite means "this address has none." A
generic diff would have reported a newly-added suite as "not available"
instead of "changed," making the addition invisible to the classifier; this
was a real bug found and fixed while building this (see git history for
`mailing_address_matching.py`).

`full_normalized` (the single-string "are these the same address" check)
includes the unit -- omitting it was a second real bug found the same way,
which would have short-circuited every suite-only change to `SAME_ADDRESS`
before the suite-diff branch ever ran.

## Identity vs. change confidence (spec item 29)

Kept as two separate questions, combined only at the end via
`_combine_confidence()`, which takes the **weaker** of the two (by rank
`HIGH > MEDIUM > LOW > NONE`). A perfect address difference attached to the
wrong account is useless, so a weak identity match always caps the combined
result -- it can never be rescued by a strong address signal.

For the Comptroller-vs-own-history path (the only one that runs today),
identity confidence is `HIGH` by construction: the comparison is always
against RenditionPilot's own prior observation for the *same*
`taxpayer_id`, so there's no fuzzy account matching involved at all. The
function is written generically to accept a weaker identity confidence for a
future path (e.g. rendition-vs-BPP-account, which would need real identity
evidence from `matching.py` -- exact account number, strong name match plus
corroboration, etc. -- never bare name similarity, per spec item 14).

## Source trust (`SOURCE_TRUST`, config not code)

A lookup table, not hardcoded logic:

| Source | Trust |
|---|---|
| `RENDITION_SUBMITTED_BY_TAXPAYER` | HIGH -- a taxpayer's own sworn statement |
| `CAD_CURRENT_RECORD` | baseline |
| `TEXAS_COMPTROLLER` | MEDIUM -- self-reported registration data |
| `MANUAL_IMPORT` | configurable |

This is what separates `CONFIRMED_MAILING_ADDRESS_CHANGE` (from a HIGH-trust
source) from `LIKELY_MAILING_ADDRESS_CHANGE` (from a MEDIUM-trust source) in
the final classification -- no source is ever assumed universally
authoritative.

## How detection runs today

`run_mailing_address_intelligence(jurisdiction_id, dry_run=False)`
(`mailing_address_intelligence.py`) does the only thing there's real data
for right now: for every Comptroller taxpayer with a mailing address, compare
it against RenditionPilot's own most recent prior observation for that same
`taxpayer_id` (mirroring New Business Detection's `first_seen_at`
snapshot-comparison philosophy, not trusting any source's own "this changed"
flag, since none exists). A material difference creates or updates a
`mailing_address_change` item on the existing `bpp_intelligence_items` table;
a non-material difference or first-ever sighting just records the
observation and moves on.

Candidates are de-duplicated by `taxpayer_id` before comparison, since the
Comptroller dataset is location-row-granular but the mailing address field is
taxpayer-level -- one taxpayer with five outlets must never generate five
redundant comparisons.

## History (`mailing_address_observations`)

Append-only. A unique index on `(jurisdiction_id, account_identifier_type,
account_identifier, source, normalized_full_address)` with
`resolution=ignore-duplicates` upsert semantics means re-observing the same
address is a cheap no-op, while a genuinely different address always inserts
a new row -- full sequential history is preserved even though the V1 UI
doesn't have a dedicated timeline view yet (a lightweight Mailing Address
Lookup view per account is a possible next increment, not built this pass).

`observed_at` (when RenditionPilot processed the source) and
`source_effective_date` (the source's own as-of date, e.g. a rendition's
signed date) are kept as separate columns -- RenditionPilot never invents an
effective date a source doesn't actually supply. `source_effective_date` is
`NULL` for every row today, since the only live source (Comptroller) doesn't
supply one.

## Deduplication that doesn't suppress real sequential changes (spec items 19/20)

Intelligence-item dedup keys on `f"{taxpayer_id}:{observed_full_normalized}"`
-- not just the taxpayer. Re-processing the same source/address updates the
existing item's evidence rather than creating a duplicate, but a **second,
different** address change for the same taxpayer is a new dedup key and
therefore its own new, independently auditable item. An earlier resolved
alert never suppresses a later, genuinely different one.

## Intelligence Queue integration

Routed through the exact same `bpp_intelligence_items` table as
`new_business` and `sales_tax_inactive` -- no new review system.
`RESOLUTION_OPTIONS_BY_SIGNAL_TYPE["mailing_address_change"]` is
`ADDRESS_UPDATED`, `CURRENT_ADDRESS_CORRECT`, `SOURCE_OUTDATED`,
`DUPLICATE_SIGNAL`, `ACCOUNT_MISMATCH`, `INSUFFICIENT_EVIDENCE`, `OTHER`.

**`ADDRESS_UPDATED` means a staff member recorded that outcome in
RenditionPilot -- it does not mean RenditionPilot changed anything in the
CAD system.** RenditionPilot never writes to an official CAD mailing
address, ever, under any resolution.

The Intelligence detail view (`frontend/index.html`) shows a dedicated
Mailing Address comparison panel for this signal type (Current on file / New
Observation + source + detected date / component-level differences /
classification + confidence / recommended action), rather than reusing the
generic new-business three-panel layout, which references fields (matched
owner name, property enrichment) that don't apply here.

## Batch CLI

```bash
python -m app.comptroller.cli mailing-address-scan --jurisdiction lubbock --dry-run
python -m app.comptroller.cli mailing-address-scan --jurisdiction lubbock
python -m app.comptroller.cli run-intelligence --jurisdiction lubbock   # dispatches here automatically once the capability is enabled
```

`--dry-run` writes nothing (no observation rows, no intelligence items) and
reports every bucket: accounts evaluated, same-address, format-only,
likely-change, insufficient-data, and duplicates suppressed. Scoped by
jurisdiction and using indexed lookups against already-paginated Comptroller
data -- no N-by-full-table-scan, so this scales the same way
`detect-new-business` already does across thousands of permits per county.

## Configuration

New jurisdiction capability: `mailing_address_monitoring` (jsonb key on
`jurisdictions.capabilities`; already present, seeded `false`, unused until
this feature). Requires `comptroller_county_code`, same as every other
Comptroller-dependent capability. **Disabled for every jurisdiction by
default, including Lubbock** -- turning it on is a deliberate config change,
not a side effect of this migration.

A county without a reliable external mailing source can still eventually use
rendition-driven comparison once OCR extraction exists; a county with no
current CAD mailing address at all correctly reports `NOT_READY`/`BLOCKED` in
the readiness diagnostic rather than manufacturing intelligence from
incomplete inputs.

## Safety

May: read Comptroller data, normalize, compare, store observations, create
`bpp_intelligence_items` rows, store human review outcomes (reviewer,
timestamp, resolution, notes).

May never: modify an official CAD mailing address, ownership, status, situs
address, or any other property record; auto-mark any address authoritative;
auto-resolve conflicting evidence; create an official CAD account. Every
output is a review recommendation -- enforced architecturally by which
tables this module ever writes to (`mailing_address_observations` and
`bpp_intelligence_items` only), not by a runtime check.

## Requires

`supabase/migrations/20260827_mailing_address_intelligence.sql` applied --
**not yet applied to production as of this writing.**

## Known limitations

- **Rendition-driven comparison has no real data path yet** -- OCR doesn't
  extract a mailing address. The orchestration module and comparison engine
  both already support `account_identifier_type="bpp_account"` for this
  exact future case; nothing calls it automatically today, and no hook was
  added to `backend/main.py`'s rendition-lock path (deliberately -- adding
  OCR extraction is a production-pipeline change that deserves its own pass
  with real rendition PDFs to validate against, not a rider on this one).
- **No real production data exists for this feature yet.** `mailing_address`
  and friends on `comptroller_permit_locations` stay `NULL` until a fresh
  `python -m app.comptroller.cli sync` runs after this migration is applied.
  The very first `mailing-address-scan` run for any jurisdiction will
  establish a baseline and report zero changes, by construction -- there is
  nothing to compare against yet.
- **Only one external source exists** (Texas Comptroller, MEDIUM trust). No
  returned-mail/undeliverable feed, no county-clerk DBA/assumed-name data, no
  manual-import tool -- all listed as `NOT_CONFIGURED` in the readiness
  diagnostic rather than silently absent.
- No dedicated Mailing Address Lookup/history UI exists yet -- history is
  captured in `mailing_address_observations` and preserved, but the only way
  to see it today is the Intelligence Queue item itself (Current vs.
  Observed) or a direct query.
