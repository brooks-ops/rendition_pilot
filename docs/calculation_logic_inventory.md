# Calculation Logic Inventory

Scope: this inventory documents valuation and calculation-related logic currently referenced from [app/streamlit_app.py](C:/Users/Brooks/Desktop/BPP/BPP%20Project/app/streamlit_app.py). This step does not move code or change behavior.

## Calculation imports used by `streamlit_app.py`

These are imported near the top of the file and provide most of the lower-level schedule math used by the UI:

- `app.depreciation.DepreciationEngine` at approximately line 26
- `app.rendition_calculator.SECTION_PRESETS` at approximately line 42
- `app.rendition_calculator.TABLE_METADATA` at approximately line 43
- `app.rendition_calculator.build_calculator_rows` at approximately line 44
- `app.rendition_calculator.build_flat_value_rows` at approximately line 45
- `app.rendition_calculator.build_saved_calculator` at approximately line 46
- `app.rendition_calculator.calculate_combined_total` at approximately line 47
- `app.rendition_calculator.calculate_section_total` at approximately line 48
- `app.rendition_calculator.generate_calculator_name` at approximately line 49
- `app.rendition_calculator.load_depreciation_tables` at approximately line 50
- `app.rendition_calculator.resolve_tax_year` at approximately line 51
- `app.review_workflow.get_recommended_value` at approximately line 61

## 1. Depreciation schedule related functions

### Local functions in `streamlit_app.py`

- `calculate_depreciated_value(...)` at approximately lines 1678-1687
  - Reads `Data/depreciation_schedule.csv`
  - Instantiates `DepreciationEngine`
  - Returns `(percent_good, depreciated_value)` from `engine.assess_value(...)`
  - Used by the manual assist "Historical Cost" workflow

- `render_manual_assist_panel(...)` at approximately lines 1706-1855
  - UI wrapper, but contains depreciation-related flow
  - Calls `calculate_depreciated_value(...)`
  - Applies manual override payload using `historical_cost`, `acquisition_year`, and `life_years`

- `build_manual_override(...)` at approximately lines 940-984
  - Builds the override payload for:
  - `attachment_total`
  - `good_faith_value`
  - `historical_cost`
  - `acquisition_year`
  - `life_years`
  - Relevant because one branch is "Force Historical Cost Less Depreciation"

### Imported helpers used by depreciation-related UI

- `load_depreciation_tables` imported at line 50 and used at approximately line 1861
- `TABLE_METADATA` imported at line 43 and used at approximately lines 1968-1969 and 2142-2143
- `build_calculator_rows` imported at line 44 and used at approximately lines 2021-2026
- `calculate_section_total` imported at line 48 and used at approximately line 2065
- `calculate_combined_total` imported at line 47 and used at approximately lines 2118 and 2223

## 2. Schedule A, B, C, D, and E calculation related functions

Note: `streamlit_app.py` does not currently define separate per-schedule functions for Schedule A, B, C, D, or E. The schedule-based calculator is generic and preset-driven.

### Generic schedule calculator orchestration

- `render_rendition_calculator(...)` at approximately lines 1858-2180
  - Main local orchestrator for the schedule calculator
  - Uses `SECTION_PRESETS` to select the schedule/category definition
  - Uses `load_depreciation_tables()` to load table data
  - Uses `build_calculator_rows()` for depreciation-table-backed schedules
  - Uses `build_flat_value_rows()` for flat-value sections
  - Recomputes each row value in-place with `row["value"] = round(row["cost"] * row["factor"], 2)` at approximately line 2059
  - Computes `section_total` with `calculate_section_total(rows)`
  - Computes saved-work aggregate with `calculate_combined_total(saved_calculators)`
  - Saves calculator payloads using `build_saved_calculator(...)`

### Session-state helpers that support the generic schedule calculator

