begin;
create table if not exists public.county_clerk_sources (
 id uuid primary key default gen_random_uuid(), jurisdiction_id uuid not null unique references public.jurisdictions(id),
 district_id uuid not null references public.districts(id), state text not null default 'TX', county text not null,
 provider text not null default 'eagleweb', source_url text not null, manual_import_enabled boolean not null default true,
 automation_authorized boolean not null default false, automation_enabled boolean not null default false check (not automation_enabled),
 retrieval_method text not null default 'manual_pdf', overlap_days integer not null default 7 check(overlap_days between 1 and 90),
 last_successful_import_at timestamptz, last_successful_retrieval_at timestamptz, last_complete_recording_date date,
 created_at timestamptz not null default now(), unique(state,county), check(state=upper(trim(state)) and county=upper(trim(county)))
);
create table if not exists public.county_clerk_dba_imports (
 id uuid primary key default gen_random_uuid(), source_id uuid not null references public.county_clerk_sources(id),
 jurisdiction_id uuid not null references public.jurisdictions(id), district_id uuid not null references public.districts(id),
 filename text not null, sha256 text not null, storage_path text not null, parser_version text not null,
 status text not null default 'PROCESSING' check(status in ('PROCESSING','COMPLETED','PARTIAL','FAILED')),
 coverage text not null check(coverage in ('COMPLETE','INCOMPLETE','UNKNOWN')), page_count integer not null,
 reported_total integer, extracted_count integer not null, imported_count integer not null default 0, duplicate_count integer not null default 0,
 search_start date, search_end date, metadata jsonb not null default '{}', error_message text,
 created_by uuid references auth.users(id) on delete set null, created_at timestamptz not null default now(), completed_at timestamptz
);
create index if not exists county_clerk_import_history_idx on public.county_clerk_dba_imports(district_id,created_at desc);
create table if not exists public.county_clerk_dba_filings (
 id uuid primary key default gen_random_uuid(), source_id uuid not null references public.county_clerk_sources(id),
 jurisdiction_id uuid not null references public.jurisdictions(id), district_id uuid not null references public.districts(id),
 state text not null, county text not null, document_number text not null,
 filing_type text not null check(filing_type in ('ASSUMED NAME','ASSUMED NAME ABANDONMENT')),
 recording_date date not null, assumed_name text not null, owner_name text, owner_text text, owner_name_complete boolean not null default false,
 expiration_date date, document_reference text, source_pages jsonb not null, raw_text text not null, original_fields jsonb not null,
 first_import_id uuid not null references public.county_clerk_dba_imports(id), created_at timestamptz not null default now(),
 unique(state,county,document_number,filing_type)
);
create index if not exists county_clerk_filing_date_idx on public.county_clerk_dba_filings(district_id,recording_date);
create table if not exists public.county_clerk_dba_observations (
 import_id uuid not null references public.county_clerk_dba_imports(id), filing_id uuid not null references public.county_clerk_dba_filings(id),
 original_fields jsonb not null, primary key(import_id,filing_id)
);
alter table public.bpp_intelligence_items add column if not exists source_dba_filing_id uuid references public.county_clerk_dba_filings(id);
alter table public.bpp_intelligence_items add column if not exists filing_date date;
alter table public.bpp_intelligence_items add column if not exists filing_type text;
alter table public.bpp_intelligence_items drop constraint if exists bpp_intelligence_items_signal_type_check;
alter table public.bpp_intelligence_items add constraint bpp_intelligence_items_signal_type_check check(signal_type in ('sales_tax_inactive','new_business','mailing_address_change','county_clerk_dba'));
alter table public.bpp_intelligence_items drop constraint if exists bpp_intelligence_items_classification_check;
alter table public.bpp_intelligence_items add constraint bpp_intelligence_items_classification_check check(classification in (
 'EXISTING_ACCOUNT_HIGH_CONFIDENCE','POSSIBLE_EXISTING_ACCOUNT','NO_ACCOUNT_FOUND','AMBIGUOUS',
 'LIKELY_MAILING_ADDRESS_CHANGE','POSSIBLE_MAILING_ADDRESS_CHANGE','CONFIRMED_MAILING_ADDRESS_CHANGE',
 'DBA_POTENTIAL_NEW_BUSINESS','DBA_EXISTING_BUSINESS','DBA_POSSIBLE_BUSINESS_CHANGE','DBA_POTENTIAL_CLOSURE_LEAD','DBA_NEEDS_REVIEW'));
