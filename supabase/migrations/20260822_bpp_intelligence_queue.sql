-- BPP Intelligence Queue + jurisdiction abstraction.
--
-- Two things land here:
--
-- 1. `jurisdictions`: the first-class "which appraisal district/county are
--    we processing" concept. Lubbock County is seeded as the first
--    configured jurisdiction, not a hardcoded special case -- everything
--    New Business Detection needs to know about Lubbock (its Comptroller
--    county code, which RenditionPilot district owns its data, which
--    intelligence modules it has enabled, how its account data maps to the
--    normalized model) lives in this one row's config, not in code.
--
-- 2. `bpp_intelligence_items`: a reusable review-item model for ANY
--    detection source (New Business Detection is the first consumer; the
--    existing sales-tax closure monitor is deliberately NOT migrated onto
--    this table in this pass -- see docs/bpp_intelligence_queue.md for why).
--
-- Deliberately additive-only elsewhere: comptroller_permit_locations gains
-- two nullable columns (jurisdiction_id, new_business_evaluated_at) and
-- nothing else about it changes; comptroller_permit_status_events,
-- comptroller_sync_runs, and comptroller_closure_reviews are untouched. The
-- live, already-running sales-tax closure monitor is not modified.

create extension if not exists pgcrypto;

create table if not exists public.jurisdictions (
  id uuid primary key default gen_random_uuid(),
  district_id uuid references public.districts(id) on delete set null,

  name text not null,
  slug text not null,
  county_name text not null,
  state text not null default 'TX',
  timezone text not null default 'America/Chicago',
  active boolean not null default true,

  -- Texas Comptroller identifiers for this jurisdiction's county. The
  -- dataset is the same statewide feed for every TX county (one connector,
  -- reused -- see app/comptroller/client.py), so only the county code and
  -- (in case the Comptroller ever republishes under a new id) dataset id
  -- vary per jurisdiction.
  comptroller_county_code text,
  comptroller_dataset_id text not null default '3kx8-uryv',

  -- Which intelligence modules this jurisdiction has enabled. Checked by
  -- app/comptroller/jurisdictions.py::require_capability() before any
  -- detection module runs -- a jurisdiction is never required to support
  -- every module. Keys correspond to signal-type-ish module names, e.g.
  -- {"sales_tax_monitoring": true, "new_business_detection": true,
  --  "real_property_linkage": false, "mailing_address_monitoring": false,
  --  "dba_monitoring": false}.
  capabilities jsonb not null default '{}'::jsonb,

  -- Per-jurisdiction override of how its CAD/BPP data maps into
  -- RenditionPilot's normalized account model (see app/comptroller/cad_adapter.py).
  -- Empty by default, meaning "use the global COMPTROLLER_MATCH_* env var
  -- defaults" (today's Lubbock behavior); a future jurisdiction with a
  -- differently-shaped data source overrides just the keys it needs here
  -- instead of requiring a code change, e.g.
  -- {"table": "parsed_rendition_results", "owner_name_path": "result->metadata->>owner_name"}.
  cad_field_mapping jsonb not null default '{}'::jsonb,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists jurisdictions_slug_unique_idx
on public.jurisdictions (slug);

create unique index if not exists jurisdictions_comptroller_county_code_unique_idx
on public.jurisdictions (comptroller_county_code)
where comptroller_county_code is not null;

drop trigger if exists set_jurisdictions_updated_at on public.jurisdictions;
create trigger set_jurisdictions_updated_at
before update on public.jurisdictions
for each row
execute function public.set_updated_at();

insert into public.jurisdictions (
  district_id, name, slug, county_name, state, comptroller_county_code, capabilities
)
select
  d.id,
  'Lubbock Central Appraisal District',
  'lubbock',
  'Lubbock',
  'TX',
  '152',
  '{"sales_tax_monitoring": true, "new_business_detection": true, "real_property_linkage": false, "mailing_address_monitoring": false, "dba_monitoring": false}'::jsonb
from public.districts d
where d.slug = 'lubbock-cad'
on conflict (slug) do nothing;

-- comptroller_permit_locations: additive only. `county` (text) is unchanged
-- and still what app.comptroller.service.sync_county() -- the already-deployed,
-- already-running sales-tax closure monitor's write path -- keys off of and
-- is NOT being changed to populate jurisdiction_id (see
-- docs/bpp_intelligence_queue.md for why that write path stays untouched).
-- jurisdiction_id is backfilled once below for existing rows and is useful
-- for direct FK-based queries/display, but New Business Detection itself
-- queries by `county` (text) precisely so it never depends on that backfill
-- or on sync_county ever being modified.
alter table public.comptroller_permit_locations
  add column if not exists jurisdiction_id uuid references public.jurisdictions(id) on delete set null;

alter table public.comptroller_permit_locations
  add column if not exists new_business_evaluated_at timestamptz;

create index if not exists comptroller_permit_locations_jurisdiction_idx
on public.comptroller_permit_locations (jurisdiction_id);

update public.comptroller_permit_locations pl
set jurisdiction_id = j.id
from public.jurisdictions j
where pl.jurisdiction_id is null
  and pl.county = j.county_name;

create table if not exists public.bpp_intelligence_items (
  id uuid primary key default gen_random_uuid(),
  jurisdiction_id uuid references public.jurisdictions(id) on delete set null,
  district_id uuid references public.districts(id) on delete set null,

  signal_type text not null,
  source text not null default 'tx_comptroller_open_data',
  source_dataset_id text,
  source_record_id text not null,
  source_taxpayer_id text,
  source_location_number text,
  source_permit_location_id uuid references public.comptroller_permit_locations(id) on delete set null,

  status text not null default 'NEW',
  classification text,
  priority text not null default 'MEDIUM',

  confidence text,
  confidence_score numeric,
  is_ambiguous boolean not null default false,

  business_name text,
  legal_name text,
  source_address text,
  source_city text,
  source_state text,
  source_zip text,
  permit_start_date date,
  permit_end_date date,
  current_status text,

  first_detected_at timestamptz,

  -- Populated once RenditionPilot actually has queryable account data (see
  -- app/comptroller/matching.py's docstring); matched_address/property_account_number
  -- stay null today by design, ready for when real address/CRS data exists.
  matched_record_id text,
  matched_account_number text,
  matched_owner_name text,
  matched_address text,
  property_account_number text,

  match_score numeric,
  match_reason text,
  match_signals jsonb,

  recommended_action text,
  evidence jsonb,

  assigned_to uuid references auth.users(id) on delete set null,
  reviewed_by uuid references auth.users(id) on delete set null,
  reviewed_at timestamptz,
  resolution text,
  resolution_notes text,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

do $$
begin
  begin
    alter table public.bpp_intelligence_items
      add constraint bpp_intelligence_items_signal_type_check
      check (signal_type in ('sales_tax_inactive', 'new_business'));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.bpp_intelligence_items
      add constraint bpp_intelligence_items_status_check
      check (status in ('NEW', 'IN_REVIEW', 'RESOLVED', 'DISMISSED'));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.bpp_intelligence_items
      add constraint bpp_intelligence_items_classification_check
      check (classification in (
        'EXISTING_ACCOUNT_HIGH_CONFIDENCE',
        'POSSIBLE_EXISTING_ACCOUNT',
        'NO_ACCOUNT_FOUND',
        'AMBIGUOUS'
      ));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.bpp_intelligence_items
      add constraint bpp_intelligence_items_priority_check
      check (priority in ('HIGH', 'MEDIUM', 'LOW'));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.bpp_intelligence_items
      add constraint bpp_intelligence_items_confidence_check
      check (confidence in ('HIGH', 'MEDIUM', 'LOW', 'UNMATCHED'));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.bpp_intelligence_items
      add constraint bpp_intelligence_items_resolution_check
      check (resolution in (
        'ACCOUNT_EXISTS',
        'NEW_ACCOUNT_NEEDED',
        'BUSINESS_CLOSED',
        'RELOCATION',
        'OWNERSHIP_CHANGE',
        'DUPLICATE',
        'FALSE_MATCH',
        'NO_TAXABLE_BPP',
        'OTHER'
      ));
  exception
    when duplicate_object then null;
  end;
end
$$;

-- Dedup key: the same underlying event (one signal, one source, one source
-- record) is never represented by more than one row, ever -- including
-- across RESOLVED/DISMISSED items, so a resolved item is never silently
-- recreated by a later run (see app/comptroller/new_business.py).
create unique index if not exists bpp_intelligence_items_dedup_idx
on public.bpp_intelligence_items (signal_type, source, source_record_id);

create index if not exists bpp_intelligence_items_status_idx
on public.bpp_intelligence_items (status);

create index if not exists bpp_intelligence_items_signal_type_idx
on public.bpp_intelligence_items (signal_type);

create index if not exists bpp_intelligence_items_priority_idx
on public.bpp_intelligence_items (priority);

create index if not exists bpp_intelligence_items_jurisdiction_idx
on public.bpp_intelligence_items (jurisdiction_id);

create index if not exists bpp_intelligence_items_district_idx
on public.bpp_intelligence_items (district_id);

create index if not exists bpp_intelligence_items_created_at_idx
on public.bpp_intelligence_items (created_at desc);

drop trigger if exists set_bpp_intelligence_items_updated_at on public.bpp_intelligence_items;
create trigger set_bpp_intelligence_items_updated_at
before update on public.bpp_intelligence_items
for each row
execute function public.set_updated_at();
