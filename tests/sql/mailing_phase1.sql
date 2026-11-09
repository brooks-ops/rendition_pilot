-- Run only in a disposable DB after 20261102_mailing_crs_comparisons.sql.
-- Requires two fixture jurisdictions/districts, as created below; rolls back.
begin;
do $$
declare d1 uuid:=gen_random_uuid();d2 uuid:=gen_random_uuid();j1 uuid:=gen_random_uuid();j2 uuid:=gen_random_uuid();
 p jsonb;obs jsonb;r jsonb;item uuid;second uuid;legacy_id uuid;run_id uuid;failed boolean:=false;prop uuid:=gen_random_uuid();
begin
 insert into districts(id,name,slug) values(d1,'Mailing test A','mailing-test-'||d1),(d2,'Mailing test B','mailing-test-'||d2);
 insert into jurisdictions(id,district_id,name,slug,county_name) values(j1,d1,'Mailing test A','mailing-test-'||j1,'Mailing test A'),(j2,d2,'Mailing test B','mailing-test-'||j2,'Mailing test B');
 obs:=jsonb_build_object('jurisdiction_id',j1,'account_identifier_type','comptroller_taxpayer','account_identifier','T1',
   'source','test','raw_address_line','100 MAIN ST','normalized_full_address','100 MAIN STREET');
 r:=mailing_record_observation(obs);
 if not (r->>'added')::boolean then raise exception 'First observation missing';end if;
 r:=mailing_record_observation(obs);
 if (r->>'added')::boolean then raise exception 'Duplicate observation';end if;
 perform mailing_record_observation(obs||'{"raw_address_line":"200 MAIN ST","normalized_full_address":"200 MAIN STREET"}');
 perform mailing_record_observation(obs);
 if (select count(*) from mailing_address_observations where jurisdiction_id=j1)!=3 then raise exception 'A B A chronology lost';end if;
 if (select raw_address_line from mailing_address_observations where jurisdiction_id=j1 order by observed_at desc limit 1)!='100 MAIN ST' then raise exception 'Latest is not A';end if;
 perform mailing_record_observation(obs||jsonb_build_object('jurisdiction_id',j2));
 if (select count(*) from mailing_address_observations where jurisdiction_id=j2)!=1 then raise exception 'Observation county collision';end if;

 p:=jsonb_build_object('district_id',d1,'source_taxpayer_id','T1','classification','LIKELY_MAILING_ADDRESS_CHANGE',
  'priority','HIGH','confidence','HIGH','matched_record_id',gen_random_uuid(),'matched_account_number','P1',
  'mailing_address_current','100 MAIN ST','mailing_address_observed','200 MAIN ST',
  'match_signals','{}'::jsonb,'evidence','{"comparison_version":2,"comparison_status":"DISCREPANCY"}'::jsonb);
 r:=mailing_save_comparison(j1,'T1:P1','A',true,p);item:=(r->>'item_id')::uuid;
 if r->>'action'!='CREATED' then raise exception 'No first comparison';end if;
 r:=mailing_save_comparison(j1,'T1:P1','A',true,p);
 if r->>'action'!='UPDATED' or (r->>'item_id')::uuid!=item then raise exception 'Repeated scan duplicates flag';end if;
 update bpp_intelligence_items set status='IN_REVIEW',resolution_notes='Keep staff notes',assigned_to=gen_random_uuid() where id=item;
 r:=mailing_save_comparison(j1,'T1:P1','B',true,p||'{"mailing_address_observed":"300 MAIN ST"}');
 if (select status from bpp_intelligence_items where id=item)!='IN_REVIEW'
   or (select resolution_notes from bpp_intelligence_items where id=item)!='Keep staff notes'
   or (select assigned_to from bpp_intelligence_items where id=item) is null then raise exception 'Review overwritten';end if;
 update bpp_intelligence_items set status='RESOLVED',resolution='CURRENT_ADDRESS_CORRECT',reviewed_at=now() where id=item;
 r:=mailing_save_comparison(j1,'T1:P1','B',true,p);
 if r->>'action'!='SUPPRESSED' then raise exception 'Resolved same comparison reopened';end if;
 r:=mailing_save_comparison(j1,'T1:P1','A',true,p);second:=(r->>'item_id')::uuid;
 if r->>'action'!='CREATED' or second=item then raise exception 'A B A after resolution suppressed';end if;
 if (select status from bpp_intelligence_items where id=item)!='RESOLVED' then raise exception 'Resolved history reset';end if;
 -- Equal/incomplete current evidence refreshes open work without declaring it corrected.
 r:=mailing_save_comparison(j1,'T1:P1','equal',false,p||'{"evidence":{"comparison_version":2,"comparison_status":"NO_CURRENT_DISCREPANCY"}}');
 if r->>'action'!='UPDATED' or (select status from bpp_intelligence_items where id=second)!='NEW' then raise exception 'Auto-resolved staff review';end if;
 update bpp_intelligence_items set status='RESOLVED',resolution='CURRENT_ADDRESS_CORRECT' where id=second;
 perform mailing_save_comparison(j1,'T1:P1','equal',false,p);
 r:=mailing_save_comparison(j1,'T1:P1','B',true,p);
 if r->>'action'!='CREATED' then raise exception 'Later discrepancy suppressed after equal state';end if;
 perform mailing_mark_unreliable(j1,'T1',null,'{"reason":"AMBIGUOUS_LOCATION"}');
 if exists(select 1 from bpp_intelligence_items where jurisdiction_id=j1 and mailing_comparison_key='T1:P1'
  and status in ('NEW','IN_REVIEW') and (confidence!='LOW' or evidence->>'comparison_status'!='ACCOUNT_MATCH_NEEDS_REVIEW'))
 then raise exception 'Stale identity confidence remained';end if;
 -- Same taxpayer/account/source can safely exist in another county.
 r:=mailing_save_comparison(j2,'T1:P1','A',true,p||jsonb_build_object('district_id',d2));
 if r->>'action'!='CREATED' then raise exception 'Comparison county collision';end if;
 begin
   perform mailing_save_comparison(j1,'T1:P1','A',true,p||jsonb_build_object('district_id',d2));
   raise exception 'Expected district rejection';
 exception when raise_exception then if sqlerrm!='Invalid mailing comparison district' then raise;end if;end;

 -- Adopt an open legacy item in place; retain its original evidence and review.
 insert into bpp_intelligence_items(jurisdiction_id,district_id,signal_type,source,source_record_id,source_taxpayer_id,
  status,classification,priority,confidence,matched_account_number,mailing_address_current,resolution_notes)
 values(j1,d1,'mailing_address_change','tx_comptroller_open_data','legacy:T2','T2','IN_REVIEW',
  'LIKELY_MAILING_ADDRESS_CHANGE','HIGH','HIGH','P2','OLD COMPTROLLER','Legacy notes') returning id into legacy_id;
 r:=mailing_save_comparison(j1,'T2:P2','A',true,p||'{"source_taxpayer_id":"T2","matched_account_number":"P2"}');
 if (r->>'item_id')::uuid!=legacy_id or r->>'action'!='UPDATED' then raise exception 'Duplicated legacy review';end if;
 if (select evidence->'legacy_snapshot'->>'previous_mailing' from bpp_intelligence_items where id=legacy_id)!='OLD COMPTROLLER'
  or (select resolution_notes from bpp_intelligence_items where id=legacy_id)!='Legacy notes' then raise exception 'Legacy evidence lost';end if;
 perform mailing_save_comparison(j1,'T2:P2','A',true,p||'{"source_taxpayer_id":"T2","matched_account_number":"P2"}');
 if (select evidence->'legacy_snapshot'->>'previous_mailing' from bpp_intelligence_items where id=legacy_id)!='OLD COMPTROLLER' then raise exception 'Second refresh lost legacy evidence';end if;

 -- Ledger records zero-change success, prevents overlaps, and exposes crashed workers.
 run_id:=mailing_start_scan(j1,d1,'Mailing test A');
 begin
  perform mailing_start_scan(j1,d1,'Mailing test A');raise exception 'Expected overlap rejection';
 exception when raise_exception then if sqlerrm!='A mailing scan is already running' then raise;end if;end;
 update mailing_scan_runs set status='SUCCESS',finished_at=now() where id=run_id;
 if (select records_examined from mailing_scan_runs where id=run_id)!=0 then raise exception 'Zero run missing';end if;
 run_id:=mailing_start_scan(j1,d1,'Mailing test A');
 update mailing_scan_runs set heartbeat_at=now()-interval '3 hours' where id=run_id;
 perform mailing_start_scan(j1,d1,'Mailing test A');
 if (select status from mailing_scan_runs where id=run_id)!='FAILED' then raise exception 'Crashed run not failed';end if;

 -- A reclaimed worker is fenced from observations and queue changes.
 begin
  perform mailing_record_observation(obs||jsonb_build_object('scan_run_id',run_id));
  raise exception 'Expected stale worker rejection';
 exception when raise_exception then if sqlerrm!='Mailing scan lease is no longer active' then raise;end if;end;
 begin
  perform mailing_save_comparison(j1,'T1:P1','stale',true,p||jsonb_build_object('evidence',jsonb_build_object('scan_run_id',run_id)));
  raise exception 'Expected stale comparison rejection';
 exception when raise_exception then if sqlerrm!='Mailing scan lease is no longer active' then raise;end if;end;

 -- Per-field mailing provenance survives sparse later imports.
 insert into real_property_records(id,jurisdiction_id,source_property_id,mailing_address_line1,mailing_city,mailing_field_sources)
 values(prop,j1,'mailing-test','100 MAIN ST','Lubbock','{"mailing_address_line1":{"import_id":"old"},"mailing_city":{"import_id":"old"}}');
 update real_property_records set mailing_city='Other City',mailing_field_sources='{"mailing_city":{"import_id":"new"}}' where id::text=prop::text;
 if (select mailing_field_sources->'mailing_address_line1'->>'import_id' from real_property_records where id::text=prop::text)!='old'
  or (select mailing_field_sources->'mailing_city'->>'import_id' from real_property_records where id::text=prop::text)!='new' then raise exception 'Sparse provenance overwritten';end if;

 if has_function_privilege('anon','mailing_save_comparison(uuid,text,text,boolean,jsonb)','execute')
  or has_function_privilege('authenticated','mailing_record_observation(jsonb)','execute')
  or has_function_privilege('anon','mailing_start_scan(uuid,uuid,text)','execute')
  or has_function_privilege('authenticated','mailing_mark_unreliable(uuid,text,text,jsonb)','execute')
  or has_table_privilege('authenticated','mailing_scan_runs','select') then raise exception 'Public mailing access';end if;
 raise notice 'Mailing SQL checks passed: chronology, dedup, county isolation, preserved reviews, legacy adoption, ledger, provenance and access';
end $$;
rollback;
