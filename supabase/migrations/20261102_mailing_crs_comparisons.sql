-- Apply before deploying the matching code. No historical rows are deleted.
begin;
alter table public.real_property_records
 add column if not exists mailing_recipient text,
 add column if not exists mailing_address_line1 text,
 add column if not exists mailing_address_line2 text,
 add column if not exists mailing_unit text,
 add column if not exists mailing_city text,
 add column if not exists mailing_state text,
 add column if not exists mailing_zip text,
 add column if not exists mailing_source text,
 add column if not exists mailing_import_id uuid,
 add column if not exists mailing_imported_at timestamptz,
 add column if not exists mailing_source_as_of_date date,
 add column if not exists mailing_field_sources jsonb not null default '{}'::jsonb;
-- Sparse imports retain provenance for fields omitted from the new file.
create or replace function public.mailing_merge_field_sources() returns trigger
 language plpgsql set search_path=public,pg_temp as $$
begin
 new.mailing_field_sources:=coalesce(old.mailing_field_sources,'{}'::jsonb)||coalesce(new.mailing_field_sources,'{}'::jsonb);
 return new;
end $$;
drop trigger if exists mailing_field_sources_merge on public.real_property_records;
create trigger mailing_field_sources_merge before update on public.real_property_records
 for each row execute function public.mailing_merge_field_sources();

