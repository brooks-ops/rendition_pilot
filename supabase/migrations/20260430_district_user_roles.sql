alter table public.district_users
add column if not exists role text not null default 'member';

update public.district_users
set role = coalesce(nullif(btrim(lower(role)), ''), 'member');

alter table public.district_users
drop constraint if exists district_users_role_check;

alter table public.district_users
add constraint district_users_role_check
check (role in ('admin', 'member'));
