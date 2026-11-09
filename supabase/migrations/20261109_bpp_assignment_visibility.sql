begin;
-- Intelligence reads/writes go through the authenticated backend, which scopes
-- non-admins to assigned_to. Prevent direct PostgREST table access bypasses.
alter table public.bpp_intelligence_items enable row level security;
alter table public.comptroller_closure_reviews enable row level security;
revoke all on public.bpp_intelligence_items,public.comptroller_closure_reviews from public,anon,authenticated;
grant select,insert,update,delete on public.bpp_intelligence_items,public.comptroller_closure_reviews to service_role;
create index if not exists bpp_assignment_visibility_idx on public.bpp_intelligence_items(district_id,assigned_to);
create index if not exists closure_assignment_visibility_idx on public.comptroller_closure_reviews(district_id,assigned_to);
commit;
