-- Property Enrichment layer.
--
-- Answers, for any jurisdiction: "given a business/BPP address, what real
-- property record is most likely associated with it?" Built as shared
-- infrastructure -- Lubbock is the first *configured* jurisdiction, not a
-- special case. A second county onboards by supplying a property export and
-- a `jurisdictions.property_field_mapping`, not by new feature development.
--
-- No real Lubbock CRS/property export exists in this codebase today (see
-- docs/property_enrichment.md). This migration creates the normalized
-- storage + import-versioning tables so a real export can be loaded via
-- `python -m app.comptroller.cli property-import` whenever one becomes
-- available; until then `real_property_records` stays empty and every
-- jurisdiction's `real_property_linkage` capability stays disabled, exactly
-- like New Business Detection shipped with no cron enabled until asked for.
--
-- Deliberately additive-only everywhere else: `jurisdictions` gains one
-- nullable-default jsonb column, `bpp_intelligence_items` gains five
-- nullable columns. Nothing about New Business Detection, the sales-tax
-- closure monitor, or the existing intelligence queue changes shape.

create extension if not exists pgcrypto;

-- Per-jurisdiction mapping from normalized property fields to that county's
-- raw export/CRS column names, e.g.
-- {"source_property_id": "PropertyID", "real_account_number": "QuickRefID",
--  "situs_address": "SitusAddress", "situs_zip": "SitusZip", "tug": "TUG",
--  "neighborhood": "NBHD", "map_id": "MapID"}.
-- Checked by app/comptroller/jurisdictions.py::validate_capability() under
-- the "real_property_linkage" capability -- the same required/optional
-- distinction New Business Detection already uses for cad_field_mapping.
alter table public.jurisdictions
  add column if not exists property_field_mapping jsonb not null default '{}'::jsonb;

-- The jurisdiction's configured "current working tax year" for property
-- matching (spec item 5): when set, matching prefers a property record from
-- this year; when null (the default -- no jurisdiction has set one yet),
-- matching falls back to the newest tax_year available for that property.
alter table public.jurisdictions
  add column if not exists current_tax_year integer;

-- One row per property-data import batch, so every real_property_records
-- row (and every enrichment result computed from it) can be traced back to
-- "which export, as of when" -- required for cache invalidation (an older
-- enrichment result must not silently survive a newer import) and for
-- audit (a reviewer's decision must remain explainable after the source
-- data has moved on).
create table if not exists public.property_source_imports (
  id uuid primary key default gen_random_uuid(),
  jurisdiction_id uuid not null references public.jurisdictions(id) on delete cascade,

  source_system text not null default 'imported_file',
  source_as_of_date date,
  imported_at timestamptz not null default now(),
  row_count integer not null default 0,
  notes text,

  created_at timestamptz not null default now()
);

create index if not exists property_source_imports_jurisdiction_idx
on public.property_source_imports (jurisdiction_id);

