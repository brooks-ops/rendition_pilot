begin;
do $$
declare d uuid:=gen_random_uuid();j uuid:=gen_random_uuid();actor uuid:=gen_random_uuid();foreign_d uuid:=gen_random_uuid();
p jsonb;r jsonb;job jsonb;replacement jsonb;doc uuid;item uuid;before_review jsonb;
begin
 insert into districts(id,name,slug) values(d,'Research test',d::text);
 insert into jurisdictions(id,district_id,name,slug,county_name) values(j,d,'Research test',j::text,'Lubbock');
 p:=jsonb_build_object('account_number','P123456','filename','P123456.pdf','relative_path','P123456/P123456.pdf','sha256',repeat('a',64),
 'storage_path',d||'/P123456/'||repeat('a',64)||'.pdf','byte_count',100,'content_type','application/pdf','page_count',1,
 'suggested_reason','RETURN_TO_SENDER','ocr_text','RETURN TO SENDER','ocr_status','READ','ocr_warnings','[]'::jsonb,'printed_accounts','[]'::jsonb);
 r:=returned_mail_commit(d,j,actor,p);doc:=(r->>'document_id')::uuid;item:=(r->>'item_id')::uuid;
 if (select count(*) from returned_mail_research where document_id=doc)!=1 then raise exception 'Missing queued job';end if;
 perform returned_mail_commit(d,j,actor,p);
 if (select count(*) from returned_mail_research where document_id=doc)!=1 then raise exception 'Duplicate job';end if;
 if returned_mail_claim_research(foreign_d,doc) is not null then raise exception 'Cross district research claim';end if;
 select to_jsonb(i)-'updated_at' into before_review from bpp_intelligence_items i where id=item;
 job:=returned_mail_claim_research(d,doc);
 if job->>'status'!='RUNNING' or job->>'attempts'!='1' then raise exception 'Job was not claimed';end if;
 if returned_mail_claim_research(d,doc) is not null then raise exception 'Double claim';end if;
 if returned_mail_finish_research(d,doc,gen_random_uuid(),'COMPLETE','{}',null) then raise exception 'Foreign token finished';end if;
 update returned_mail_research set lease_until=now()-interval '1 second' where document_id=doc;
 if returned_mail_finish_research(d,doc,(job->>'lease_token')::uuid,'COMPLETE','{}',null) then raise exception 'Expired worker finished';end if;
 replacement:=returned_mail_claim_research(d,doc);
 if replacement->>'lease_token'=job->>'lease_token' then raise exception 'Lease not rotated';end if;
 if (select status from returned_mail_research_runs where id=(job->>'lease_token')::uuid)!='INTERRUPTED' then raise exception 'Interruption not audited';end if;
 if returned_mail_finish_research(d,doc,(job->>'lease_token')::uuid,'COMPLETE','{}',null) then raise exception 'Old worker overwrote result';end if;
 if not returned_mail_finish_research(d,doc,(replacement->>'lease_token')::uuid,'PARTIAL','{"leads":["example"]}','Internal lookup failed') then raise exception 'Valid worker could not finish';end if;
 if returned_mail_claim_research(d,doc) is not null then raise exception 'Backoff ignored';end if;
 update returned_mail_research set retry_at=now(),attempts=5 where document_id=doc;
 if returned_mail_claim_research(d,doc) is not null then raise exception 'Retry limit ignored';end if;
 update returned_mail_research set attempts=2 where document_id=doc;
 update returned_mail_documents set review_status='REVIEWED' where id=doc;
 if returned_mail_claim_research(d,doc) is not null then raise exception 'Completed scan researched';end if;
 update returned_mail_documents set review_status='PENDING' where id=doc;
 update bpp_intelligence_items set status='RESOLVED' where id=item;
 if returned_mail_claim_research(d,doc) is not null then raise exception 'Completed account researched';end if;
 update bpp_intelligence_items set status='NEW' where id=item;
 if (select to_jsonb(i)-'updated_at' from bpp_intelligence_items i where id=item) is distinct from before_review then raise exception 'Research modified queue state';end if;
 if has_table_privilege('authenticated','returned_mail_research','select') or has_table_privilege('anon','returned_mail_research_runs','select')
 or has_function_privilege('authenticated','returned_mail_claim_research(uuid,uuid)','execute') then raise exception 'Public research access';end if;
 raise notice 'Research checks passed: enqueue/dedup, scope, leases, crash recovery, run history, retry limits and preserved queue';
end $$;
rollback;