- `get_calculator_store_key(...)` at approximately lines 1034-1036
- `get_calculator_editor_key(...)` at approximately lines 1039-1041
- `get_calculator_cost_key(...)` at approximately lines 1044-1046
- `get_saved_calculators(...)` at approximately lines 1049-1052
- `set_saved_calculators(...)` at approximately lines 1055-1056
- `build_default_calculator_editor(...)` at approximately lines 1059-1073
  - Defaults to `SECTION_PRESETS["schedule_a_furniture"]`
- `get_calculator_editor(...)` at approximately lines 1076-1082
- `save_calculator_editor(...)` at approximately lines 1085-1086
- `reset_calculator_editor(...)` at approximately lines 1089-1093
- `load_saved_calculator_into_editor(...)` at approximately lines 1096-1120
  - Resolves schedule/category back into a preset key

### Schedule E and pipeline-derived totals surfaced in this file

These are not calculated locally in `streamlit_app.py`, but the file reads and presents them from pipeline output:

- `show_top_metrics(...)` at approximately lines 1224-1298
  - Displays recommended value context only
- `show_flags_and_findings(...)` at approximately lines 1301-1427
  - Reads:
  - `result["schedule_e"]["total"]`
  - `result["schedule_values"]["good_faith_total"]`
  - `result["schedule_values"]["historical_cost_total"]`
  - `result["attachments"]["best_attachment_total"]`
- `build_batch_row(...)` at approximately lines 2446-2488
  - Reads the same pipeline-derived totals for batch output

## 3. Recommended value calculation related functions

### Local functions

- `get_result_value(...)` at approximately lines 1646-1653
  - Chooses the first available value in this order:
  - `assessment_summary.recommended_value`
  - `assessment_summary.recommended_market_value`
  - `assessment_summary.recommended_assessed_value`
  - `assessment_summary.extracted_value`

- `needs_manual_assist(...)` at approximately lines 1656-1665
  - Uses:
  - `assessment_summary.recommended_path`
  - `assessment_summary.confidence`
  - `get_result_value(...)`
  - review flags
  - Decides when the UI should force stronger manual review/override behavior

- `show_top_metrics(...)` at approximately lines 1224-1298
  - Displays the current recommended value and metadata
  - Uses the same fallback chain as `get_result_value(...)`
  - Reads `recommended_path`, `confidence`, `reason`, and `depreciated_override_result.percent_good`

- `apply_calculated_total_to_final_value(...)` at approximately lines 1123-1125
  - Does not change pipeline recommendation
  - Sets the appraiser-facing final value/source in session state to the saved calculator aggregate

- `finalize_review_panel(...)` at approximately lines 2219-2443
  - Seeds the final value from `get_recommended_value(result)`
  - Tracks the selected final source
  - Shows calculator combined total beside the final appraiser value
  - Allows final source options including:
  - `calculator_combined_total`
  - `manual_override`
  - `attachment_total`
  - `good_faith_value`
  - `historical_cost_depreciated`
  - `schedule_e_total`

### Imported helper used for recommended value

- `get_recommended_value` imported at line 61 and used at approximately line 2220
  - This appears to be the current helper that determines the default final review value before the appraiser edits it

## 4. Helper functions these calculations depend on

### Pure or mostly-pure local helpers

- `format_money(...)` at approximately lines 987-993
- `format_percent(...)` at approximately lines 996-1002
- `parse_money_input(...)` at approximately lines 1013-1019
- `prettify_path(...)` at approximately lines 1128-1141
- `prettify_confidence(...)` at approximately lines 1144-1152
- `confidence_color(...)` at approximately lines 1155-1160
- `extract_money_values(...)` at approximately lines 1668-1675
  - Used to convert freeform good-faith text entry into numeric line items

### State/persistence helpers tightly coupled to the calculator UI

