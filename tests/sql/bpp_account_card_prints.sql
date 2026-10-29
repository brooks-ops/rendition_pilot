begin;
do $$
declare src county_clerk_sources%rowtype; item uuid:=gen_random_uuid(); actor uuid:=gen_random_uuid(); cards jsonb; result jsonb; stamp timestamptz;
begin
 select * into src from county_clerk_sources limit 1;
 insert into bpp_intelligence_items(id,district_id,jurisdiction_id,signal_type,source,source_record_id,status,classification,account_card_draft,account_card_version,reviewed_by)
 values(item,src.district_id,src.jurisdiction_id,'new_business','test','print-rollback','NEW','NO_ACCOUNT_FOUND','{}',1,actor);
 cards:=jsonb_build_array(jsonb_build_object('id',item,'version',1));
 begin perform bpp_confirm_account_card_prints(src.district_id,actor,cards);raise exception 'Incomplete accepted';exception when raise_exception then if sqlerrm!='PRINT_CARD_CONFLICT' then raise;end if;end;
 update bpp_intelligence_items set status='RESOLVED' where id=item;
 begin perform bpp_confirm_account_card_prints(gen_random_uuid(),actor,cards);raise exception 'Foreign district accepted';exception when raise_exception then if sqlerrm!='PRINT_CARD_CONFLICT' then raise;end if;end;
 begin perform bpp_confirm_account_card_prints(src.district_id,gen_random_uuid(),cards);raise exception 'Foreign actor accepted';exception when raise_exception then if sqlerrm!='PRINT_CARD_CONFLICT' then raise;end if;end;
 begin perform bpp_confirm_account_card_prints(src.district_id,actor,jsonb_build_array(jsonb_build_object('id',item,'version',2)));raise exception 'Stale version accepted';exception when raise_exception then if sqlerrm!='PRINT_CARD_CONFLICT' then raise;end if;end;
 -- Whole batch rolls back when any card fails authorization.
 begin perform bpp_confirm_account_card_prints(src.district_id,actor,cards||jsonb_build_array(jsonb_build_object('id',gen_random_uuid(),'version',1)));raise exception 'Invalid batch accepted';exception when raise_exception then if sqlerrm!='PRINT_CARD_CONFLICT' then raise;end if;end;
 if exists(select 1 from bpp_account_card_prints where item_id=item) then raise exception 'Partial batch written';end if;
 result:=bpp_confirm_account_card_prints(src.district_id,actor,cards);
 stamp:=(result->0->>'printed_at')::timestamptz;
 perform bpp_confirm_account_card_prints(src.district_id,actor,cards);
 if (select count(*) from bpp_account_card_prints where item_id=item)!=1 or (select printed_at from bpp_account_card_prints where item_id=item)!=stamp then raise exception 'Duplicate confirmation changed original';end if;
 if has_table_privilege('authenticated','bpp_account_card_prints','select') or has_function_privilege('anon','bpp_confirm_account_card_prints(uuid,uuid,jsonb)','execute') then raise exception 'Public access';end if;
 raise notice 'Print checks passed: completed only, district/actor/version isolation, atomic batches, idempotency, restricted access';
end $$;
rollback;
