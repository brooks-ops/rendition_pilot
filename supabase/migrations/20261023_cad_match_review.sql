begin;
alter table public.real_property_records add column if not exists business_name text;
alter table public.real_property_records add column if not exists dba_name text;
alter table public.real_property_records add column if not exists account_status text;
alter table public.real_property_records add column if not exists situs_unit text;
alter table public.property_source_imports add column if not exists import_status text not null default 'LEGACY';
alter table public.property_source_imports add column if not exists quality_summary jsonb;
alter table public.property_source_imports add column if not exists completed_at timestamptz;

create table if not exists public.cad_match_decisions (
 id uuid primary key default gen_random_uuid(),
 district_id uuid not null,
 jurisdiction_id uuid not null references public.jurisdictions(id),
 permit_location_id uuid not null references public.comptroller_permit_locations(id),
 property_record_id uuid references public.real_property_records(id) on delete set null,
 account_number text not null,
 decision text not null check (decision in ('CONFIRMED','REJECTED')),
 notes text,
 reviewed_by uuid not null references auth.users(id),
 reviewed_at timestamptz not null default now(),
 source_table text not null check (source_table in ('bpp_intelligence_items','comptroller_closure_reviews')),
 source_item_id text not null
);
create index if not exists cad_match_decisions_permit_idx
 on public.cad_match_decisions(jurisdiction_id, permit_location_id, reviewed_at desc, id desc);
alter table public.cad_match_decisions enable row level security;
revoke all on public.cad_match_decisions from anon, authenticated;
grant select, insert on public.cad_match_decisions to service_role;
notify pgrst, 'reload schema';
commit;
