begin;
alter table public.comptroller_closure_reviews add column if not exists assigned_to uuid;
alter table public.comptroller_closure_reviews add column if not exists assigned_at timestamptz;
alter table public.bpp_intelligence_items add column if not exists assigned_at timestamptz;
create table public.bpp_assignment_settings (
 district_id uuid primary key references public.districts(id), enabled boolean not null default false,
 roster jsonb not null default '[]' check(jsonb_typeof(roster)='array'),updated_by uuid,updated_at timestamptz not null default now()
);
create table public.bpp_assignment_events (
 id uuid primary key default gen_random_uuid(),district_id uuid not null references public.districts(id),
 source_table text not null,item_id uuid not null,work_key text not null,
 previous_assignee uuid,assigned_to uuid not null,actor_id uuid,reason text not null,occurred_at timestamptz not null default now()
);
create index bpp_assignment_history_idx on public.bpp_assignment_events(district_id,occurred_at desc);
create index bpp_active_assignee_idx on public.bpp_intelligence_items(district_id,assigned_to) where status in ('NEW','IN_REVIEW');
create index closure_active_assignee_idx on public.comptroller_closure_reviews(district_id,assigned_to) where workflow_status in ('PENDING_REVIEW','OTHER_NEEDS_RESEARCH');
create index bpp_assignment_rotation_idx on public.bpp_assignment_events(district_id,assigned_to,occurred_at desc);
create or replace view public.bpp_active_assignment_work as
 select district_id,'bpp_intelligence_items'::text source_table,id,
 case when nullif(trim(matched_account_number),'') is not null then 'account:'||upper(trim(matched_account_number)) else 'item:bpp_intelligence_items:'||id end work_key,
 assigned_to,assigned_at,status='IN_REVIEW' busy,reviewed_by,
 case priority when 'HIGH' then 0 when 'MEDIUM' then 1 else 2 end tier,created_at
 from public.bpp_intelligence_items where status in ('NEW','IN_REVIEW')
 and (signal_type<>'mailing_address_change' or (nullif(trim(matched_account_number),'') is not null and
 (source='returned_mail_scan' or evidence->>'comparison_version'='2' or
 (mailing_address_current is not null and mailing_address_observed is not null and upper(trim(mailing_address_current))<>upper(trim(mailing_address_observed))
 and classification in ('CONFIRMED_MAILING_ADDRESS_CHANGE','LIKELY_MAILING_ADDRESS_CHANGE','POSSIBLE_MAILING_ADDRESS_CHANGE')))))
 union all
 select district_id,'comptroller_closure_reviews',id,
 case when nullif(trim(matched_account_number),'') is not null then 'account:'||upper(trim(matched_account_number)) else 'item:comptroller_closure_reviews:'||id end,
 assigned_to,assigned_at,workflow_status='OTHER_NEEDS_RESEARCH',reviewed_by,1,created_at
 from public.comptroller_closure_reviews where workflow_status in ('PENDING_REVIEW','OTHER_NEEDS_RESEARCH');
revoke all on public.bpp_active_assignment_work from public,anon,authenticated;
grant select on public.bpp_active_assignment_work to service_role;

