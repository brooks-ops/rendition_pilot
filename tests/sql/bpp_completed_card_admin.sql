begin;
do $$
declare src county_clerk_sources%rowtype; a uuid:=gen_random_uuid();b uuid:=gen_random_uuid();adm uuid:=gen_random_uuid(); one uuid:=gen_random_uuid();two uuid:=gen_random_uuid();r jsonb;
begin
 select * into src from county_clerk_sources limit 1;
 insert into district_users(district_id,user_id,email,role) values(src.district_id,adm,'admin-test@example.invalid','admin');
 insert into bpp_intelligence_items(id,district_id,jurisdiction_id,signal_type,source,source_record_id,status,classification,account_card_draft,account_card_version,reviewed_by)
 values(one,src.district_id,src.jurisdiction_id,'new_business','test','admin-card-one','RESOLVED','NO_ACCOUNT_FOUND','{}',1,a),
 (two,src.district_id,src.jurisdiction_id,'new_business','test','admin-card-two','RESOLVED','NO_ACCOUNT_FOUND','{}',1,b);
 perform bpp_confirm_account_card_prints(src.district_id,adm,jsonb_build_array(jsonb_build_object('id',one,'version',1)));
 r:=bpp_completed_account_cards(src.district_id,a,'printed',0);
 if jsonb_array_length(r->'rows')!=1 or r->'rows'->0->>'id'!=one::text then raise exception 'Printed/appraiser filter failed';end if;
 r:=bpp_completed_account_cards(src.district_id,a,'unprinted',0);
 if jsonb_array_length(r->'rows')!=0 then raise exception 'Printed card shown as unprinted';end if;
 r:=bpp_completed_account_cards(src.district_id,b,'unprinted',0);
 if jsonb_array_length(r->'rows')!=1 or r->'rows'->0->>'id'!=two::text then raise exception 'Unprinted/appraiser filter failed';end if;
 r:=bpp_completed_account_cards(gen_random_uuid(),null,'all',0);
 if jsonb_array_length(r->'rows')!=0 or jsonb_array_length(r->'appraisers')!=0 then raise exception 'District leak';end if;
 begin perform bpp_confirm_account_card_prints(src.district_id,a,jsonb_build_array(jsonb_build_object('id',two,'version',1)));raise exception 'Member printed another card';exception when raise_exception then if sqlerrm!='PRINT_CARD_CONFLICT' then raise;end if;end;
 begin perform bpp_confirm_account_card_prints(gen_random_uuid(),adm,jsonb_build_array(jsonb_build_object('id',two,'version',1)));raise exception 'Admin crossed district';exception when raise_exception then if sqlerrm!='PRINT_CARD_CONFLICT' then raise;end if;end;
 if has_function_privilege('authenticated','bpp_completed_account_cards(uuid,uuid,text,integer)','execute') then raise exception 'Public query access';end if;
 raise notice 'Admin card checks passed: appraiser/print filters, district isolation, admin team output, member own-only output';
end $$;
rollback;
