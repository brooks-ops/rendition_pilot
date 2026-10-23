# Commercial workspace

The `/commercial` page provides account search, property inputs, Pro Forma,
cost/equity comparisons, category-update previews, persistent input overrides,
batch undo, adding properties from model templates, and combined model-summary
PDF packets. The main application links to it after sign-in. Its API resolves
the existing authenticated district before accessing any district data.

## Import and local preview

Install `requirements.txt` in the project environment. Import the supplied ZIP
into a private data directory outside the checkout:

```sh
python -m app.commercial.importer '/path/to/Rendition Pilot Models.zip' \
  --data-dir '/private/commercial-data' --district '<district-id>'
```

For the production application, `district-id` must be the actual district UUID
from the existing membership system. Set `COMMERCIAL_DATA_DIR` to the parent
directory, not the district directory. For a standalone local preview, import
with `--district lubbock-preview`, then run:

```sh
COMMERCIAL_DATA_DIR=/private/commercial-data \
  uvicorn app.commercial.preview:app --host 127.0.0.1 --port 8767
```

The preview accepts loopback clients only and is separate from production
authentication. Never serve that preview entrypoint publicly. The integrated
API uses `get_authenticated_district_context` and does not bypass sign-in.

The importer refuses to overwrite an existing import. Stage later workbook
versions in another private directory so source changes can be reconciled with
saved edits before a replacement. The original ZIP and workbooks are never
modified. Imported values, formula graphs, account metadata, SQLite data and
change history must remain private runtime data, not Git assets.

## Source behavior

- Nine explicit model definitions identify account, category, inputs and
  outputs. The account identifier remains text, normalized to `R` plus digits.
- Repeated source rows and memberships are retained. Values from different
  models or components are never silently summed into a single account value.
- Account-only lookup/reference rows remain in the source graphs but are
  excluded from the default working register and bulk edits.
- The apartment unit-mix inputs are separate from the summary Pro Forma.
  Canonical apartment results follow the median-rent calculation used by the
  source final-value formula; the actual-rent calculation remains separately
  mapped for traceability.
- Existing source lookup overrides remain part of the formula logic. The bulk
  update option for overrides refers specifically to saved workspace input
  overrides; it does not remove the workbook's final-value override tables.
- The cost workbook supplies cost/equity values, not a replacement-cost engine.
  Where verified against the source total, the workspace recalculates cost
  total as land plus improvements. Editing area alone does not manufacture a
  new replacement-cost value.
- One oversized historical office override worksheet is not imported.
  Missing external workbook dependencies and unsupported calculations remain
  unavailable. Cached formula errors are retained and exposed rather than
  replaced with zero.

Unchanged formulas use Excel's saved result. Changed dependencies are evaluated
by a bounded, non-executing formula interpreter. It supports the operators and
lookup/aggregation functions currently needed for these models and treats
unvalidated features as unavailable, even inside `IFERROR`. Operator ordering
follows [Microsoft's Excel documentation](https://support.microsoft.com/en-us/excel/calculation-operators-and-precedence-in-excel).

Creating a new property copies the selected template's current input overrides.
Same-row formula references are rebound to the new workspace row; category
schedule and comparison-range references stay with the selected template.
The appraiser must review these inherited assumptions. New properties do not
automatically expand the source workbook's historical comparison ranges.

## Saved changes

Preview and apply are separate operations. A preview contains the proposed
inputs and recalculated account impacts. The apply operation checks the
workspace revision inside a SQLite write transaction, saves the actor and
reason, and invalidates all outstanding previews. Previews expire after 30
minutes. Existing saved input overrides are excluded unless explicitly included.
Zero is preserved separately from a blank input. The newest active batch can be
undone; later work must be undone first. New-property creation is also audited
and can be undone.

All affected model rows are recalculated for a batch, including formula
dependents outside the selected category. The change history stores the before
and after data for these dependent rows so undo remains consistent.

## Packet scope and production readiness

Packets currently contain model summaries, source references, notes and selected
equity comparisons. They are **not yet complete ARB hearing packets**: source
packet-sheet layouts, photos, notices, outside evidence, attachment merging,
property-specific appraiser notes and final approval are follow-up work.

The local implementation uses one SQLite file and private formula graphs per
district. Production needs a durable disk configured for `COMMERCIAL_DATA_DIR`,
or a database/storage adaptation, plus a reviewed source import. Do not deploy
with ephemeral local storage. Multi-instance database concurrency and district
backup/restore need to be established before shared production use.

The tax-year labels currently reflect the supplied 2026 workbooks. A tax-year
rollover and source-version reconciliation UI remain future work. The initial
formula checks compare representative downstream results with the workbooks'
saved results; a complete native-Excel reconciliation of every property has
not been performed.

## Verification

```sh
python -m pytest tests/test_commercial_workspace.py \
  tests/test_backend_intelligence_queue.py
```

Tests cover recalculation, lookup error isolation, unavailable dependencies,
preview/apply/undo, stale preview rejection, existing overrides, zero inputs,
empty selections, audited new-property creation and template inheritance.
