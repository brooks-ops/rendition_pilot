create extension if not exists pgcrypto;

-- Current state of every Texas Comptroller sales-tax permit location that has
-- ever been observed within a monitored county. One row per
-- (taxpayer_id, location_number) pair, upserted on every sync.
create table if not exists public.comptroller_permit_locations (
  id uuid primary key default gen_random_uuid(),
  district_id uuid references public.districts(id) on delete set null,
  county text not null,
  taxpayer_id text not null,
  location_number text not null,
  legal_name text,
  location_name text,
  address text,
  city text,
  state text,
  zip text,
  permit_start_date date,
  permit_end_date date,
  current_status text not null default 'ACTIVE',
  is_baseline boolean not null default false,
  first_seen_at timestamptz not null default now(),
  last_checked_at timestamptz not null default now(),
  last_changed_at timestamptz,
  source text not null default 'tx_comptroller_open_data',
  source_dataset_id text,
  source_row_raw jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.comptroller_permit_locations add column if not exists district_id uuid references public.districts(id) on delete set null;
alter table public.comptroller_permit_locations add column if not exists county text;
alter table public.comptroller_permit_locations add column if not exists taxpayer_id text;
alter table public.comptroller_permit_locations add column if not exists location_number text;
alter table public.comptroller_permit_locations add column if not exists legal_name text;
alter table public.comptroller_permit_locations add column if not exists location_name text;
alter table public.comptroller_permit_locations add column if not exists address text;
alter table public.comptroller_permit_locations add column if not exists city text;
alter table public.comptroller_permit_locations add column if not exists state text;
alter table public.comptroller_permit_locations add column if not exists zip text;
alter table public.comptroller_permit_locations add column if not exists permit_start_date date;
alter table public.comptroller_permit_locations add column if not exists permit_end_date date;
alter table public.comptroller_permit_locations add column if not exists current_status text;
alter table public.comptroller_permit_locations add column if not exists is_baseline boolean not null default false;
alter table public.comptroller_permit_locations add column if not exists first_seen_at timestamptz not null default now();
alter table public.comptroller_permit_locations add column if not exists last_checked_at timestamptz not null default now();
alter table public.comptroller_permit_locations add column if not exists last_changed_at timestamptz;
alter table public.comptroller_permit_locations add column if not exists source text not null default 'tx_comptroller_open_data';
alter table public.comptroller_permit_locations add column if not exists source_dataset_id text;
alter table public.comptroller_permit_locations add column if not exists source_row_raw jsonb;
alter table public.comptroller_permit_locations add column if not exists created_at timestamptz not null default now();
alter table public.comptroller_permit_locations add column if not exists updated_at timestamptz not null default now();

do $$
begin
  begin
    alter table public.comptroller_permit_locations
      add constraint comptroller_permit_locations_status_check
      check (current_status in ('ACTIVE', 'INACTIVE'));
  exception
    when duplicate_object then null;
  end;
end
$$;

create unique index if not exists comptroller_permit_locations_taxpayer_location_unique_idx
on public.comptroller_permit_locations (taxpayer_id, location_number);

create index if not exists comptroller_permit_locations_county_idx
on public.comptroller_permit_locations (county);

create index if not exists comptroller_permit_locations_status_idx
on public.comptroller_permit_locations (current_status);

drop trigger if exists set_comptroller_permit_locations_updated_at on public.comptroller_permit_locations;
create trigger set_comptroller_permit_locations_updated_at
before update on public.comptroller_permit_locations
for each row
execute function public.set_updated_at();

-- Durable, append-only history of every meaningful status/permit-end-date
-- change detected for a permit location. Baseline rows (the very first import)
-- are recorded here too (change_type = 'BASELINE') so "when did we start
-- tracking this permit" stays answerable, but they are excluded from month-end
-- review processing by change_type, not deleted or hidden.
create table if not exists public.comptroller_permit_status_events (
  id uuid primary key default gen_random_uuid(),
  permit_location_id uuid not null references public.comptroller_permit_locations(id) on delete cascade,
  taxpayer_id text not null,
  location_number text not null,
  change_type text not null,
  previous_status text,
  new_status text not null,
  previous_permit_end_date date,
  new_permit_end_date date,
  detected_at timestamptz not null default now(),
  source_data_date timestamptz,
  sync_run_id uuid,
  month_end_processed_at timestamptz,
  review_item_id uuid,
  created_at timestamptz not null default now()
);

alter table public.comptroller_permit_status_events add column if not exists permit_location_id uuid references public.comptroller_permit_locations(id) on delete cascade;
alter table public.comptroller_permit_status_events add column if not exists taxpayer_id text;
alter table public.comptroller_permit_status_events add column if not exists location_number text;
alter table public.comptroller_permit_status_events add column if not exists change_type text;
alter table public.comptroller_permit_status_events add column if not exists previous_status text;
alter table public.comptroller_permit_status_events add column if not exists new_status text;
alter table public.comptroller_permit_status_events add column if not exists previous_permit_end_date date;
alter table public.comptroller_permit_status_events add column if not exists new_permit_end_date date;
alter table public.comptroller_permit_status_events add column if not exists detected_at timestamptz not null default now();
alter table public.comptroller_permit_status_events add column if not exists source_data_date timestamptz;
alter table public.comptroller_permit_status_events add column if not exists sync_run_id uuid;
alter table public.comptroller_permit_status_events add column if not exists month_end_processed_at timestamptz;
alter table public.comptroller_permit_status_events add column if not exists review_item_id uuid;
alter table public.comptroller_permit_status_events add column if not exists created_at timestamptz not null default now();

do $$
begin
  begin
    alter table public.comptroller_permit_status_events
      add constraint comptroller_permit_status_events_change_type_check
      check (change_type in (
        'BASELINE',
        'STATUS_CHANGE',
        'PERMIT_END_DATE_ADDED',
        'STATUS_AND_END_DATE_CHANGE',
        'REOPENED',
        'OTHER'
      ));
  exception
    when duplicate_object then null;
  end;
end
$$;

create index if not exists comptroller_permit_status_events_location_idx
on public.comptroller_permit_status_events (permit_location_id);

create index if not exists comptroller_permit_status_events_detected_at_idx
on public.comptroller_permit_status_events (detected_at);

create index if not exists comptroller_permit_status_events_unprocessed_idx
on public.comptroller_permit_status_events (change_type, month_end_processed_at);

-- One row per sync attempt (baseline, daily, month-end, or manual) for
-- admin/observability. A failed or partial fetch is logged here without
-- ever mutating comptroller_permit_locations.
create table if not exists public.comptroller_sync_runs (
  id uuid primary key default gen_random_uuid(),
  run_type text not null,
  county text,
  status text not null default 'RUNNING',
  permits_checked integer not null default 0,
  permits_new integer not null default 0,
  permits_newly_inactive integer not null default 0,
  error_message text,
  source_data_date timestamptz,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.comptroller_sync_runs add column if not exists run_type text;
alter table public.comptroller_sync_runs add column if not exists county text;
alter table public.comptroller_sync_runs add column if not exists status text not null default 'RUNNING';
alter table public.comptroller_sync_runs add column if not exists permits_checked integer not null default 0;
alter table public.comptroller_sync_runs add column if not exists permits_new integer not null default 0;
alter table public.comptroller_sync_runs add column if not exists permits_newly_inactive integer not null default 0;
alter table public.comptroller_sync_runs add column if not exists error_message text;
alter table public.comptroller_sync_runs add column if not exists source_data_date timestamptz;
alter table public.comptroller_sync_runs add column if not exists started_at timestamptz not null default now();
alter table public.comptroller_sync_runs add column if not exists finished_at timestamptz;
alter table public.comptroller_sync_runs add column if not exists created_at timestamptz not null default now();

do $$
begin
  begin
    alter table public.comptroller_sync_runs
      add constraint comptroller_sync_runs_run_type_check
      check (run_type in ('BASELINE', 'DAILY', 'MONTH_END', 'MANUAL'));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.comptroller_sync_runs
      add constraint comptroller_sync_runs_status_check
      check (status in ('RUNNING', 'SUCCESS', 'FAILED', 'PARTIAL'));
  exception
    when duplicate_object then null;
  end;
end
$$;

create index if not exists comptroller_sync_runs_started_at_idx
on public.comptroller_sync_runs (started_at desc);

-- Month-end review queue. One row per closure/status-change event that was
-- selected for review, created only during month-end processing (never on a
-- daily run). Reusing the districts table for tenancy the same way
-- cad_districts/cad_users do.
create table if not exists public.comptroller_closure_reviews (
  id uuid primary key default gen_random_uuid(),
  district_id uuid references public.districts(id) on delete set null,
  permit_location_id uuid references public.comptroller_permit_locations(id) on delete set null,
  status_event_id uuid references public.comptroller_permit_status_events(id) on delete set null,
  review_month date not null,
  comptroller_taxpayer_id text,
  comptroller_location_number text,
  comptroller_business_name text,
  comptroller_legal_name text,
  comptroller_address text,
  comptroller_city text,
  comptroller_state text,
  comptroller_zip text,
  comptroller_permit_start_date date,
  comptroller_permit_end_date date,
  comptroller_previous_status text,
  comptroller_current_status text,
  first_detected_at timestamptz,
  matched_account_id text,
  matched_account_number text,
  matched_owner_name text,
  matched_situs_address text,
  match_confidence text not null default 'UNMATCHED',
  match_score numeric,
  match_reason text,
  workflow_status text not null default 'PENDING_REVIEW',
  reviewer_notes text,
  reviewed_by uuid references auth.users(id) on delete set null,
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.comptroller_closure_reviews add column if not exists district_id uuid references public.districts(id) on delete set null;
alter table public.comptroller_closure_reviews add column if not exists permit_location_id uuid references public.comptroller_permit_locations(id) on delete set null;
alter table public.comptroller_closure_reviews add column if not exists status_event_id uuid references public.comptroller_permit_status_events(id) on delete set null;
alter table public.comptroller_closure_reviews add column if not exists review_month date;
alter table public.comptroller_closure_reviews add column if not exists comptroller_taxpayer_id text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_location_number text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_business_name text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_legal_name text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_address text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_city text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_state text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_zip text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_permit_start_date date;
alter table public.comptroller_closure_reviews add column if not exists comptroller_permit_end_date date;
alter table public.comptroller_closure_reviews add column if not exists comptroller_previous_status text;
alter table public.comptroller_closure_reviews add column if not exists comptroller_current_status text;
alter table public.comptroller_closure_reviews add column if not exists first_detected_at timestamptz;
alter table public.comptroller_closure_reviews add column if not exists matched_account_id text;
alter table public.comptroller_closure_reviews add column if not exists matched_account_number text;
alter table public.comptroller_closure_reviews add column if not exists matched_owner_name text;
alter table public.comptroller_closure_reviews add column if not exists matched_situs_address text;
alter table public.comptroller_closure_reviews add column if not exists match_confidence text not null default 'UNMATCHED';
alter table public.comptroller_closure_reviews add column if not exists match_score numeric;
alter table public.comptroller_closure_reviews add column if not exists match_reason text;
alter table public.comptroller_closure_reviews add column if not exists workflow_status text not null default 'PENDING_REVIEW';
alter table public.comptroller_closure_reviews add column if not exists reviewer_notes text;
alter table public.comptroller_closure_reviews add column if not exists reviewed_by uuid references auth.users(id) on delete set null;
alter table public.comptroller_closure_reviews add column if not exists reviewed_at timestamptz;
alter table public.comptroller_closure_reviews add column if not exists created_at timestamptz not null default now();
alter table public.comptroller_closure_reviews add column if not exists updated_at timestamptz not null default now();

do $$
begin
  begin
    alter table public.comptroller_closure_reviews
      add constraint comptroller_closure_reviews_confidence_check
      check (match_confidence in ('HIGH', 'MEDIUM', 'LOW', 'UNMATCHED'));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.comptroller_closure_reviews
      add constraint comptroller_closure_reviews_workflow_status_check
      check (workflow_status in (
        'PENDING_REVIEW',
        'CONFIRMED_CLOSURE',
        'NOT_CLOSED',
        'OWNERSHIP_CHANGE',
        'RELOCATED',
        'DUPLICATE',
        'OTHER_NEEDS_RESEARCH'
      ));
  exception
    when duplicate_object then null;
  end;
end
$$;

-- NOT a partial index: PostgREST's on_conflict=status_event_id upsert (used by
-- app.comptroller.month_end._create_review, with Prefer: resolution=ignore-duplicates)
-- generates a plain `ON CONFLICT (status_event_id)`, which Postgres can only
-- resolve against a full unique index/constraint on that column -- a partial
-- index (e.g. "where status_event_id is not null") is not a valid arbiter for
-- that unqualified ON CONFLICT target and would make every insert fail with
-- "no unique or exclusion constraint matching the ON CONFLICT specification".
-- status_event_id is always populated by this feature, so a full index costs
-- nothing here.
create unique index if not exists comptroller_closure_reviews_status_event_unique_idx
on public.comptroller_closure_reviews (status_event_id);

create index if not exists comptroller_closure_reviews_review_month_idx
on public.comptroller_closure_reviews (review_month);

create index if not exists comptroller_closure_reviews_workflow_status_idx
on public.comptroller_closure_reviews (workflow_status);

drop trigger if exists set_comptroller_closure_reviews_updated_at on public.comptroller_closure_reviews;
create trigger set_comptroller_closure_reviews_updated_at
before update on public.comptroller_closure_reviews
for each row
execute function public.set_updated_at();