-- Uses the same district lock as bpp_claim_work: allocation cannot race a start.
create or replace function public.bpp_allocate_account(p_district_id uuid,p_work_key text)
returns integer language plpgsql security definer set search_path=public,pg_temp as $$
declare roster jsonb;target uuid;owners integer;busy boolean;row record;n integer:=0;label text;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_district_id::text,0));
 select s.roster into roster from bpp_assignment_settings s where s.district_id=p_district_id and enabled;
 if roster is null then return 0;end if;
 if not exists(select 1 from bpp_active_assignment_work w where w.district_id=p_district_id and w.work_key=p_work_key and assigned_to is null) then return 0;end if;
 select actor_id into target from bpp_work_claims where district_id=p_district_id and work_key=p_work_key;
 if target is null then
  select count(distinct assigned_to),bool_or(w.busy) into owners,busy from bpp_active_assignment_work w where district_id=p_district_id and work_key=p_work_key;
  if owners>1 then return 0;end if; -- Conflicting existing owners require admin review.
  if owners=1 then
   select assigned_to into target from bpp_active_assignment_work where district_id=p_district_id and work_key=p_work_key and assigned_to is not null limit 1;
  elsif busy then
   select reviewed_by into target from bpp_active_assignment_work where district_id=p_district_id and work_key=p_work_key and busy and reviewed_by is not null limit 1;
   if target is null then return 0;end if; -- Do not guess ownership of started work.
  end if;
 end if;
 if target is null then
  select (r->>'id')::uuid into target from jsonb_array_elements(roster) r
  join district_users u on u.district_id=p_district_id and u.user_id=(r->>'id')::uuid
  order by (select count(distinct w.work_key) from bpp_active_assignment_work w where w.district_id=p_district_id and w.assigned_to=u.user_id),
   (select max(e.occurred_at) from bpp_assignment_events e where e.district_id=p_district_id and e.assigned_to=u.user_id) nulls first,u.user_id limit 1;
 end if;
 if target is null then return 0;end if;
 -- Preserve a real reservation even if its actor is not in today's automatic roster.
 if not exists(select 1 from district_users where district_id=p_district_id and user_id=target) then return 0;end if;
 for row in select * from bpp_active_assignment_work where district_id=p_district_id and work_key=p_work_key and assigned_to is null loop
  if row.source_table='bpp_intelligence_items' then
   update bpp_intelligence_items set assigned_to=target,assigned_at=clock_timestamp() where id=row.id and assigned_to is null and status in ('NEW','IN_REVIEW');
  else
   update comptroller_closure_reviews set assigned_to=target,assigned_at=clock_timestamp() where id=row.id and assigned_to is null and workflow_status in ('PENDING_REVIEW','OTHER_NEEDS_RESEARCH');
  end if;
  if found then
   insert into bpp_assignment_events(district_id,source_table,item_id,work_key,assigned_to,reason) values(p_district_id,row.source_table,row.id,p_work_key,target,'Automatic allocation by open account workload');n:=n+1;
  end if;
 end loop;
 return n;
end $$;

create or replace function public.bpp_balance_pending(p_district_id uuid)
returns integer language plpgsql security definer set search_path=public,pg_temp as $$
declare row record;n integer:=0;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_district_id::text,0));
 for row in select work_key,min(tier) tier,min(created_at) oldest from bpp_active_assignment_work where district_id=p_district_id and assigned_to is null group by work_key order by min(tier),min(created_at),work_key loop
  n:=n+bpp_allocate_account(p_district_id,row.work_key);
 end loop;
 return n;
end $$;

create or replace function public.bpp_assignment_on_arrival() returns trigger
language plpgsql security definer set search_path=public,pg_temp as $$
declare key text;
begin
 key:=case when nullif(trim(new.matched_account_number),'') is not null then 'account:'||upper(trim(new.matched_account_number)) else 'item:'||tg_table_name||':'||new.id end;
 perform bpp_allocate_account(new.district_id,key);
 return new;
end $$;
create trigger bpp_auto_assign_arrival after insert on public.bpp_intelligence_items for each row execute function bpp_assignment_on_arrival();
create trigger closure_auto_assign_arrival after insert on public.comptroller_closure_reviews for each row execute function bpp_assignment_on_arrival();
create trigger bpp_auto_assign_changed after update of status,matched_account_number on public.bpp_intelligence_items for each row when(old.status is distinct from new.status or old.matched_account_number is distinct from new.matched_account_number) execute function bpp_assignment_on_arrival();
create trigger closure_auto_assign_changed after update of workflow_status,matched_account_number on public.comptroller_closure_reviews for each row when(old.workflow_status is distinct from new.workflow_status or old.matched_account_number is distinct from new.matched_account_number) execute function bpp_assignment_on_arrival();

create or replace function public.bpp_configure_assignments(p_district_id uuid,p_actor_id uuid,p_enabled boolean,p_roster jsonb)
returns integer language plpgsql security definer set search_path=public,pg_temp as $$
begin
 perform pg_advisory_xact_lock(hashtextextended(p_district_id::text,0));
 if not exists(select 1 from district_users where district_id=p_district_id and user_id=p_actor_id and role='admin') then raise exception 'District admin required';end if;
 if jsonb_typeof(p_roster) is distinct from 'array' or (p_enabled and jsonb_array_length(p_roster)=0) then raise exception 'At least one BPP appraiser is required';end if;
 if exists(select 1 from jsonb_array_elements(p_roster) r where not exists(select 1 from district_users where district_id=p_district_id and user_id=(r->>'id')::uuid)) then raise exception 'Roster contains a user outside this district';end if;
 insert into bpp_assignment_settings(district_id,enabled,roster,updated_by) values(p_district_id,p_enabled,p_roster,p_actor_id)
 on conflict(district_id) do update set enabled=excluded.enabled,roster=excluded.roster,updated_by=excluded.updated_by,updated_at=clock_timestamp();
 if p_enabled then return bpp_balance_pending(p_district_id);end if;
 return 0;
