begin;
do $$
declare src county_clerk_sources%rowtype;r county_clerk_collection_runs%rowtype;cl jsonb;tok uuid;imp uuid:=gen_random_uuid(); result jsonb;
begin
 select * into src from county_clerk_sources limit 1;
 update county_clerk_sources set automation_authorized=true,automation_enabled=true,retrieval_method='public_guest_html',operator_authorized_at=now(),operator_authorization_basis='Disposable test only' where id=src.id;
 select * into r from jsonb_populate_record(null::county_clerk_collection_runs,dba_plan_quarterly_run(src.id,'2026-07-01'));
 cl:=dba_claim_collection(r.id);if cl->>'claimed'!='true' then raise exception 'Claim failed';end if;
 tok:=(cl->'run'->>'lease_token')::uuid;
 if dba_claim_collection(r.id)->>'claimed'!='false' then raise exception 'Concurrent claim accepted';end if;
 if not dba_collection_heartbeat(r.id,tok) then raise exception 'Heartbeat failed';end if;
 begin
  perform dba_finish_collection(r.id,gen_random_uuid(),null,'Test');raise exception 'Foreign lease accepted';
 exception when raise_exception then if sqlerrm!='Collection lease lost' then raise;end if;end;
 insert into county_clerk_dba_imports(id,source_id,jurisdiction_id,district_id,filename,sha256,storage_path,parser_version,status,coverage,page_count,extracted_count,imported_count,duplicate_count,search_start,search_end,metadata)
 values(imp,src.id,src.jurisdiction_id,src.district_id,'test.json','test','test','test','COMPLETED','COMPLETE',0,0,0,0,r.window_start,r.window_end,'{}');
 begin
  perform dba_finish_collection(r.id,tok,imp);raise exception 'Manual import accepted';
 exception when raise_exception then if sqlerrm!='Complete unattended import evidence required' then raise;end if;end;
 update county_clerk_dba_imports set metadata='{"collection_method":"public_guest_html","unattended":true}' where id=imp;
 result:=dba_finish_collection(r.id,tok,imp);
 if result->>'status'!='COMPLETED' or result->>'records_retrieved'!='0' then raise exception 'Completion failed';end if;
 if dba_claim_collection(r.id)->>'claimed'!='false' then raise exception 'Completed run repeated';end if;
 if (select last_successful_retrieval_at from county_clerk_sources where id=src.id) is null then raise exception 'No sync timestamp';end if;
 select * into r from jsonb_populate_record(null::county_clerk_collection_runs,dba_plan_quarterly_run(src.id,'2026-04-01'));
 cl:=dba_claim_collection(r.id);tok:=(cl->'run'->>'lease_token')::uuid;
 perform dba_finish_collection(r.id,tok,null,'Failure test');
 if dba_claim_collection(r.id)->>'claimed'!='false' then raise exception 'Retry cooldown skipped';end if;
 update county_clerk_collection_runs set next_attempt_at=now()-interval '1 minute' where id=r.id;
 cl:=dba_claim_collection(r.id);if cl->>'claimed'!='true' then raise exception 'Retry unavailable';end if;
 update county_clerk_collection_runs set attempt_count=3,lease_until=now()-interval '1 minute' where id=r.id;
 if dba_claim_collection(r.id)->>'reason'!='FAILED' then raise exception 'Expired worker not failed';end if;
 if has_function_privilege('authenticated','dba_claim_collection(uuid)','execute') or has_function_privilege('anon','dba_finish_collection(uuid,uuid,uuid,text)','execute') then raise exception 'Public collector access';end if;
 raise notice 'Collection checks passed: exclusive lease, evidence validation, completion, cooldown, bounded retries and service-only access';
end $$;
rollback;