-- Extend the existing match-decision history rather than creating a second account-link system.
alter table public.cad_match_decisions alter column permit_location_id drop not null;
alter table public.cad_match_decisions add column if not exists dba_filing_id uuid references public.county_clerk_dba_filings(id);
alter table public.cad_match_decisions drop constraint if exists cad_match_decision_source_check;
alter table public.cad_match_decisions add constraint cad_match_decision_source_check check(num_nonnulls(permit_location_id,dba_filing_id)=1);
create index if not exists cad_match_decisions_dba_idx on public.cad_match_decisions(jurisdiction_id,dba_filing_id,reviewed_at desc,id desc);
create table if not exists public.county_clerk_dba_review_events (
 id uuid primary key default gen_random_uuid(), filing_id uuid not null references public.county_clerk_dba_filings(id),
 intelligence_item_id uuid not null references public.bpp_intelligence_items(id), district_id uuid not null,
 actor_id uuid references auth.users(id) on delete set null, before_state jsonb not null, after_state jsonb not null, created_at timestamptz not null default now()
);
create or replace function public.dba_review_audit() returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
 if new.signal_type='county_clerk_dba' and new.source_dba_filing_id is not null and
    (to_jsonb(old)-'updated_at') is distinct from (to_jsonb(new)-'updated_at') then
  insert into public.county_clerk_dba_review_events(filing_id,intelligence_item_id,district_id,actor_id,before_state,after_state)
  values(new.source_dba_filing_id,new.id,new.district_id,coalesce(new.reviewed_by,new.assigned_to),to_jsonb(old),to_jsonb(new));
 end if;return new;
end $$;
drop trigger if exists county_clerk_review_audit on public.bpp_intelligence_items;
create trigger county_clerk_review_audit after update on public.bpp_intelligence_items for each row execute function public.dba_review_audit();
-- Indexed exact normalized names; no county-wide CRS download and no guessed address.
create or replace function public.dba_name_key(value text) returns text language sql immutable parallel safe as $$
 select regexp_replace(lower(coalesce(value,'')),'[^a-z0-9]','','g')
$$;
create index if not exists dba_crs_business_name_idx on public.real_property_records(jurisdiction_id,public.dba_name_key(business_name));
create index if not exists dba_crs_owner_name_idx on public.real_property_records(jurisdiction_id,public.dba_name_key(owner_name));
create index if not exists dba_crs_dba_name_idx on public.real_property_records(jurisdiction_id,public.dba_name_key(dba_name));
create index if not exists dba_permit_name_idx on public.comptroller_permit_locations(district_id,public.dba_name_key(location_name));
create index if not exists dba_permit_legal_idx on public.comptroller_permit_locations(district_id,public.dba_name_key(legal_name));
create index if not exists dba_prior_name_idx on public.county_clerk_dba_filings(district_id,public.dba_name_key(assumed_name));
create or replace function public.dba_match_context(p_district_id uuid,p_jurisdiction_id uuid,p_names text[]) returns jsonb
language plpgsql security definer set search_path=public,pg_temp as $$
declare keys text[];result jsonb;
begin
 if not exists(select 1 from public.jurisdictions where id=p_jurisdiction_id and district_id=p_district_id) then raise exception 'District mismatch';end if;
 if cardinality(p_names)>6000 then raise exception 'Too many filing names';end if;
 select array_agg(distinct public.dba_name_key(n)) into keys from unnest(p_names) n where length(public.dba_name_key(n))>=3;
 select jsonb_build_object(
 'accounts',coalesce((select jsonb_agg(to_jsonb(a)) from (select id,real_account_number,owner_name,business_name,dba_name,situs_address_raw,situs_zip,tax_year,account_status,source_import_id from public.real_property_records where jurisdiction_id=p_jurisdiction_id and (public.dba_name_key(owner_name)=any(keys) or public.dba_name_key(business_name)=any(keys) or public.dba_name_key(dba_name)=any(keys)) order by tax_year desc nulls last,id limit 1001) a),'[]'::jsonb),
 'sales_tax',coalesce((select jsonb_agg(to_jsonb(a)) from (select id,taxpayer_id,location_number,legal_name,location_name,address,city,state,zip,current_status from public.comptroller_permit_locations where district_id=p_district_id and (public.dba_name_key(legal_name)=any(keys) or public.dba_name_key(location_name)=any(keys)) order by id limit 1001) a),'[]'::jsonb),
 'prior_filings',coalesce((select jsonb_agg(to_jsonb(a)) from (select id,document_number,filing_type,assumed_name,owner_name,recording_date from public.county_clerk_dba_filings where district_id=p_district_id and public.dba_name_key(assumed_name)=any(keys) order by recording_date desc,id limit 1001) a),'[]'::jsonb)
 ) into result;return result;