- `get_session_district_slug(...)` at approximately lines 533-537
- `get_calculator_store_key(...)` at approximately lines 1034-1036
- `get_calculator_editor_key(...)` at approximately lines 1039-1041
- `get_calculator_cost_key(...)` at approximately lines 1044-1046
- `get_saved_calculators(...)` at approximately lines 1049-1052
- `set_saved_calculators(...)` at approximately lines 1055-1056
- `save_calculator_editor(...)` at approximately lines 1085-1086
- `reset_calculator_editor(...)` at approximately lines 1089-1093
- `load_saved_calculator_into_editor(...)` at approximately lines 1096-1120

### Pipeline/execution helpers that valuation flows call into

- `run_pipeline_from_upload(...)` at approximately lines 1627-1643
- `apply_manual_assist_override(...)` at approximately lines 1690-1703

## 5. Approximate line-number map by topic

### Depreciation/manual override cluster

- `build_manual_override(...)`: 940-984
- `calculate_depreciated_value(...)`: 1678-1687
- `render_manual_assist_panel(...)`: 1706-1855

### Calculator/session-state cluster

- `get_calculator_store_key(...)`: 1034-1036
- `get_calculator_editor_key(...)`: 1039-1041
- `get_calculator_cost_key(...)`: 1044-1046
- `get_saved_calculators(...)`: 1049-1052
- `set_saved_calculators(...)`: 1055-1056
- `build_default_calculator_editor(...)`: 1059-1073
- `get_calculator_editor(...)`: 1076-1082
- `save_calculator_editor(...)`: 1085-1086
- `reset_calculator_editor(...)`: 1089-1093
- `load_saved_calculator_into_editor(...)`: 1096-1120
- `apply_calculated_total_to_final_value(...)`: 1123-1125
- `render_rendition_calculator(...)`: 1858-2180

### Recommended value/finalization cluster

- `show_top_metrics(...)`: 1224-1298
- `get_result_value(...)`: 1646-1653
- `needs_manual_assist(...)`: 1656-1665
- `finalize_review_panel(...)`: 2219-2443
- `build_batch_row(...)`: 2446-2488

## 6. Suggested safe extraction order for later move into `core/valuation_engine.py`

1. Extract pure formatting/parsing/value-selection helpers first.
   - Candidate functions: `parse_money_input`, `extract_money_values`, `get_result_value`
   - Reason: lowest coupling to Streamlit session state and UI

2. Extract standalone depreciation helper next.
   - Candidate function: `calculate_depreciated_value`
   - Reason: already close to service-style logic, only depends on `PROJECT_ROOT` and `DepreciationEngine`

3. Extract manual override payload builders next.
   - Candidate function: `build_manual_override`
   - Reason: deterministic data-shaping logic with no UI side effects

4. Extract calculator row/total orchestration only after separating state from math.
   - First move math/data assembly behind pure functions in `core/valuation_engine.py`
   - Keep Streamlit-only state functions in `streamlit_app.py` temporarily
   - Relevant local entrypoint: `render_rendition_calculator(...)`
   - Relevant imported dependencies: `build_calculator_rows`, `build_flat_value_rows`, `calculate_section_total`, `calculate_combined_total`, `build_saved_calculator`

5. Extract final-value recommendation helpers after the calculator service boundary is stable.
   - Candidate logic: default value selection and source labeling now spread across `get_result_value`, `show_top_metrics`, and `finalize_review_panel`
   - Reason: this step should come after the calculator/output contract is defined

6. Leave Streamlit-only orchestration in place until last.
   - Keep these in `streamlit_app.py` until the pure/core API is settled:
   - `render_manual_assist_panel`
   - `render_rendition_calculator`
   - `finalize_review_panel`
   - calculator session-state helpers

## Summary

`streamlit_app.py` currently contains a mix of:

- Pure-ish value helpers
- Manual override and depreciation glue
- Session-state-backed calculator orchestration
- Display/finalization logic for recommended and appraiser-selected values

The actual reusable schedule math already appears to live mostly in `app.rendition_calculator`; the riskiest part to move later will be the Streamlit session-state orchestration around that math, not the imports themselves.