end $$;

create or replace function public.bpp_reassign_account(p_district_id uuid,p_actor_id uuid,p_work_key text,p_target uuid)
returns integer language plpgsql security definer set search_path=public,pg_temp as $$
declare row record;n integer:=0;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_district_id::text,0));
 if not exists(select 1 from district_users where district_id=p_district_id and user_id=p_actor_id and role='admin') then raise exception 'District admin required';end if;
 if not exists(select 1 from bpp_assignment_settings s,jsonb_array_elements(s.roster) r where s.district_id=p_district_id and (r->>'id')::uuid=p_target) or not exists(select 1 from district_users where district_id=p_district_id and user_id=p_target) then raise exception 'Choose a current BPP appraiser';end if;
 if exists(select 1 from bpp_work_claims where district_id=p_district_id and work_key=p_work_key) then raise exception 'Started work must be finished or released before reassignment';end if;
 for row in select * from bpp_active_assignment_work where district_id=p_district_id and work_key=p_work_key and assigned_to is distinct from p_target loop
  if row.source_table='bpp_intelligence_items' then update bpp_intelligence_items set assigned_to=p_target,assigned_at=clock_timestamp() where id=row.id and status in ('NEW','IN_REVIEW');
  else update comptroller_closure_reviews set assigned_to=p_target,assigned_at=clock_timestamp() where id=row.id and workflow_status in ('PENDING_REVIEW','OTHER_NEEDS_RESEARCH');end if;
  if found then insert into bpp_assignment_events(district_id,source_table,item_id,work_key,previous_assignee,assigned_to,actor_id,reason) values(p_district_id,row.source_table,row.id,p_work_key,row.assigned_to,p_target,p_actor_id,'Admin reassignment');n:=n+1;end if;
 end loop;
 return n;
end $$;

alter table public.bpp_assignment_settings enable row level security;
alter table public.bpp_assignment_events enable row level security;
revoke all on public.bpp_assignment_settings,public.bpp_assignment_events from public,anon,authenticated;
grant select,insert,update on public.bpp_assignment_settings to service_role;
grant select,insert on public.bpp_assignment_events to service_role;
revoke update,delete,truncate on public.bpp_assignment_events from service_role;
revoke all on function public.bpp_allocate_account(uuid,text),public.bpp_balance_pending(uuid),public.bpp_assignment_on_arrival(),public.bpp_configure_assignments(uuid,uuid,boolean,jsonb),public.bpp_reassign_account(uuid,uuid,text,uuid) from public,anon,authenticated;
grant execute on function public.bpp_allocate_account(uuid,text),public.bpp_balance_pending(uuid),public.bpp_configure_assignments(uuid,uuid,boolean,jsonb),public.bpp_reassign_account(uuid,uuid,text,uuid) to service_role;
-- Preserve existing reservations; new starts honor the saved allocation.
create or replace function public.bpp_claim_work(p_district_id uuid,p_work_key text,p_actor_id uuid,p_actor_email text,p_source_table text,p_item_id text)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare result public.bpp_work_claims;owner uuid;owner_email text;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_district_id::text,0));
 select * into result from bpp_work_claims where district_id=p_district_id and actor_id<>p_actor_id and (work_key=p_work_key or (source_table=p_source_table and item_id=p_item_id)) limit 1;
 if found then return to_jsonb(result);end if;
 select * into result from bpp_work_claims where district_id=p_district_id and (work_key=p_work_key or (source_table=p_source_table and item_id=p_item_id)) limit 1;
 if found then return to_jsonb(result);end if;
 if exists(select 1 from bpp_assignment_settings where district_id=p_district_id and enabled) then
  select assigned_to into owner from bpp_active_assignment_work where district_id=p_district_id and source_table=p_source_table and id::text=p_item_id;
  if owner is not null and owner<>p_actor_id then
   select email into owner_email from district_users where district_id=p_district_id and user_id=owner limit 1;
   return jsonb_build_object('actor_id',owner,'actor_email',coalesce(owner_email,'another appraiser'),'assignment_only',true);
  end if;
 end if;
 insert into bpp_work_claims(district_id,work_key,actor_id,actor_email,source_table,item_id) values(p_district_id,p_work_key,p_actor_id,p_actor_email,p_source_table,p_item_id) on conflict(district_id,work_key) do nothing;
 select * into result from bpp_work_claims where district_id=p_district_id and work_key=p_work_key;
 return to_jsonb(result);
end $$;
commit;