end $$;
create or replace function public.dba_commit_import(p_import_id uuid,p_filings jsonb) returns jsonb
language plpgsql security definer set search_path=public,pg_temp as $$
declare imp public.county_clerk_dba_imports%rowtype;src public.county_clerk_sources%rowtype;rec jsonb;lead jsonb;fid uuid;added integer:=0;dupes integer:=0;was_new boolean;max_date date;
begin
 select * into imp from public.county_clerk_dba_imports where id=p_import_id for update;
 if not found then raise exception 'Import not found';end if;
 if imp.status in ('COMPLETED','PARTIAL') then return jsonb_build_object('import_id',imp.id,'imported_count',imp.imported_count,'duplicate_count',imp.duplicate_count,'status',imp.status);end if;
 if imp.status!='PROCESSING' then raise exception 'Import is not processing';end if;
 select * into src from public.county_clerk_sources where id=imp.source_id;
 if not src.manual_import_enabled or src.district_id!=imp.district_id or src.jurisdiction_id!=imp.jurisdiction_id then raise exception 'Source/district mismatch';end if;
 if jsonb_typeof(p_filings)!='array' or jsonb_array_length(p_filings)!=imp.extracted_count or imp.extracted_count<1 or imp.extracted_count>3000 then raise exception 'Filing count mismatch';end if;
 for rec in select value from jsonb_array_elements(p_filings) loop
  fid:=null;
  insert into public.county_clerk_dba_filings(source_id,jurisdiction_id,district_id,state,county,document_number,filing_type,recording_date,assumed_name,owner_name,owner_text,owner_name_complete,expiration_date,document_reference,source_pages,raw_text,original_fields,first_import_id)
  values(src.id,imp.jurisdiction_id,imp.district_id,src.state,src.county,rec->>'document_number',rec->>'filing_type',(rec->>'recording_date')::date,rec->>'assumed_name',rec->>'owner_name',rec->>'owner_text',coalesce((rec->>'owner_name_complete')::boolean,false),(rec->>'expiration_date')::date,rec->>'document_reference',rec->'source_pages',rec->>'raw_text',rec-'intelligence',imp.id)
  on conflict(state,county,document_number,filing_type) do nothing returning id into fid;
  was_new:=fid is not null;
  if not was_new then
   select id into fid from public.county_clerk_dba_filings where state=src.state and county=src.county and document_number=rec->>'document_number' and filing_type=rec->>'filing_type' and district_id=imp.district_id;
   if fid is null then raise exception 'Filing belongs to another district';end if;
   dupes:=dupes+1;
  else added:=added+1;end if;
  insert into public.county_clerk_dba_observations(import_id,filing_id,original_fields) values(imp.id,fid,rec-'intelligence') on conflict do nothing;
  if was_new then
   lead:=rec->'intelligence';if lead is null then raise exception 'Missing intelligence evidence';end if;
   insert into public.bpp_intelligence_items(jurisdiction_id,district_id,signal_type,source,source_record_id,source_dba_filing_id,filing_date,filing_type,classification,priority,confidence,confidence_score,is_ambiguous,business_name,legal_name,matched_record_id,matched_account_number,matched_owner_name,matched_address,match_reason,match_signals,recommended_action,evidence,first_detected_at)
   values(imp.jurisdiction_id,imp.district_id,'county_clerk_dba','county_clerk_eagleweb',src.state||':'||src.county||':'||(rec->>'document_number')||':'||(rec->>'filing_type'),fid,(rec->>'recording_date')::date,rec->>'filing_type',lead->>'classification',lead->>'priority',lead->>'confidence',(lead->>'confidence_score')::numeric,coalesce((lead->>'is_ambiguous')::boolean,false),rec->>'assumed_name',rec->>'owner_name',lead->>'matched_record_id',lead->>'matched_account_number',lead->>'matched_owner_name',lead->>'matched_address',lead->>'match_reason',lead->'match_signals',lead->>'recommended_action',lead->'evidence',now())
   on conflict(signal_type,source,source_record_id) do nothing;
  end if;
  max_date:=greatest(max_date,(rec->>'recording_date')::date);
 end loop;
 update public.county_clerk_dba_imports set status=case when coverage='COMPLETE' then 'COMPLETED' else 'PARTIAL' end,imported_count=added,duplicate_count=dupes,completed_at=now() where id=imp.id;
 update public.county_clerk_sources set last_successful_import_at=now(),last_complete_recording_date=case when imp.coverage='COMPLETE' then greatest(last_complete_recording_date,max_date) else last_complete_recording_date end where id=src.id;
 return jsonb_build_object('import_id',imp.id,'imported_count',added,'duplicate_count',dupes,'status',case when imp.coverage='COMPLETE' then 'COMPLETED' else 'PARTIAL' end);
end $$;
-- Private original PDFs, accessible only through district-checked application endpoints.
insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types) values('county-clerk-dba','county-clerk-dba',false,26214400,array['application/pdf']) on conflict(id) do nothing;
insert into public.county_clerk_sources(jurisdiction_id,district_id,state,county,source_url)
select id,district_id,upper(state),upper(county_name),'https://erecord.lubbockcounty.gov/recorder/web/splash.jsp' from public.jurisdictions where slug='lubbock' and district_id is not null on conflict(jurisdiction_id) do nothing;
do $$ declare t text;begin
 foreach t in array array['county_clerk_sources','county_clerk_dba_imports','county_clerk_dba_filings','county_clerk_dba_observations','county_clerk_dba_review_events'] loop
 execute format('alter table public.%I enable row level security',t);
 execute format('revoke all on public.%I from public,anon,authenticated',t);
 execute format('grant select,insert,update on public.%I to service_role',t);
 end loop;
end $$;
revoke all on function public.dba_commit_import(uuid,jsonb) from public,anon,authenticated;
revoke all on function public.dba_match_context(uuid,uuid,text[]) from public,anon,authenticated;
grant execute on function public.dba_commit_import(uuid,jsonb) to service_role;
grant execute on function public.dba_match_context(uuid,uuid,text[]) to service_role;
notify pgrst,'reload schema';
commit;
