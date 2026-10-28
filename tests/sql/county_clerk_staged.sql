-- Run after the migration in a disposable database with one configured source.
-- All test records are rolled back. Never changes a taxable account.
begin;
do $$
declare src county_clerk_sources%rowtype; first_id uuid:=gen_random_uuid(); second_id uuid:=gen_random_uuid(); bad_id uuid:=gen_random_uuid(); payload jsonb; result jsonb; item_id uuid; count_before integer;
begin
 select * into src from county_clerk_sources limit 1;
 if src.id is null then raise exception 'Configure a source first';end if;
 payload:=jsonb_build_array(jsonb_build_object('document_number','DBA-ROLLBACK-TEST','filing_type','ASSUMED NAME','recording_date','2026-01-02','assumed_name','Rollback Test Business','owner_name_complete',false,'source_pages',jsonb_build_array(1),'raw_text','Test only','intelligence',jsonb_build_object('classification','DBA_NEEDS_REVIEW','priority','LOW','confidence','LOW','confidence_score',0.25,'is_ambiguous',false,'evidence',jsonb_build_object('notice','Test only'))));
 insert into county_clerk_dba_imports(id,source_id,jurisdiction_id,district_id,filename,sha256,storage_path,parser_version,coverage,page_count,extracted_count)
 values(first_id,src.id,src.jurisdiction_id,src.district_id,'rollback-test.pdf','test','test','test','INCOMPLETE',1,1);
 update county_clerk_dba_imports set status='PROCESSING' where id=first_id;
 begin
   perform dba_commit_staged_import(first_id);
   raise exception 'Incomplete stage accepted';
 exception when raise_exception then
   if sqlerrm!='Incomplete staged filing set' then raise;end if;
 end;
 perform dba_stage_import_batch(first_id,0,payload);
 perform dba_stage_import_batch(first_id,0,payload);
 begin
   perform dba_stage_import_batch(first_id,0,jsonb_build_array(jsonb_set(payload->0,'{assumed_name}','"Different"')));
   raise exception 'Changed stage accepted';
 exception when raise_exception then
   if sqlerrm!='Original import batch differs' then raise;end if;
 end;
 result:=dba_commit_staged_import(first_id);
 if (result->>'imported_count')::int!=1 or result->>'status'!='PARTIAL' then raise exception 'Initial import failed: %',result;end if;
 select id into item_id from bpp_intelligence_items where source_dba_filing_id=(select id from county_clerk_dba_filings where first_import_id=first_id);
 update bpp_intelligence_items set status='RESOLVED',resolution='OTHER',resolution_notes='Independent research completed' where id=item_id;
 if not exists(select from county_clerk_dba_review_events where intelligence_item_id=item_id and after_state->>'status'='RESOLVED') then raise exception 'Missing audit';end if;
 insert into county_clerk_dba_imports(id,source_id,jurisdiction_id,district_id,filename,sha256,storage_path,parser_version,coverage,page_count,extracted_count)
 values(second_id,src.id,src.jurisdiction_id,src.district_id,'rollback-test.pdf','test','test2','test','INCOMPLETE',1,1);
 result:=dba_commit_import(second_id,payload);
 if (result->>'duplicate_count')::int!=1 or (result->>'imported_count')::int!=0 then raise exception 'Dedup failed';end if;
 if (select status from bpp_intelligence_items where id=item_id)!='RESOLVED' then raise exception 'Review overwritten';end if;
 if (select count(*) from county_clerk_dba_observations where import_id in(first_id,second_id))!=2 then raise exception 'Lost source observation';end if;
 result:=dba_commit_import(second_id,payload);
 if (result->>'duplicate_count')::int!=1 then raise exception 'RPC idempotence failed';end if;
 insert into county_clerk_dba_imports(id,source_id,jurisdiction_id,district_id,filename,sha256,storage_path,parser_version,coverage,page_count,extracted_count)
 values(bad_id,src.id,src.jurisdiction_id,src.district_id,'bad.pdf','test','bad','test','INCOMPLETE',1,2);
 select count(*) into count_before from county_clerk_dba_filings;
 begin
   perform dba_commit_import(bad_id,jsonb_build_array(jsonb_set(payload->0,'{document_number}','"DBA-ATOMIC-TEST"'),jsonb_set(payload->0,'{filing_type}','"INVALID"')));
   raise exception 'Bad row should fail';
 exception when check_violation then null;
 end;
 if (select count(*) from county_clerk_dba_filings)!=count_before then raise exception 'Atomic rollback failed';end if;
 if has_function_privilege('anon','public.dba_commit_import(uuid,jsonb)','EXECUTE') or has_function_privilege('authenticated','public.dba_match_context(uuid,uuid,text[])','EXECUTE') then raise exception 'Public RPC access';end if;
 if has_table_privilege('authenticated','public.county_clerk_dba_filings','SELECT') then raise exception 'Public filing access';end if;
 if exists(select from county_clerk_sources where automation_enabled) then raise exception 'Automation enabled';end if;
 perform dba_match_context(src.district_id,src.jurisdiction_id,array['Rollback Test Business']);
 raise notice 'DBA SQL checks passed: atomicity, dedup, preserved reviews, source observations, audit and access restrictions';
end $$;
rollback;
