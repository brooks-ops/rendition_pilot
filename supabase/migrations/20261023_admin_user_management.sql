begin;

create or replace function public.remove_district_user(
 p_district_id uuid, p_membership_id uuid, p_actor_user_id uuid, p_actor_email text
) returns jsonb language plpgsql security definer set search_path = public, pg_temp as $$
declare target public.district_users%rowtype;
begin
 -- Serialize membership removals so concurrent admins cannot remove each
 -- other and leave the district with no administrator.
 perform 1 from public.districts where id = p_district_id for update;
 if not exists (select 1 from public.district_users
   where district_id = p_district_id and role = 'admin'
     and (user_id = p_actor_user_id or (user_id is null and lower(email) = lower(p_actor_email)))) then
   raise exception 'Only a current district administrator can remove users.';
 end if;
 select * into target from public.district_users
  where id = p_membership_id and district_id = p_district_id;
 if not found then raise exception 'User was not found in this district.'; end if;
 if target.user_id = p_actor_user_id or lower(target.email) = lower(p_actor_email) then
   raise exception 'You cannot remove your own district access.';
 end if;
 if target.role = 'admin' and (select count(*) from public.district_users where district_id = p_district_id and role = 'admin') <= 1 then
   raise exception 'The district must retain at least one administrator.';
 end if;
 delete from public.district_users where id = target.id and district_id = p_district_id;
 return jsonb_build_object('removed',true,'email',target.email);
end;
$$;
revoke all on function public.remove_district_user(uuid,uuid,uuid,text) from public, anon, authenticated;
grant execute on function public.remove_district_user(uuid,uuid,uuid,text) to service_role;
notify pgrst, 'reload schema';
commit;
