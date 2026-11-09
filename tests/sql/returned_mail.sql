begin;
do $$
declare d uuid:=gen_random_uuid();j uuid:=gen_random_uuid();actor uuid:=gen_random_uuid();other_actor uuid:=gen_random_uuid();
 p jsonb;r jsonb;doc uuid;item uuid;new_item uuid;other_d uuid:=gen_random_uuid();other_j uuid:=gen_random_uuid();
begin
 insert into districts(id,name,slug) values(d,'Return test','return-'||d),(other_d,'Other test','return-'||other_d);
 insert into jurisdictions(id,district_id,name,slug,county_name) values(j,d,'Return test','return-'||j,'Lubbock'),(other_j,other_d,'Other','return-'||other_j,'Dallam');
 p:=jsonb_build_object('account_number','P123456','filename','envelope.pdf','relative_path','Returned Mail/P123456/envelope.pdf',
  'sha256',repeat('a',64),'storage_path',d||'/P123456/'||repeat('a',64)||'.pdf','byte_count',100,'content_type','application/pdf','page_count',2,
  'matched_record_id',gen_random_uuid(),'owner_name','Test only','crs_address','100 Main St','ocr_text','UNABLE TO FORWARD',
  'ocr_status','READ','ocr_warnings','[]'::jsonb,'suggested_reason','UNABLE_TO_FORWARD','printed_accounts','[]'::jsonb,'account_mismatch',false);
 r:=returned_mail_commit(d,j,actor,p);doc:=(r->>'document_id')::uuid;item:=(r->>'item_id')::uuid;
 if (r->>'duplicate')::boolean then raise exception 'First scan incorrectly duplicate';end if;
 r:=returned_mail_commit(d,j,actor,p);
 if not (r->>'duplicate')::boolean or (r->>'document_id')::uuid!=doc then raise exception 'Duplicate not suppressed';end if;
 if (select count(*) from returned_mail_documents where district_id=d)!=1 then raise exception 'Duplicate documents';end if;
 update bpp_intelligence_items set status='IN_REVIEW',assigned_to=actor,resolution_notes='Keep work' where id=item;
 r:=returned_mail_commit(d,j,actor,p||jsonb_build_object('sha256',repeat('b',64),'storage_path',d||'/P123456/'||repeat('b',64)||'.pdf'));
 if (r->>'item_id')::uuid!=item or (select resolution_notes from bpp_intelligence_items where id=item)!='Keep work'
  or (select status from bpp_intelligence_items where id=item)!='IN_REVIEW' then raise exception 'New evidence reset work';end if;
 begin
  perform returned_mail_review(d,actor,doc,0,'{"review_status":"REVIEWED","return_reason":"UNKNOWN"}');raise exception 'Expected reservation guard';
 exception when raise_exception then if sqlerrm!='Reserve this account before reviewing its returned mail' then raise;end if;end;
 insert into bpp_work_claims(district_id,work_key,actor_id) values(d,'account:P123456',actor);
 r:=returned_mail_review(d,actor,doc,0,'{"review_status":"REVIEWED","return_reason":"UNABLE_TO_FORWARD","forwarding_address":"","review_notes":"Checked original"}');
 if r->>'version'!='1' or r->>'forwarding_address' is not null then raise exception 'Review not saved';end if;
 if (select count(*) from returned_mail_review_events where document_id=doc)!=1 then raise exception 'Audit missing';end if;
 begin
  perform returned_mail_review(d,actor,doc,0,'{"review_status":"REVIEWED","return_reason":"UNKNOWN"}');raise exception 'Expected stale version guard';
 exception when raise_exception then if sqlerrm!='This scan changed. Reload before saving.' then raise;end if;end;
 begin
  perform returned_mail_review(d,other_actor,doc,1,'{"review_status":"REVIEWED","return_reason":"UNKNOWN"}');raise exception 'Expected ownership guard';
 exception when raise_exception then if sqlerrm!='Reserve this account before reviewing its returned mail' then raise;end if;end;
 begin
  perform returned_mail_review(other_d,actor,doc,1,'{"review_status":"REVIEWED","return_reason":"UNKNOWN"}');raise exception 'Expected district guard';
 exception when raise_exception then if sqlerrm!='Returned-mail scan not found' then raise;end if;end;
 begin
  perform returned_mail_resolve(d,actor,item,'CURRENT_ADDRESS_CORRECT','Checked');raise exception 'Expected pending-scan guard';
 exception when raise_exception then if sqlerrm!='Review each returned-mail scan before completing this account' then raise;end if;end;
 perform returned_mail_review(d,actor,id,version,'{"review_status":"REVIEWED","return_reason":"UNKNOWN"}')
  from returned_mail_documents where intelligence_item_id=item and review_status='PENDING';
 perform returned_mail_resolve(d,actor,item,'CURRENT_ADDRESS_CORRECT','Checked');
 if (select status from bpp_intelligence_items where id=item)!='RESOLVED' then raise exception 'Completion not saved';end if;
 begin
  perform returned_mail_review(d,actor,doc,1,'{"review_status":"REVIEWED","return_reason":"UNKNOWN"}');raise exception 'Expected closed review guard';
 exception when raise_exception then if sqlerrm!='Reopen the returned-mail review before changing this scan' then raise;end if;end;
 r:=returned_mail_commit(d,j,actor,p);
 if not (r->>'duplicate')::boolean or (select status from bpp_intelligence_items where id=item)!='RESOLVED' then raise exception 'Duplicate reopened history';end if;
 r:=returned_mail_commit(d,j,actor,p||jsonb_build_object('sha256',repeat('c',64),'storage_path',d||'/P123456/'||repeat('c',64)||'.pdf','account_mismatch',true));
 new_item:=(r->>'item_id')::uuid;
 if new_item=item or (select priority from bpp_intelligence_items where id=new_item)!='MEDIUM' then raise exception 'Repeat return not separate work/mismatch ignored';end if;
 r:=returned_mail_commit(other_d,other_j,actor,p||jsonb_build_object('storage_path',other_d||'/P123456/'||repeat('a',64)||'.pdf'));
 if (r->>'duplicate')::boolean then raise exception 'Cross-county collision';end if;
 if has_table_privilege('authenticated','returned_mail_documents','select') or has_function_privilege('anon','returned_mail_commit(uuid,uuid,uuid,jsonb)','execute')
 or has_function_privilege('authenticated','returned_mail_review(uuid,uuid,uuid,integer,jsonb)','execute') then raise exception 'Public access';end if;
 if (select public from storage.buckets where id='returned-mail') then raise exception 'Public scans bucket';end if;
 raise notice 'Returned-mail checks passed: dedup, repeat evidence, preserved history, reservation/version/district guards, audit and private storage';
end $$;
rollback;
