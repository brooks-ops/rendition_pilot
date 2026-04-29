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

create table if not exists public.districts (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  slug text not null,
  domain text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.districts add column if not exists name text;
alter table public.districts add column if not exists slug text;
alter table public.districts add column if not exists domain text;
alter table public.districts add column if not exists created_at timestamptz;
alter table public.districts add column if not exists updated_at timestamptz;

update public.districts
set
  created_at = coalesce(created_at, now()),
  updated_at = coalesce(updated_at, now());

update public.districts
set slug = lower(
  trim(both '-' from regexp_replace(coalesce(nullif(slug, ''), nullif(name, ''), 'district'), '[^a-z0-9]+', '-', 'g'))
)
where slug is null or btrim(slug) = '';

with dupes as (
  select
    id,
    slug,
    row_number() over (partition by slug order by created_at nulls last, id) as slug_rank
  from public.districts
  where slug is not null and btrim(slug) <> ''
)
update public.districts d
set slug = d.slug || '-' || substr(replace(d.id::text, '-', ''), 1, 8)
from dupes
where d.id = dupes.id
  and dupes.slug_rank > 1;

do $$
begin
  begin
    alter table public.districts alter column name set not null;
  exception
    when others then null;
  end;
  begin
    alter table public.districts alter column slug set not null;
  exception
    when others then null;
  end;
  begin
    alter table public.districts alter column created_at set default now();
  exception
    when others then null;
  end;
  begin
    alter table public.districts alter column updated_at set default now();
  exception
    when others then null;
  end;
  begin
    alter table public.districts alter column created_at set not null;
  exception
    when others then null;
  end;
  begin
    alter table public.districts alter column updated_at set not null;
  exception
    when others then null;
  end;
end
$$;

create unique index if not exists districts_slug_unique_idx
on public.districts (slug);

drop trigger if exists set_districts_updated_at on public.districts;
create trigger set_districts_updated_at
before update on public.districts
for each row
execute function public.set_updated_at();

create table if not exists public.district_users (
  id uuid primary key default gen_random_uuid(),
  district_id uuid not null references public.districts(id) on delete cascade,
  user_id uuid references auth.users(id) on delete cascade,
  email text not null,
  created_at timestamptz not null default now()
);

alter table public.district_users add column if not exists district_id uuid references public.districts(id) on delete cascade;
alter table public.district_users add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table public.district_users add column if not exists email text;
alter table public.district_users add column if not exists created_at timestamptz;

update public.district_users
set
  email = lower(email),
  created_at = coalesce(created_at, now())
where email is not null or created_at is null;

with email_dupes as (
  select
    id,
    row_number() over (partition by lower(email) order by created_at nulls last, id) as email_rank
  from public.district_users
  where email is not null and btrim(email) <> ''
),
user_dupes as (
  select
    id,
    row_number() over (partition by user_id order by created_at nulls last, id) as user_rank
  from public.district_users
  where user_id is not null
)
delete from public.district_users du
where du.id in (
  select id from email_dupes where email_rank > 1
  union
  select id from user_dupes where user_rank > 1
);

do $$
begin
  begin
    alter table public.district_users alter column created_at set default now();
  exception
    when others then null;
  end;
  begin
    alter table public.district_users alter column created_at set not null;
  exception
    when others then null;
  end;
end
$$;

create unique index if not exists district_users_email_unique_idx
on public.district_users (email);

create unique index if not exists district_users_user_id_unique
on public.district_users (user_id)
where user_id is not null;

insert into public.districts (name, slug, domain, created_at, updated_at)
select seed.name, seed.slug, seed.domain, now(), now()
from (
  values
    ('Lubbock Central Appraisal District', 'lubbock-cad', 'lubbockcad.org'),
    ('Dallam County Appraisal District', 'dallam-cad', 'dallamcad.org')
) as seed(name, slug, domain)
where not exists (
  select 1
  from public.districts d
  where d.slug = seed.slug
);

update public.districts d
set
  name = seed.name,
  domain = seed.domain,
  updated_at = now()
from (
  values
    ('Lubbock Central Appraisal District', 'lubbock-cad', 'lubbockcad.org'),
    ('Dallam County Appraisal District', 'dallam-cad', 'dallamcad.org')
) as seed(name, slug, domain)
where d.slug = seed.slug;

with seeded_districts as (
  select id, slug
  from public.districts
  where slug in ('lubbock-cad', 'dallam-cad')
),
existing_auth_users as (
  select
    id as user_id,
    lower(email) as email,
    case
      when lower(email) like '%@lubbockcad.org' then 'lubbock-cad'
      when lower(email) like '%@dallamcad.org' then 'dallam-cad'
      else null
    end as district_slug
  from auth.users
  where lower(email) like '%@lubbockcad.org'
     or lower(email) like '%@dallamcad.org'
),
candidate_links as (
  select
    d.id as district_id,
    u.user_id,
    u.email
  from existing_auth_users u
  join seeded_districts d
    on d.slug = u.district_slug
  where u.district_slug is not null
),
update_existing as (
  update public.district_users du
  set
    district_id = cl.district_id,
    user_id = coalesce(du.user_id, cl.user_id)
  from candidate_links cl
  where lower(du.email) = cl.email
  returning du.email
)
insert into public.district_users (district_id, user_id, email, created_at)
select
  cl.district_id,
  cl.user_id,
  cl.email,
  now()
from candidate_links cl
where not exists (
  select 1
  from public.district_users du
  where lower(du.email) = cl.email
);

do $$
declare
  target_table text;
  lubbock_id uuid;
begin
  select id
  into lubbock_id
  from public.districts
  where slug = 'lubbock-cad';

  foreach target_table in array array[
    'accounts',
    'completed_reviews',
    'rendition_accounts',
    'rendition_reviews',
    'renditions',
    'review_queue',
    'saved_renditions'
  ]
  loop
    if exists (
      select 1
      from information_schema.tables
      where table_schema = 'public'
        and table_name = target_table
    ) then
      execute format(
        'alter table public.%I add column if not exists district_id uuid references public.districts(id)',
        target_table
      );

      if lubbock_id is not null then
        execute format(
          'update public.%I set district_id = %L where district_id is null',
          target_table,
          lubbock_id
        );
      end if;
    end if;
  end loop;
end
$$;
