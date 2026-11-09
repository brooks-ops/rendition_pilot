begin;
insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values('returned-mail','returned-mail',false,12582912,array['application/pdf','image/jpeg','image/png'])
on conflict(id) do update set public=false,file_size_limit=excluded.file_size_limit,allowed_mime_types=excluded.allowed_mime_types;

create table public.returned_mail_documents(
 id uuid primary key default gen_random_uuid(),district_id uuid not null references public.districts(id),
 jurisdiction_id uuid not null references public.jurisdictions(id),account_number text not null,
 intelligence_item_id uuid not null references public.bpp_intelligence_items(id),filename text not null,relative_path text not null,
 sha256 text not null,storage_path text not null,byte_count integer not null check(byte_count between 1 and 12582912),
 content_type text not null check(content_type in ('application/pdf','image/jpeg','image/png')),page_count integer not null check(page_count between 1 and 10),
 uploaded_at timestamptz not null default now(),uploaded_by uuid not null,
 ocr_text text not null default '',ocr_status text not null,ocr_warnings jsonb not null default '[]',
 suggested_reason text not null default 'UNKNOWN',suggested_forwarding text,printed_accounts jsonb not null default '[]',account_mismatch boolean not null default false,
 review_status text not null default 'PENDING' check(review_status in ('PENDING','REVIEWED','DISMISSED')),
 return_date date,return_reason text,forwarding_address text,review_notes text,
 reviewed_at timestamptz,reviewed_by uuid,version integer not null default 0,
 unique(district_id,account_number,sha256)
);
create index returned_mail_account_idx on public.returned_mail_documents(district_id,account_number,uploaded_at desc);
create table public.returned_mail_review_events(
 id uuid primary key default gen_random_uuid(),document_id uuid not null references public.returned_mail_documents(id),
 district_id uuid not null,actor_id uuid not null,occurred_at timestamptz not null default now(),before_state jsonb not null,after_state jsonb not null
);
-- Atomic file-to-queue linkage, protected against retries/concurrent uploads.
create or replace function public.returned_mail_commit(p_district_id uuid,p_jurisdiction_id uuid,p_actor_id uuid,p_document jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare doc returned_mail_documents;item uuid;acct text:=p_document->>'account_number';mismatch boolean:=coalesce((p_document->>'account_mismatch')::boolean,false);
begin
 if not exists(select 1 from jurisdictions where id=p_jurisdiction_id and district_id=p_district_id) then raise exception 'Invalid returned-mail district';end if;
 if acct !~ '^[A-Z]?[0-9]{1,20}$' or p_document->>'storage_path' not like p_district_id::text||'/'||acct||'/%' then raise exception 'Invalid account/storage scope';end if;
 perform pg_advisory_xact_lock(hashtextextended('returned-mail:'||p_district_id||':'||acct,0));
 select * into doc from returned_mail_documents where district_id=p_district_id and account_number=acct and sha256=p_document->>'sha256';
 if doc.id is not null then return jsonb_build_object('duplicate',true,'document_id',doc.id,'item_id',doc.intelligence_item_id,'account',acct);end if;
 select id into item from bpp_intelligence_items where district_id=p_district_id and source='returned_mail_scan'
  and matched_account_number=acct and status in ('NEW','IN_REVIEW') order by created_at desc limit 1 for update;
 if item is null then
  insert into bpp_intelligence_items(jurisdiction_id,district_id,signal_type,source,source_record_id,status,classification,priority,
   confidence,is_ambiguous,business_name,matched_record_id,matched_account_number,matched_owner_name,mailing_address_current,first_detected_at,
   match_reason,recommended_action,evidence)
  values(p_jurisdiction_id,p_district_id,'mailing_address_change','returned_mail_scan',gen_random_uuid()::text,'NEW','POSSIBLE_MAILING_ADDRESS_CHANGE',
   case when mismatch then 'MEDIUM' else 'HIGH' end,case when mismatch then 'MEDIUM' else 'HIGH' end,mismatch,
   coalesce(p_document->>'business_name',p_document->>'owner_name',acct),p_document->>'matched_record_id',acct,p_document->>'owner_name',p_document->>'crs_address',now(),
   'Folder account matches an imported account; verify the envelope recipient and any OCR findings.',
   'Review the returned envelope and research the mailing address. No address or CRS account was updated.',
   jsonb_build_object('returned_mail',true,'folder_account',acct,'crs_recipient',p_document->>'crs_recipient','crs_imported_at',p_document->>'crs_imported_at','crs_as_of_date',p_document->>'crs_as_of_date','postal_validation','NOT_PERFORMED')) returning id into item;
 end if;
 insert into returned_mail_documents(district_id,jurisdiction_id,account_number,intelligence_item_id,filename,relative_path,sha256,storage_path,
  byte_count,content_type,page_count,uploaded_by,ocr_text,ocr_status,ocr_warnings,suggested_reason,suggested_forwarding,printed_accounts,account_mismatch)
 values(p_district_id,p_jurisdiction_id,acct,item,p_document->>'filename',p_document->>'relative_path',p_document->>'sha256',p_document->>'storage_path',
  (p_document->>'byte_count')::integer,p_document->>'content_type',(p_document->>'page_count')::integer,p_actor_id,
  coalesce(p_document->>'ocr_text',''),p_document->>'ocr_status',p_document->'ocr_warnings',p_document->>'suggested_reason',p_document->>'suggested_forwarding',
  p_document->'printed_accounts',mismatch) returning * into doc;
 -- Adding evidence never resets status, assignment, notes, or staff decisions.
 if mismatch then update bpp_intelligence_items set is_ambiguous=true,confidence='MEDIUM',priority='MEDIUM' where id=item;end if;
 return jsonb_build_object('duplicate',false,'document_id',doc.id,'item_id',item,'account',acct,'ocr_status',doc.ocr_status);
end $$;

create or replace function public.returned_mail_review(p_district_id uuid,p_actor_id uuid,p_document_id uuid,p_version integer,p_review jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare old_doc returned_mail_documents;new_doc returned_mail_documents;
begin
 select * into old_doc from returned_mail_documents where id=p_document_id and district_id=p_district_id;
 if old_doc.id is null then raise exception 'Returned-mail scan not found';end if;
 perform pg_advisory_xact_lock(hashtextextended('returned-mail:'||p_district_id||':'||old_doc.account_number,0));
 select * into old_doc from returned_mail_documents where id=p_document_id and district_id=p_district_id for update;
 if old_doc.id is null then raise exception 'Returned-mail scan not found';end if;
 if old_doc.version!=p_version then raise exception 'This scan changed. Reload before saving.';end if;
 -- Same reservation as queue completion, including other items for this account.
 if not exists(select 1 from bpp_work_claims where district_id=p_district_id and work_key='account:'||old_doc.account_number
  and actor_id=p_actor_id) then raise exception 'Reserve this account before reviewing its returned mail';end if;
 if not exists(select 1 from bpp_intelligence_items where id=old_doc.intelligence_item_id and status in ('NEW','IN_REVIEW')) then
  raise exception 'Reopen the returned-mail review before changing this scan';end if;
 if coalesce(p_review->>'review_status','') not in ('REVIEWED','DISMISSED') then raise exception 'Choose a review status';end if;
 if coalesce(p_review->>'return_reason','') not in ('UNKNOWN','UNABLE_TO_FORWARD','NOT_DELIVERABLE','VACANT','REFUSED','DECEASED','INSUFFICIENT_ADDRESS',
  'NO_SUCH_NUMBER','ATTEMPTED_NOT_KNOWN','UNCLAIMED','RETURN_TO_SENDER','WRONG_ACCOUNT') then raise exception 'Invalid return reason';end if;
 if (p_review->>'return_date')::date>(now() at time zone 'America/Chicago')::date then raise exception 'Return date cannot be in the future';end if;
 update returned_mail_documents set review_status=p_review->>'review_status',return_date=(p_review->>'return_date')::date,
  return_reason=p_review->>'return_reason',forwarding_address=nullif(p_review->>'forwarding_address',''),review_notes=p_review->>'review_notes',
  reviewed_at=now(),reviewed_by=p_actor_id,version=version+1 where id=old_doc.id returning * into new_doc;
 insert into returned_mail_review_events(document_id,district_id,actor_id,before_state,after_state)
 values(old_doc.id,p_district_id,p_actor_id,to_jsonb(old_doc)-'ocr_text',to_jsonb(new_doc)-'ocr_text');
 return to_jsonb(new_doc)-'storage_path'-'sha256'-'district_id';
end $$;
-- Serialize completion against new scan imports: a late arrival creates
-- new work or blocks completion, never an unreviewed scan hidden in history.
create or replace function public.returned_mail_resolve(p_district_id uuid,p_actor_id uuid,p_item_id uuid,p_resolution text,p_notes text)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare item bpp_intelligence_items;
begin
 select * into item from bpp_intelligence_items where id=p_item_id and district_id=p_district_id and source='returned_mail_scan';
 if item.id is null then raise exception 'Returned-mail item not found';end if;
 perform pg_advisory_xact_lock(hashtextextended('returned-mail:'||p_district_id||':'||item.matched_account_number,0));
 select * into item from bpp_intelligence_items where id=p_item_id for update;
 if item.status not in ('NEW','IN_REVIEW') then raise exception 'Review is already complete';end if;
 if not exists(select 1 from bpp_work_claims where district_id=p_district_id and work_key='account:'||item.matched_account_number and actor_id=p_actor_id)
 then raise exception 'Reserve this account before completing the review';end if;
 if exists(select 1 from returned_mail_documents where intelligence_item_id=item.id and review_status='PENDING')
 then raise exception 'Review each returned-mail scan before completing this account';end if;
 if coalesce(p_resolution,'') not in ('ADDRESS_UPDATED','CURRENT_ADDRESS_CORRECT','SOURCE_OUTDATED','DUPLICATE_SIGNAL','ACCOUNT_MISMATCH','INSUFFICIENT_EVIDENCE','OTHER')
 then raise exception 'Invalid mailing review outcome';end if;
 update bpp_intelligence_items set status='RESOLVED',resolution=p_resolution,resolution_notes=p_notes,reviewed_by=p_actor_id,reviewed_at=now()
 where id=item.id returning * into item;
 return to_jsonb(item);
end $$;

alter table public.returned_mail_documents enable row level security;
alter table public.returned_mail_review_events enable row level security;
revoke all on public.returned_mail_documents,public.returned_mail_review_events from public,anon,authenticated;
grant select,insert,update on public.returned_mail_documents to service_role;
grant select,insert on public.returned_mail_review_events to service_role;
revoke update,delete,truncate on public.returned_mail_review_events from service_role;
revoke all on function public.returned_mail_resolve(uuid,uuid,uuid,text,text) from public,anon,authenticated;
grant execute on function public.returned_mail_resolve(uuid,uuid,uuid,text,text) to service_role;
revoke all on function public.returned_mail_commit(uuid,uuid,uuid,jsonb),public.returned_mail_review(uuid,uuid,uuid,integer,jsonb) from public,anon,authenticated;
grant execute on function public.returned_mail_commit(uuid,uuid,uuid,jsonb),public.returned_mail_review(uuid,uuid,uuid,integer,jsonb) to service_role;
commit;
