begin;
create or replace function public.bpp_completed_account_cards(p_district_id uuid,p_reviewer uuid,p_print_status text,p_offset integer)
returns jsonb language sql stable security definer set search_path=public,pg_temp as $$
 select jsonb_build_object('rows',coalesce((select jsonb_agg(to_jsonb(r)) from (
  select i.*,p.printed_at from public.bpp_intelligence_items i
  left join public.bpp_account_card_prints p on p.item_id=i.id and p.version=i.account_card_version
  where i.district_id=p_district_id and i.status='RESOLVED' and i.account_card_draft is not null
   and (p_reviewer is null or i.reviewed_by=p_reviewer)
   and (p_print_status='all' or (p_print_status='printed' and p.printed_at is not null) or (p_print_status='unprinted' and p.printed_at is null))
  order by i.reviewed_at desc,i.id desc limit 101 offset greatest(p_offset,0)
 ) r),'[]'::jsonb),'appraisers',coalesce((select jsonb_agg(a order by a.label) from (
  select i.reviewed_by as id,coalesce(min(u.email),'Former appraiser '||left(i.reviewed_by::text,8)) as label
  from public.bpp_intelligence_items i left join public.district_users u on u.district_id=i.district_id and u.user_id=i.reviewed_by
  where i.district_id=p_district_id and i.status='RESOLVED' and i.account_card_draft is not null and i.reviewed_by is not null
  group by i.reviewed_by
 ) a),'[]'::jsonb));
$$;
revoke all on function public.bpp_completed_account_cards(uuid,uuid,text,integer) from public,anon,authenticated;
grant execute on function public.bpp_completed_account_cards(uuid,uuid,text,integer) to service_role;
-- The existing confirmation RPC also supports district admins printing their team's cards.
create or replace function public.bpp_confirm_account_card_prints(p_district_id uuid,p_actor_id uuid,p_cards jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare c record; district_admin boolean;
begin
 if p_actor_id is null or jsonb_typeof(p_cards)!='array' or jsonb_array_length(p_cards) not between 1 and 1000 then raise exception 'Invalid print confirmation';end if;
 select exists(select 1 from public.district_users where district_id=p_district_id and user_id=p_actor_id and role='admin') into district_admin;
 for c in select * from jsonb_to_recordset(p_cards) as x(id uuid,version integer) order by id loop
   perform 1 from public.bpp_intelligence_items i where i.id=c.id and i.district_id=p_district_id
    and (i.reviewed_by=p_actor_id or district_admin) and i.status='RESOLVED' and i.account_card_draft is not null
    and i.account_card_version=c.version and c.version>0 for update;
   if not found then raise exception 'PRINT_CARD_CONFLICT';end if;
 end loop;
 insert into public.bpp_account_card_prints(item_id,version,district_id,printed_by)
 select distinct x.id,x.version,p_district_id,p_actor_id from jsonb_to_recordset(p_cards) as x(id uuid,version integer)
 on conflict(item_id,version) do nothing;
 return (select jsonb_agg(to_jsonb(p)) from public.bpp_account_card_prints p
  where p.district_id=p_district_id and exists(select 1 from jsonb_to_recordset(p_cards) as x(id uuid,version integer) where x.id=p.item_id and x.version=p.version));
end $$;
notify pgrst,'reload schema';
commit;
