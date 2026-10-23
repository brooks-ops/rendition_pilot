-- Transactional smoke test. All fixture records and changes are rolled back.
begin;
do $$
declare
 district_one uuid := gen_random_uuid(); district_two uuid := gen_random_uuid();
 actor_id uuid := gen_random_uuid(); actor_membership uuid := gen_random_uuid();
 member_id uuid := gen_random_uuid(); other_member uuid := gen_random_uuid();
 suffix text := replace(gen_random_uuid()::text,'-','');
 actor_email text; target_email text; rejected boolean;
begin
 actor_email := 'admin-'||suffix||'@example.invalid';
 target_email := 'member-'||suffix||'@example.invalid';
 insert into public.districts(id,name,slug) values
 (district_one,'Temporary admin feature test','admin-feature-test-'||suffix),
 (district_two,'Temporary isolation test','admin-isolation-test-'||suffix);
 insert into public.district_users(id,district_id,email,role) values
 (actor_membership,district_one,actor_email,'admin'),
 (member_id,district_one,target_email,'member'),
 (other_member,district_two,'other-'||suffix||'@example.invalid','member');
 rejected := false;
 begin
   perform public.remove_district_user(district_one,actor_membership,actor_id,actor_email);
 exception when others then
   if sqlerrm not like '%cannot remove your own%' then raise; end if;
   rejected := true;
 end;
 assert rejected,'Self-removal should be refused';
 rejected := false;
 begin
   perform public.remove_district_user(district_one,other_member,actor_id,actor_email);
 exception when others then
   if sqlerrm not like '%not found in this district%' then raise; end if;
   rejected := true;
 end;
 assert rejected,'Cross-district removal should be refused';
 rejected := false;
 begin
   perform public.remove_district_user(district_one,member_id,actor_id,'not-an-admin@example.invalid');
 exception when others then
   if sqlerrm not like '%current district administrator%' then raise; end if;
   rejected := true;
 end;
 assert rejected,'Unknown actor should be refused';
 perform public.remove_district_user(district_one,member_id,actor_id,actor_email);
 assert not exists(select 1 from public.district_users where id=member_id),'Selected membership should be removed';
 assert exists(select 1 from public.district_users where id=actor_membership),'Admin must remain';
 assert exists(select 1 from public.district_users where id=other_member),'Other district must remain';
 assert not has_function_privilege('anon','public.remove_district_user(uuid,uuid,uuid,text)','EXECUTE'),'Anonymous removal must be blocked';
 assert not has_function_privilege('authenticated','public.remove_district_user(uuid,uuid,uuid,text)','EXECUTE'),'Direct client removal must be blocked';
end;
$$;
rollback;
select 'PASS: removal, self-protection, district isolation, and RPC permissions; all fixtures rolled back' as verification;