-- Unique destinations are not a chronology: preserve existing observations,
-- then append only when the latest value differs, including A -> B -> A.
drop index if exists public.mailing_address_observations_dedup_idx;
create or replace function public.mailing_record_observation(p_observation jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare prev public.mailing_address_observations; inserted public.mailing_address_observations;
begin
 perform mailing_assert_run((p_observation->>'scan_run_id')::uuid,(p_observation->>'jurisdiction_id')::uuid);
 perform pg_advisory_xact_lock(hashtextextended(concat_ws(':',p_observation->>'jurisdiction_id',
   p_observation->>'account_identifier_type',p_observation->>'account_identifier',p_observation->>'source'),0));
 select * into prev from mailing_address_observations where jurisdiction_id=(p_observation->>'jurisdiction_id')::uuid
   and account_identifier_type=p_observation->>'account_identifier_type' and account_identifier=p_observation->>'account_identifier'
   and source=p_observation->>'source' order by observed_at desc,id desc limit 1;
 if prev.id is not null and prev.normalized_full_address=p_observation->>'normalized_full_address' then
   return jsonb_build_object('added',false,'observation',to_jsonb(prev));
 end if;
 insert into mailing_address_observations(jurisdiction_id,account_identifier_type,account_identifier,source,source_record_id,
   source_effective_date,raw_address_line,address_type,po_box_number,unit,city,state,zip,zip4,normalized_full_address,observed_at)
 values((p_observation->>'jurisdiction_id')::uuid,p_observation->>'account_identifier_type',p_observation->>'account_identifier',
   p_observation->>'source',p_observation->>'source_record_id',(p_observation->>'source_effective_date')::date,
   p_observation->>'raw_address_line',p_observation->>'address_type',p_observation->>'po_box_number',p_observation->>'unit',
   p_observation->>'city',p_observation->>'state',p_observation->>'zip',p_observation->>'zip4',p_observation->>'normalized_full_address',
   greatest(clock_timestamp(),prev.observed_at+interval '1 microsecond'))
 returning * into inserted;
 return jsonb_build_object('added',true,'observation',to_jsonb(inserted));
end $$;

-- Scope all queue sources to county; legacy source_record_id values stay intact.
drop index if exists public.bpp_intelligence_items_dedup_idx;
create unique index bpp_intelligence_items_dedup_idx on public.bpp_intelligence_items
 (jurisdiction_id,signal_type,source,source_record_id);
alter table public.bpp_intelligence_items add column if not exists mailing_comparison_key text;
create unique index if not exists mailing_one_open_comparison_idx on public.bpp_intelligence_items
 (jurisdiction_id,mailing_comparison_key) where mailing_comparison_key is not null and status in ('NEW','IN_REVIEW');

create table if not exists public.mailing_comparison_states (
 jurisdiction_id uuid not null references public.jurisdictions(id), comparison_key text not null,
 fingerprint text not null, last_item_id uuid references public.bpp_intelligence_items(id),
 evaluated_at timestamptz not null default now(), primary key(jurisdiction_id,comparison_key)
);
create table if not exists public.mailing_scan_runs (
 id uuid primary key default gen_random_uuid(), jurisdiction_id uuid not null references public.jurisdictions(id),
 district_id uuid not null references public.districts(id), county text not null,
 source_scope text not null default 'active_comptroller_taxpayers_vs_imported_crs',
 status text not null default 'RUNNING' check(status in ('RUNNING','SUCCESS','FAILED')),
 started_at timestamptz not null default now(), heartbeat_at timestamptz not null default now(), finished_at timestamptz,
 records_examined integer not null default 0, observations_added integer not null default 0,
 flags_created integer not null default 0, flags_updated integer not null default 0,
 unlinked integer not null default 0, missing_crs integer not null default 0, incomplete_source integer not null default 0,
 errors jsonb not null default '[]'::jsonb
);
create unique index if not exists mailing_one_running_scan_idx on public.mailing_scan_runs(jurisdiction_id) where status='RUNNING';
create index if not exists mailing_scan_latest_idx on public.mailing_scan_runs(district_id,started_at desc);

-- Fence expired workers: a reclaimed run cannot later overwrite newer evidence.
create or replace function public.mailing_assert_run(p_run_id uuid,p_jurisdiction_id uuid)
 returns void language plpgsql security definer set search_path=public,pg_temp as $$
begin
 if p_run_id is null then return;end if;
 perform 1 from mailing_scan_runs where id=p_run_id and jurisdiction_id=p_jurisdiction_id and status='RUNNING' for share;
 if not found then raise exception 'Mailing scan lease is no longer active';end if;
end $$;

create or replace function public.mailing_start_scan(p_jurisdiction_id uuid,p_district_id uuid,p_county text)
returns uuid language plpgsql security definer set search_path=public,pg_temp as $$
declare result uuid;
begin
 perform pg_advisory_xact_lock(hashtextextended('mailing-run:'||p_jurisdiction_id::text,0));
 if not exists(select 1 from jurisdictions where id=p_jurisdiction_id and district_id=p_district_id and county_name=p_county) then
   raise exception 'Invalid mailing scan jurisdiction';
 end if;
 update mailing_scan_runs set status='FAILED',finished_at=now(),errors='["Previous scan exceeded the two-hour lease; inspect worker logs."]'
 where jurisdiction_id=p_jurisdiction_id and status='RUNNING' and heartbeat_at<now()-interval '2 hours';
 if exists(select 1 from mailing_scan_runs where jurisdiction_id=p_jurisdiction_id and status='RUNNING') then
   raise exception 'A mailing scan is already running';
 end if;
 insert into mailing_scan_runs(jurisdiction_id,district_id,county) values(p_jurisdiction_id,p_district_id,p_county) returning id into result;
 return result;
end $$;

-- Atomic evidence refresh: NEVER replace a review's status, assignment,
-- notes, reviewer, or timestamps. A changed comparison after a completed
-- review creates a new event; unchanged resolved comparisons stay resolved.
create or replace function public.mailing_save_comparison(p_jurisdiction_id uuid,p_key text,p_fingerprint text,
 p_actionable boolean,p_payload jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare prior public.mailing_comparison_states; item public.bpp_intelligence_items;
 result_id uuid; action text:='NONE'; changed boolean;
begin
 perform mailing_assert_run((p_payload->'evidence'->>'scan_run_id')::uuid,p_jurisdiction_id);
 perform pg_advisory_xact_lock(hashtextextended('mailing-comparison:'||p_jurisdiction_id::text||':'||p_key,0));
 if not exists(select 1 from jurisdictions where id=p_jurisdiction_id and district_id=(p_payload->>'district_id')::uuid) then
   raise exception 'Invalid mailing comparison district';
 end if;
 select * into prior from mailing_comparison_states where jurisdiction_id=p_jurisdiction_id and comparison_key=p_key for update;
 changed := prior.fingerprint is distinct from p_fingerprint;
 select * into item from bpp_intelligence_items where jurisdiction_id=p_jurisdiction_id and mailing_comparison_key=p_key
   and status in ('NEW','IN_REVIEW') for update;
 if item.id is null and prior.last_item_id is null then
   -- Adopt one open legacy review instead of creating duplicate work.
   -- Keep original source keys, reservations, assignment and review history.
   select * into item from bpp_intelligence_items where jurisdiction_id=p_jurisdiction_id
     and signal_type='mailing_address_change' and mailing_comparison_key is null
     and source_taxpayer_id=p_payload->>'source_taxpayer_id'
     and matched_account_number=p_payload->>'matched_account_number'
     and status in ('NEW','IN_REVIEW') order by created_at desc,id desc limit 1 for update;
 end if;
 if item.id is null and not changed and prior.last_item_id is not null then
   update mailing_comparison_states set evaluated_at=clock_timestamp() where jurisdiction_id=p_jurisdiction_id and comparison_key=p_key;
   return jsonb_build_object('action','SUPPRESSED','item_id',prior.last_item_id);
 end if;
 if item.id is not null then
   update bpp_intelligence_items set mailing_address_current=p_payload->>'mailing_address_current',
     mailing_address_observed=p_payload->>'mailing_address_observed',mailing_comparison_key=p_key,
     evidence=(p_payload->'evidence') || case when item.mailing_comparison_key is null then
       jsonb_build_object('legacy_snapshot',jsonb_build_object('source',item.source,'source_record_id',item.source_record_id,
         'previous_mailing',item.mailing_address_current,'observed_mailing',item.mailing_address_observed,'evidence',item.evidence))
       when item.evidence ? 'legacy_snapshot' then jsonb_build_object('legacy_snapshot',item.evidence->'legacy_snapshot')
       else '{}'::jsonb end,
     match_reason=p_payload->>'match_reason',match_signals=p_payload->'match_signals',
     confidence=p_payload->>'confidence',priority=p_payload->>'priority',classification=p_payload->>'classification',
     recommended_action=p_payload->>'recommended_action',business_name=p_payload->>'business_name',legal_name=p_payload->>'legal_name'
   where id=item.id;
   result_id:=item.id; action:='UPDATED';
 elsif p_actionable then
   insert into bpp_intelligence_items(jurisdiction_id,district_id,signal_type,source,source_record_id,source_taxpayer_id,
    mailing_comparison_key,status,classification,priority,confidence,business_name,legal_name,first_detected_at,
    matched_record_id,matched_account_number,matched_owner_name,mailing_address_current,mailing_address_observed,
    match_reason,match_signals,recommended_action,evidence)
   values(p_jurisdiction_id,(p_payload->>'district_id')::uuid,'mailing_address_change','crs_comptroller_mailing_comparison',
    p_key||':'||gen_random_uuid()::text,p_payload->>'source_taxpayer_id',p_key,'NEW',p_payload->>'classification',
    p_payload->>'priority',p_payload->>'confidence',p_payload->>'business_name',p_payload->>'legal_name',now(),
    p_payload->>'matched_record_id',p_payload->>'matched_account_number',p_payload->>'matched_owner_name',
    p_payload->>'mailing_address_current',p_payload->>'mailing_address_observed',p_payload->>'match_reason',
    p_payload->'match_signals',p_payload->>'recommended_action',p_payload->'evidence') returning id into result_id;
   action:='CREATED';
 end if;
 insert into mailing_comparison_states(jurisdiction_id,comparison_key,fingerprint,last_item_id)
 values(p_jurisdiction_id,p_key,p_fingerprint,result_id)
 on conflict(jurisdiction_id,comparison_key) do update set fingerprint=excluded.fingerprint,
   last_item_id=excluded.last_item_id,evaluated_at=now();
 return jsonb_build_object('action',action,'item_id',result_id);
end $$;

-- A later matching failure must not leave an old HIGH-confidence badge.
-- Retain the old account reservation and reviewed facts for staff to resolve.
create or replace function public.mailing_mark_unreliable(p_jurisdiction_id uuid,p_taxpayer_id text,
 p_current_account text,p_diagnostic jsonb) returns integer
 language plpgsql security definer set search_path=public,pg_temp as $$
declare affected integer;
begin
 perform mailing_assert_run((p_diagnostic->>'scan_run_id')::uuid,p_jurisdiction_id);
 update bpp_intelligence_items set confidence='LOW',priority='MEDIUM',
  match_reason='Account match is no longer reliable; review the retained account before acting.',
  recommended_action='No supported correction. Recheck the account identity first.',
  evidence=coalesce(evidence,'{}'::jsonb)||jsonb_build_object('comparison_status','ACCOUNT_MATCH_NEEDS_REVIEW',
    'identity_confidence','NONE','account_match',p_diagnostic,'prior_account_match',
     coalesce(evidence->'prior_account_match',evidence->'account_match'),
    'evaluated_at',clock_timestamp(),'correction_supported',false)
 where jurisdiction_id=p_jurisdiction_id and source_taxpayer_id=p_taxpayer_id
  and mailing_comparison_key is not null and status in ('NEW','IN_REVIEW')
  and (p_current_account is null or matched_account_number is distinct from p_current_account);
 get diagnostics affected=row_count;
 return affected;
end $$;

alter table public.mailing_comparison_states enable row level security;
alter table public.mailing_scan_runs enable row level security;
revoke all on public.mailing_comparison_states,public.mailing_scan_runs from public,anon,authenticated;
grant select,insert,update on public.mailing_comparison_states,public.mailing_scan_runs to service_role;
revoke all on function public.mailing_assert_run(uuid,uuid) from public,anon,authenticated;
revoke all on function public.mailing_mark_unreliable(uuid,text,text,jsonb) from public,anon,authenticated;
revoke all on function public.mailing_record_observation(jsonb) from public,anon,authenticated;
revoke all on function public.mailing_start_scan(uuid,uuid,text) from public,anon,authenticated;
revoke all on function public.mailing_save_comparison(uuid,text,text,boolean,jsonb) from public,anon,authenticated;
grant execute on function public.mailing_record_observation(jsonb),public.mailing_start_scan(uuid,uuid,text),
 public.mailing_save_comparison(uuid,text,text,boolean,jsonb),public.mailing_mark_unreliable(uuid,text,text,jsonb),
 public.mailing_assert_run(uuid,uuid) to service_role;
commit;