-- Normalized real-property records. County-specific column names never
-- leak past import time -- app/comptroller/property_adapter.py's
-- normalize_source_record() maps a raw export row into this shape using
-- jurisdiction.property_field_mapping, and every downstream reader
-- (matching, intelligence, UI, CLI) only ever sees these columns.
create table if not exists public.real_property_records (
  id uuid primary key default gen_random_uuid(),
  jurisdiction_id uuid not null references public.jurisdictions(id) on delete cascade,

  -- Minimum portable fields (spec item 3): every jurisdiction is expected
  -- to have these. Everything below is optional enrichment metadata that
  -- degrades gracefully when a county's export doesn't carry it.
  source_property_id text not null,
  situs_address_raw text,

  -- Property exports are annual (e.g. Lubbock's AdHocTaxYear). A newer
  -- year's import must never silently overwrite an older year's row --
  -- see the two unique indexes below and property_adapter.py's
  -- tax-year-selection logic. Null means the source didn't supply a year
  -- at all (treated as its own single "yearless" record, not merged with
  -- any dated year).
  tax_year integer,

  real_account_number text,
  situs_address_normalized text,
  situs_city text,
  situs_state text,
  situs_zip text,

  owner_name text,

  tug text,
  neighborhood text,
  map_id text,

  latitude numeric,
  longitude numeric,

  source_system text not null default 'imported_file',
  source_import_id uuid references public.property_source_imports(id) on delete set null,
  source_updated_at timestamptz,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Two partial unique indexes rather than one plain one: Postgres treats
-- NULL as distinct-from-itself in a unique index, so a plain
-- (jurisdiction_id, source_property_id, tax_year) index would silently
-- allow unlimited duplicate rows for any source with no tax year at all.
-- Dated rows are unique per year; a "yearless" row is unique on its own.
create unique index if not exists real_property_records_dated_unique_idx
on public.real_property_records (jurisdiction_id, source_property_id, tax_year)
where tax_year is not null;

create unique index if not exists real_property_records_undated_unique_idx
on public.real_property_records (jurisdiction_id, source_property_id)
where tax_year is null;

create index if not exists real_property_records_jurisdiction_idx
on public.real_property_records (jurisdiction_id);

create index if not exists real_property_records_situs_normalized_idx
on public.real_property_records (jurisdiction_id, situs_address_normalized);

create index if not exists real_property_records_real_account_idx
on public.real_property_records (jurisdiction_id, real_account_number);

-- Supports "pick the newest year for this property" without pulling every
-- year into application memory (spec item 22).
create index if not exists real_property_records_source_id_year_idx
on public.real_property_records (jurisdiction_id, source_property_id, tax_year desc);

drop trigger if exists set_real_property_records_updated_at on public.real_property_records;
create trigger set_real_property_records_updated_at
before update on public.real_property_records
for each row
execute function public.set_updated_at();

-- One cached enrichment result per subject (a BPP account, an intelligence
-- item, a rendition, a new-business candidate, or an ad-hoc Property Lookup
-- query). Advisory/review data only -- see app/comptroller/property_enrichment.py;
-- nothing in this table is ever written back into official CAD data.
create table if not exists public.property_enrichment_results (
  id uuid primary key default gen_random_uuid(),
  jurisdiction_id uuid not null references public.jurisdictions(id) on delete cascade,

  subject_type text not null,
  subject_id text not null,

  input_address text,
  normalized_input_address text,

  property_record_id uuid references public.real_property_records(id) on delete set null,
  real_account_number text,

  match_status text not null,
  confidence text not null default 'NONE',
  confidence_score numeric,
  candidate_count integer not null default 0,

  match_reason text,
  signals jsonb,

  tug text,
  neighborhood text,
  map_id text,

  source_import_id uuid references public.property_source_imports(id) on delete set null,
  review_status text not null default 'NOT_REVIEWED',

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

do $$
begin
  begin
    alter table public.property_enrichment_results
      add constraint property_enrichment_results_subject_type_check
      check (subject_type in (
        'BPP_ACCOUNT', 'INTELLIGENCE_ITEM', 'RENDITION', 'NEW_BUSINESS_CANDIDATE', 'AD_HOC_LOOKUP'
      ));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.property_enrichment_results
      add constraint property_enrichment_results_match_status_check
      check (match_status in (
        'EXACT_PROPERTY_MATCH', 'STRONG_PROPERTY_MATCH', 'POSSIBLE_PROPERTY_MATCH',
        'AMBIGUOUS_PROPERTY_MATCH', 'NO_PROPERTY_MATCH'
      ));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.property_enrichment_results
      add constraint property_enrichment_results_confidence_check
      check (confidence in ('HIGH', 'MEDIUM', 'LOW', 'NONE'));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.property_enrichment_results
      add constraint property_enrichment_results_review_status_check
      check (review_status in ('NOT_REVIEWED', 'ACCEPTED', 'REJECTED'));
  exception
    when duplicate_object then null;
  end;
end
$$;

-- One current result per subject -- re-running enrichment for the same
-- subject updates this row in place (see app/comptroller/property_enrichment.py
-- caching rules) rather than creating history; property_source_imports is
-- what preserves audit trail of which dataset produced which result.
create unique index if not exists property_enrichment_results_subject_idx
on public.property_enrichment_results (jurisdiction_id, subject_type, subject_id);

create index if not exists property_enrichment_results_jurisdiction_idx
on public.property_enrichment_results (jurisdiction_id);

create index if not exists property_enrichment_results_match_status_idx
on public.property_enrichment_results (match_status);

drop trigger if exists set_property_enrichment_results_updated_at on public.property_enrichment_results;
create trigger set_property_enrichment_results_updated_at
before update on public.property_enrichment_results
for each row
execute function public.set_updated_at();

-- bpp_intelligence_items: additive columns so New Business Detection can
-- surface property corroboration where it exists. matched_address and
-- property_account_number already existed (reserved, unused, since
-- 20260822) and are reused here for the property's situs address and
-- real-property account number rather than duplicating them.
alter table public.bpp_intelligence_items add column if not exists property_match_status text;
alter table public.bpp_intelligence_items add column if not exists property_record_id uuid references public.real_property_records(id) on delete set null;
alter table public.bpp_intelligence_items add column if not exists tug text;
alter table public.bpp_intelligence_items add column if not exists neighborhood text;
alter table public.bpp_intelligence_items add column if not exists map_id text;

do $$
begin
  begin
    alter table public.bpp_intelligence_items
      add constraint bpp_intelligence_items_property_match_status_check
      check (property_match_status is null or property_match_status in (
        'EXACT_PROPERTY_MATCH', 'STRONG_PROPERTY_MATCH', 'POSSIBLE_PROPERTY_MATCH',
        'AMBIGUOUS_PROPERTY_MATCH', 'NO_PROPERTY_MATCH'
      ));
  exception
    when duplicate_object then null;
  end;
end
$$;
