begin;
alter table public.bpp_intelligence_items add column if not exists account_card_draft jsonb;
alter table public.bpp_intelligence_items add column if not exists account_card_version integer not null default 0;
alter table public.bpp_intelligence_items add column if not exists account_card_saved_at timestamptz;
alter table public.bpp_intelligence_items add column if not exists account_card_saved_by uuid;
create table if not exists public.bpp_account_card_events (
 id uuid primary key default gen_random_uuid(),district_id uuid not null references public.districts(id),
 item_id uuid not null references public.bpp_intelligence_items(id),actor_id uuid not null,
 version integer not null,data jsonb not null,saved_at timestamptz not null default now(),unique(item_id,version)
);
alter table public.bpp_account_card_events enable row level security;
revoke all on public.bpp_account_card_events from public,anon,authenticated;
grant select,insert on public.bpp_account_card_events to service_role;
create or replace function public.bpp_save_account_card(p_item_id uuid,p_district_id uuid,p_actor_id uuid,p_version integer,p_data jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare i public.bpp_intelligence_items%rowtype; saved timestamptz:=now();
begin
 perform pg_advisory_xact_lock(hashtextextended(p_district_id::text,0));
 select * into i from public.bpp_intelligence_items where id=p_item_id and district_id=p_district_id for update;
 if not found or i.signal_type not in ('new_business','county_clerk_dba') or jsonb_typeof(p_data)!='object' then raise exception 'Invalid account card';end if;
 if i.status in ('RESOLVED','DISMISSED') or not exists(select 1 from public.bpp_work_claims c where c.district_id=p_district_id and c.actor_id=p_actor_id and ((c.source_table='bpp_intelligence_items' and c.item_id=p_item_id::text) or c.work_key='account:'||upper(trim(i.matched_account_number))))
 then raise exception 'CARD_WORK_CONFLICT';end if;
 if i.account_card_version!=p_version then raise exception 'CARD_VERSION_CONFLICT';end if;
 update public.bpp_intelligence_items set account_card_draft=p_data,account_card_version=p_version+1,account_card_saved_at=saved,account_card_saved_by=p_actor_id where id=p_item_id;
 insert into public.bpp_account_card_events(district_id,item_id,actor_id,version,data) values(p_district_id,p_item_id,p_actor_id,p_version+1,p_data);
 return jsonb_build_object('version',p_version+1,'saved_at',saved,'saved_by',p_actor_id);
end $$;
revoke all on function public.bpp_save_account_card(uuid,uuid,uuid,integer,jsonb) from public,anon,authenticated;
grant execute on function public.bpp_save_account_card(uuid,uuid,uuid,integer,jsonb) to service_role;
notify pgrst,'reload schema';
commit;
