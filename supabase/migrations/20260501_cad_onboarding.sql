create extension if not exists pgcrypto;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table if not exists public.cad_districts (
  id uuid primary key default gen_random_uuid(),
  district_id uuid references public.districts(id) on delete cascade,
  cad_name text not null,
  display_name text,
  email text,
  phone text,
  address text,
  website text,
  onboarding_completed boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.cad_districts add column if not exists district_id uuid references public.districts(id) on delete cascade;
alter table public.cad_districts add column if not exists cad_name text;
alter table public.cad_districts add column if not exists display_name text;
alter table public.cad_districts add column if not exists email text;
alter table public.cad_districts add column if not exists phone text;
alter table public.cad_districts add column if not exists address text;
alter table public.cad_districts add column if not exists website text;
alter table public.cad_districts add column if not exists onboarding_completed boolean not null default false;
alter table public.cad_districts add column if not exists created_at timestamptz not null default now();
alter table public.cad_districts add column if not exists updated_at timestamptz not null default now();

create unique index if not exists cad_districts_district_id_unique_idx
on public.cad_districts (district_id)
where district_id is not null;

drop trigger if exists set_cad_districts_updated_at on public.cad_districts;
create trigger set_cad_districts_updated_at
before update on public.cad_districts
for each row
execute function public.set_updated_at();

create table if not exists public.cad_users (
  id uuid primary key default gen_random_uuid(),
  district_id uuid not null references public.cad_districts(id) on delete cascade,
  district_user_id uuid references public.district_users(id) on delete set null,
  first_name text,
  last_name text,
  email text not null,
  role_title text,
  is_admin boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.cad_users add column if not exists district_id uuid references public.cad_districts(id) on delete cascade;
alter table public.cad_users add column if not exists district_user_id uuid references public.district_users(id) on delete set null;
alter table public.cad_users add column if not exists first_name text;
alter table public.cad_users add column if not exists last_name text;
alter table public.cad_users add column if not exists email text;
alter table public.cad_users add column if not exists role_title text;
alter table public.cad_users add column if not exists is_admin boolean not null default false;
alter table public.cad_users add column if not exists created_at timestamptz not null default now();
alter table public.cad_users add column if not exists updated_at timestamptz not null default now();

create unique index if not exists cad_users_district_email_unique_idx
on public.cad_users (district_id, lower(email));

drop trigger if exists set_cad_users_updated_at on public.cad_users;
create trigger set_cad_users_updated_at
before update on public.cad_users
for each row
execute function public.set_updated_at();

create table if not exists public.cad_depreciation_schedules (
  id uuid primary key default gen_random_uuid(),
  district_id uuid not null references public.cad_districts(id) on delete cascade,
  schedule_name text not null,
  schedule_type text,
  schedule_years int not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.cad_depreciation_schedules add column if not exists district_id uuid references public.cad_districts(id) on delete cascade;
alter table public.cad_depreciation_schedules add column if not exists schedule_name text;
alter table public.cad_depreciation_schedules add column if not exists schedule_type text;
alter table public.cad_depreciation_schedules add column if not exists schedule_years int;
alter table public.cad_depreciation_schedules add column if not exists is_active boolean not null default true;
alter table public.cad_depreciation_schedules add column if not exists created_at timestamptz not null default now();
alter table public.cad_depreciation_schedules add column if not exists updated_at timestamptz not null default now();

drop trigger if exists set_cad_depreciation_schedules_updated_at on public.cad_depreciation_schedules;
create trigger set_cad_depreciation_schedules_updated_at
before update on public.cad_depreciation_schedules
for each row
execute function public.set_updated_at();

create table if not exists public.cad_depreciation_schedule_rows (
  id uuid primary key default gen_random_uuid(),
  schedule_id uuid not null references public.cad_depreciation_schedules(id) on delete cascade,
  year_number int not null,
  depreciation_percent numeric not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.cad_depreciation_schedule_rows add column if not exists schedule_id uuid references public.cad_depreciation_schedules(id) on delete cascade;
alter table public.cad_depreciation_schedule_rows add column if not exists year_number int;
alter table public.cad_depreciation_schedule_rows add column if not exists depreciation_percent numeric;
alter table public.cad_depreciation_schedule_rows add column if not exists created_at timestamptz not null default now();
alter table public.cad_depreciation_schedule_rows add column if not exists updated_at timestamptz not null default now();

create unique index if not exists cad_depreciation_schedule_rows_schedule_year_unique_idx
on public.cad_depreciation_schedule_rows (schedule_id, year_number);

drop trigger if exists set_cad_depreciation_schedule_rows_updated_at on public.cad_depreciation_schedule_rows;
create trigger set_cad_depreciation_schedule_rows_updated_at
before update on public.cad_depreciation_schedule_rows
for each row
execute function public.set_updated_at();
