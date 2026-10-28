begin;
-- Operator opt-in for the verified Lubbock public guest route, not county endorsement.
alter table public.county_clerk_sources drop constraint if exists county_clerk_sources_automation_enabled_check;
alter table public.county_clerk_sources add column if not exists operator_authorized_at timestamptz;
alter table public.county_clerk_sources add column if not exists operator_authorization_basis text;
alter table public.county_clerk_sources add constraint county_clerk_public_route_check check (
 not automation_enabled or (automation_authorized and operator_authorized_at is not null
 and operator_authorization_basis is not null and retrieval_method='public_guest_html'
 and state='TX' and county='LUBBOCK'
 and source_url='https://erecord.lubbockcounty.gov/recorder/web/splash.jsp'));
alter table public.county_clerk_collection_runs add column if not exists lease_token uuid;
alter table public.county_clerk_collection_runs add column if not exists lease_until timestamptz;
alter table public.county_clerk_collection_runs add column if not exists attempt_count integer not null default 0;
alter table public.county_clerk_collection_runs add column if not exists next_attempt_at timestamptz;
alter table public.county_clerk_collection_runs add column if not exists last_attempt_at timestamptz;

create or replace function public.dba_claim_collection(p_run_id uuid)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare r public.county_clerk_collection_runs%rowtype; src public.county_clerk_sources%rowtype;
begin
 select * into r from public.county_clerk_collection_runs where id=p_run_id for update;
 if not found then raise exception 'Collection run not found';end if;
 select * into src from public.county_clerk_sources where id=r.source_id;
 if not src.automation_enabled or not src.automation_authorized or src.retrieval_method!='public_guest_html'
 then return jsonb_build_object('claimed',false,'reason','DISABLED');end if;
 if r.status='RUNNING' and r.lease_until<=now() and r.attempt_count>=3 then
  update public.county_clerk_collection_runs set status='FAILED',message='Collection worker lease expired after three attempts; administrator review required.',lease_until=null,lease_token=null,completed_at=now() where id=p_run_id;
  return jsonb_build_object('claimed',false,'reason','FAILED');
 end if;
 if r.status='COMPLETED' or r.attempt_count>=3 or r.lease_until>now() or r.next_attempt_at>now()
 then return jsonb_build_object('claimed',false,'reason',r.status);end if;
 update public.county_clerk_collection_runs set status='RUNNING',lease_token=gen_random_uuid(),lease_until=now()+interval '2 hours',
 attempt_count=attempt_count+1,last_attempt_at=now(),next_attempt_at=null,completed_at=null,
 message='Collecting public guest records at a low request rate.' where id=p_run_id returning * into r;
 return jsonb_build_object('claimed',true,'run',to_jsonb(r));
end $$;

create or replace function public.dba_collection_heartbeat(p_run_id uuid,p_token uuid)
returns boolean language plpgsql security definer set search_path=public,pg_temp as $$
begin
 update public.county_clerk_collection_runs set lease_until=now()+interval '2 hours'
 where id=p_run_id and lease_token=p_token and status='RUNNING'
 and exists(select 1 from public.county_clerk_sources s where s.id=source_id and s.automation_enabled and s.automation_authorized);
 return found;
end $$;

create or replace function public.dba_finish_collection(p_run_id uuid,p_token uuid,p_import_id uuid default null,p_error text default null)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare r public.county_clerk_collection_runs%rowtype; i public.county_clerk_dba_imports%rowtype;
begin
 select * into r from public.county_clerk_collection_runs where id=p_run_id for update;
 if not found or r.lease_token is distinct from p_token or r.status!='RUNNING' then raise exception 'Collection lease lost';end if;
 if p_error is not null then
  update public.county_clerk_collection_runs set status='FAILED',message=left(p_error,500),lease_until=null,lease_token=null,
  completed_at=now(),attempt_count=case when p_error like 'STOP:%' then 3 else attempt_count end,next_attempt_at=case when p_error not like 'STOP:%' and attempt_count<3 then now()+interval '24 hours' else null end where id=p_run_id returning * into r;
  if p_error like 'STOP:%' then update public.county_clerk_sources set automation_enabled=false where id=r.source_id;end if;
 else
  select * into i from public.county_clerk_dba_imports where id=p_import_id;
  if not found or i.source_id!=r.source_id or i.district_id!=r.district_id or i.jurisdiction_id!=r.jurisdiction_id
   or i.status!='COMPLETED' or i.coverage!='COMPLETE' or i.search_start is distinct from r.window_start or i.search_end is distinct from r.window_end
   or i.extracted_count!=i.imported_count+i.duplicate_count
   or i.metadata->>'collection_method' is distinct from 'public_guest_html' or i.metadata->>'unattended' is distinct from 'true'
  then raise exception 'Complete unattended import evidence required';end if;
  update public.county_clerk_collection_runs set status='COMPLETED',message='Quarterly public guest collection complete; source counts reconciled and matched against BPP accounts.',
  records_retrieved=i.extracted_count,imported_count=i.imported_count,duplicate_count=i.duplicate_count,import_id=i.id,
  lease_until=null,lease_token=null,next_attempt_at=null,completed_at=now() where id=p_run_id returning * into r;
  update public.county_clerk_sources set last_successful_retrieval_at=now(),
  last_complete_recording_date=greatest(last_complete_recording_date,r.window_end) where id=r.source_id;
 end if;
 return to_jsonb(r);
end $$;
revoke all on function public.dba_claim_collection(uuid) from public,anon,authenticated;
revoke all on function public.dba_collection_heartbeat(uuid,uuid) from public,anon,authenticated;
revoke all on function public.dba_finish_collection(uuid,uuid,uuid,text) from public,anon,authenticated;
grant execute on function public.dba_claim_collection(uuid) to service_role;
grant execute on function public.dba_collection_heartbeat(uuid,uuid) to service_role;
grant execute on function public.dba_finish_collection(uuid,uuid,uuid,text) to service_role;
notify pgrst,'reload schema';
commit;
