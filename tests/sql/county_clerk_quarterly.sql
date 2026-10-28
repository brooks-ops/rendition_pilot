begin;
do $$
declare src county_clerk_sources%rowtype; a jsonb; b jsonb; due date:=date_trunc('quarter',now() at time zone 'America/Chicago')::date; n integer;
begin
 select * into src from county_clerk_sources limit 1;
 if src.id is null then raise exception 'Configure a source first';end if;
 a:=dba_plan_quarterly_run(src.id,due);
 b:=dba_plan_quarterly_run(src.id,due);
 if a->>'id'!=b->>'id' then raise exception 'Repeated schedule created duplicate jobs';end if;
 if a->>'status'!='BLOCKED' or a->>'records_retrieved' is not null or a->>'completed_at' is not null
 then raise exception 'Blocked plan fabricated collection success';end if;
 if (a->>'window_start')::date!=date_trunc('year',due-1)::date or (a->>'window_end')::date!=due-1
 then raise exception 'Wrong YTD coverage';end if;
 update county_clerk_collection_runs set message='Preserve failure evidence',status='FAILED' where id=(a->>'id')::uuid;
 b:=dba_plan_quarterly_run(src.id,due);
 if b->>'status'!='FAILED' or b->>'message'!='Preserve failure evidence' then raise exception 'Repeated planning erased history';end if;
 select count(*) into n from county_clerk_collection_runs where source_id=src.id and scheduled_for=due;
 if n!=1 then raise exception 'Duplicate quarterly plan';end if;
 begin
  perform dba_plan_quarterly_run(src.id,due+1);
  raise exception 'Non-quarter date should fail';
 exception when raise_exception then
  if sqlerrm='Non-quarter date should fail' then raise;end if;
 end;
 if has_function_privilege('anon','public.dba_plan_quarterly_run(uuid,date)','EXECUTE')
 or has_function_privilege('authenticated','public.dba_plan_quarterly_run(uuid,date)','EXECUTE')
 or has_table_privilege('authenticated','public.county_clerk_collection_runs','SELECT') then raise exception 'Public planning access';end if;
 if exists(select from county_clerk_sources where automation_enabled) then raise exception 'Unattended collection enabled';end if;
 raise notice 'Quarterly SQL checks passed: YTD windows, idempotent plans, preserved history, no false success, service-only access';
end $$;
rollback;
