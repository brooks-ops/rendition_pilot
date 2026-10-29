begin;
do $$
declare src county_clerk_sources%rowtype;item uuid:=gen_random_uuid();actor uuid:=gen_random_uuid();result jsonb;
begin
 select * into src from county_clerk_sources limit 1;
 insert into bpp_intelligence_items(id,district_id,jurisdiction_id,signal_type,source,source_record_id,status,classification)
 values(item,src.district_id,src.jurisdiction_id,'new_business','test','card-rollback','NEW','NO_ACCOUNT_FOUND');
 begin
  perform bpp_save_account_card(item,src.district_id,actor,0,'{}');raise exception 'Unclaimed card saved';
 exception when raise_exception then if sqlerrm!='CARD_WORK_CONFLICT' then raise;end if;end;
 perform bpp_claim_work(src.district_id,'item:bpp_intelligence_items:'||item,actor,'test@example.invalid','bpp_intelligence_items',item::text);
 result:=bpp_save_account_card(item,src.district_id,actor,0,'{"neighborhood":"0722","segments":[{"flat_value":"0"}]}');
 if result->>'version'!='1' then raise exception 'Version not advanced';end if;
 if (select account_card_draft->>'neighborhood' from bpp_intelligence_items where id=item)!='0722' then raise exception 'Code changed';end if;
 if (select count(*) from bpp_account_card_events where item_id=item)!=1 then raise exception 'Missing audit event';end if;
 begin
  perform bpp_save_account_card(item,src.district_id,actor,0,'{}');raise exception 'Stale version saved';
 exception when raise_exception then if sqlerrm!='CARD_VERSION_CONFLICT' then raise;end if;end;
 begin
  perform bpp_save_account_card(item,gen_random_uuid(),actor,1,'{}');raise exception 'Foreign district saved';
 exception when raise_exception then if sqlerrm!='Invalid account card' then raise;end if;end;
 begin
  perform bpp_save_account_card(item,src.district_id,gen_random_uuid(),1,'{}');raise exception 'Foreign appraiser saved';
 exception when raise_exception then if sqlerrm!='CARD_WORK_CONFLICT' then raise;end if;end;
 update bpp_intelligence_items set status='RESOLVED' where id=item;
 begin
  perform bpp_save_account_card(item,src.district_id,actor,1,'{}');raise exception 'Closed review edited';
 exception when raise_exception then if sqlerrm!='CARD_WORK_CONFLICT' then raise;end if;end;
 if (select account_card_version from bpp_intelligence_items where id=item)!=1 or (select count(*) from bpp_account_card_events where item_id=item)!=1 then raise exception 'Failed saves changed draft/audit';end if;
 if has_function_privilege('anon','bpp_save_account_card(uuid,uuid,uuid,integer,jsonb)','execute') or has_table_privilege('authenticated','bpp_account_card_events','select') then raise exception 'Public draft access';end if;
 raise notice 'Account-card checks passed: saved drafts, versions, district/owner isolation, audit and closed-review protection';
end $$;
rollback;
