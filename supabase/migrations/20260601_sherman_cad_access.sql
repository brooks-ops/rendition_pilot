insert into public.districts (name, slug, domain, created_at, updated_at)
select
  'Sherman CAD',
  'sherman-cad',
  'shermancad.org',
  now(),
  now()
where not exists (
  select 1
  from public.districts
  where slug = 'sherman-cad'
);

update public.districts
set
  name = 'Sherman CAD',
  domain = 'shermancad.org',
  updated_at = now()
where slug = 'sherman-cad';

with sherman as (
  select id
  from public.districts
  where slug = 'sherman-cad'
  limit 1
),
allowed_admin(email) as (
  values ('ccopley@shermancad.org')
),
existing_auth_users as (
  select id as user_id, lower(email) as email
  from auth.users
  where lower(email) in (select email from allowed_admin)
)
insert into public.district_users (district_id, user_id, email, role, created_at)
select
  sherman.id,
  existing_auth_users.user_id,
  allowed_admin.email,
  'admin',
  now()
from allowed_admin
cross join sherman
left join existing_auth_users
  on existing_auth_users.email = allowed_admin.email
on conflict (email)
do update set
  district_id = excluded.district_id,
  user_id = coalesce(public.district_users.user_id, excluded.user_id),
  role = 'admin';

with sherman as (
  select id
  from public.districts
  where slug = 'sherman-cad'
  limit 1
)
insert into public.cad_districts (
  district_id,
  cad_name,
  display_name,
  email,
  onboarding_completed,
  created_at,
  updated_at
)
select
  sherman.id,
  'Sherman CAD',
  'Sherman CAD',
  'ccopley@shermancad.org',
  false,
  now(),
  now()
from sherman
where not exists (
  select 1
  from public.cad_districts cd
  where cd.district_id = sherman.id
);

with sherman as (
  select id
  from public.districts
  where slug = 'sherman-cad'
  limit 1
)
update public.cad_districts cd
set
  cad_name = 'Sherman CAD',
  display_name = 'Sherman CAD',
  email = 'ccopley@shermancad.org',
  updated_at = now()
from sherman
where cd.district_id = sherman.id;

with cad_profile as (
  select cd.id as cad_district_id, du.id as district_user_id
  from public.cad_districts cd
  join public.districts d
    on d.id = cd.district_id
  left join public.district_users du
    on du.district_id = d.id
   and lower(du.email) = 'ccopley@shermancad.org'
  where d.slug = 'sherman-cad'
  limit 1
)
update public.cad_users cu
set
  district_user_id = cad_profile.district_user_id,
  first_name = 'Courtney',
  last_name = 'Copley',
  role_title = 'Chief Appraiser',
  is_admin = true,
  updated_at = now()
from cad_profile
where cu.district_id = cad_profile.cad_district_id
  and lower(cu.email) = 'ccopley@shermancad.org';

with cad_profile as (
  select cd.id as cad_district_id, du.id as district_user_id
  from public.cad_districts cd
  join public.districts d
    on d.id = cd.district_id
  left join public.district_users du
    on du.district_id = d.id
   and lower(du.email) = 'ccopley@shermancad.org'
  where d.slug = 'sherman-cad'
  limit 1
)
insert into public.cad_users (
  district_id,
  district_user_id,
  first_name,
  last_name,
  email,
  role_title,
  is_admin,
  created_at,
  updated_at
)
select
  cad_profile.cad_district_id,
  cad_profile.district_user_id,
  'Courtney',
  'Copley',
  'ccopley@shermancad.org',
  'Chief Appraiser',
  true,
  now(),
  now()
from cad_profile
where not exists (
  select 1
  from public.cad_users cu
  where cu.district_id = cad_profile.cad_district_id
    and lower(cu.email) = 'ccopley@shermancad.org'
);
