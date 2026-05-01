create table if not exists public.app_access (
  id uuid primary key default gen_random_uuid(),
  app_name text not null,
  email text not null,
  user_id uuid references auth.users(id) on delete cascade,
  role text not null default 'member',
  created_at timestamptz not null default now()
);

alter table public.app_access add column if not exists app_name text;
alter table public.app_access add column if not exists email text;
alter table public.app_access add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table public.app_access add column if not exists role text not null default 'member';
alter table public.app_access add column if not exists created_at timestamptz not null default now();

update public.app_access
set
  app_name = lower(btrim(app_name)),
  email = lower(btrim(email)),
  role = coalesce(nullif(btrim(lower(role)), ''), 'member'),
  created_at = coalesce(created_at, now());

alter table public.app_access
drop constraint if exists app_access_app_name_check;

alter table public.app_access
add constraint app_access_app_name_check
check (app_name in ('arb_pilot', 'rendition_pilot'));

alter table public.app_access
drop constraint if exists app_access_role_check;

alter table public.app_access
add constraint app_access_role_check
check (role in ('admin', 'member'));

create unique index if not exists app_access_email_app_unique_idx
on public.app_access (email, app_name);

create unique index if not exists app_access_user_app_unique_idx
on public.app_access (user_id, app_name)
where user_id is not null;

with allowed_arb_users(email, role) as (
  values
    ('bbarrett@lubbockcar.org', 'admin'),
    ('bbarrett@lubbockcad.org', 'admin'),
    ('hstewart@lubbockcad.org', 'member'),
    ('evaldez@lubbockcad.org', 'member'),
    ('lcantrell@lubbockcad.org', 'member'),
    ('lsloan@lubbockcad.org', 'member'),
    ('bmilner@lubbockcad.org', 'member')
),
existing_auth_users as (
  select id as user_id, lower(email) as email
  from auth.users
  where lower(email) in (select email from allowed_arb_users)
)
insert into public.app_access (app_name, email, user_id, role, created_at)
select
  'arb_pilot',
  allowed_arb_users.email,
  existing_auth_users.user_id,
  allowed_arb_users.role,
  now()
from allowed_arb_users
left join existing_auth_users
  on existing_auth_users.email = allowed_arb_users.email
on conflict (email, app_name)
do update set
  user_id = coalesce(public.app_access.user_id, excluded.user_id),
  role = excluded.role;

delete from public.district_users
where lower(email) in (
  'hstewart@lubbockcad.org',
  'evaldez@lubbockcad.org',
  'lcantrell@lubbockcad.org',
  'lsloan@lubbockcad.org',
  'bmilner@lubbockcad.org'
);

with lubbock as (
  select id
  from public.districts
  where slug = 'lubbock-cad'
  limit 1
),
allowed_barrett(email) as (
  values
    ('bbarrett@lubbockcar.org'),
    ('bbarrett@lubbockcad.org')
),
existing_auth_users as (
  select id as user_id, lower(email) as email
  from auth.users
  where lower(email) in (select email from allowed_barrett)
)
insert into public.district_users (district_id, user_id, email, role, created_at)
select
  lubbock.id,
  existing_auth_users.user_id,
  allowed_barrett.email,
  'admin',
  now()
from allowed_barrett
cross join lubbock
left join existing_auth_users
  on existing_auth_users.email = allowed_barrett.email
on conflict (email)
do update set
  district_id = excluded.district_id,
  user_id = coalesce(public.district_users.user_id, excluded.user_id),
  role = 'admin';
