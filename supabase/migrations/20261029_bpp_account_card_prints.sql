begin;
create table if not exists public.bpp_account_card_prints (
 item_id uuid not null references public.bpp_intelligence_items(id),
 version integer not null check(version>0),
 district_id uuid not null references public.districts(id),
 printed_by uuid not null,
 printed_at timestamptz not null default now(),
 primary key(item_id,version)
);
alter table public.bpp_account_card_prints enable row level security;
revoke all on public.bpp_account_card_prints from public,anon,authenticated;
grant select,insert on public.bpp_account_card_prints to service_role;
create or replace function public.bpp_confirm_account_card_prints(p_district_id uuid,p_actor_id uuid,p_cards jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare c record;
begin
 if p_actor_id is null or jsonb_typeof(p_cards)!='array' or jsonb_array_length(p_cards) not between 1 and 1000 then raise exception 'Invalid print confirmation';end if;
 -- Lock all cards in a stable order; validate the entire batch before writing.
 for c in select * from jsonb_to_recordset(p_cards) as x(id uuid,version integer) order by id loop
   perform 1 from public.bpp_intelligence_items i where i.id=c.id and i.district_id=p_district_id
    and i.reviewed_by=p_actor_id and i.status='RESOLVED' and i.account_card_draft is not null
    and i.account_card_version=c.version and c.version>0 for update;
   if not found then raise exception 'PRINT_CARD_CONFLICT';end if;
 end loop;
 insert into public.bpp_account_card_prints(item_id,version,district_id,printed_by)
 select distinct x.id,x.version,p_district_id,p_actor_id from jsonb_to_recordset(p_cards) as x(id uuid,version integer)
 on conflict(item_id,version) do nothing;
 return (select jsonb_agg(to_jsonb(p)) from public.bpp_account_card_prints p
  where p.district_id=p_district_id and exists(select 1 from jsonb_to_recordset(p_cards) as x(id uuid,version integer) where x.id=p.item_id and x.version=p.version));
end $$;
revoke all on function public.bpp_confirm_account_card_prints(uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.bpp_confirm_account_card_prints(uuid,uuid,jsonb) to service_role;
notify pgrst,'reload schema';
commit;
