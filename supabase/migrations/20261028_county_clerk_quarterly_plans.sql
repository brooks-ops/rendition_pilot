begin;
-- A blocked plan is not a completed import or a successful source retrieval.
create table if not exists public.county_clerk_collection_runs (
 id uuid primary key default gen_random_uuid(),
 source_id uuid not null references public.county_clerk_sources(id),
 jurisdiction_id uuid not null references public.jurisdictions(id),
 district_id uuid not null references public.districts(id),
 scheduled_for date not null,
 window_start date not null, window_end date not null,
 status text not null default 'BLOCKED' check(status in ('BLOCKED','RUNNING','COMPLETED','PARTIAL','FAILED')),
 message text, records_retrieved integer, imported_count integer, duplicate_count integer,
 import_id uuid references public.county_clerk_dba_imports(id),
 created_at timestamptz not null default now(), completed_at timestamptz,
 unique(source_id,scheduled_for),
 check(window_start<=window_end),
 check(extract(day from scheduled_for)=1 and extract(month from scheduled_for) in (1,4,7,10)),
 check(window_end=scheduled_for-1 and window_start=date_trunc('year',window_end)::date)
);
create index if not exists county_clerk_collection_history_idx
 on public.county_clerk_collection_runs(district_id,scheduled_for desc);
alter table public.county_clerk_collection_runs enable row level security;
revoke all on public.county_clerk_collection_runs from public,anon,authenticated;
grant select,insert,update on public.county_clerk_collection_runs to service_role;

create or replace function public.dba_plan_quarterly_run(p_source_id uuid,p_scheduled_for date)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare src public.county_clerk_sources%rowtype; run public.county_clerk_collection_runs%rowtype;
begin
 select * into src from public.county_clerk_sources where id=p_source_id;
 if not found then raise exception 'County source not found';end if;
 if not exists(select 1 from public.jurisdictions where id=src.jurisdiction_id and district_id=src.district_id)
 then raise exception 'Source/district mismatch';end if;
 if p_scheduled_for is null or extract(day from p_scheduled_for)!=1 or extract(month from p_scheduled_for) not in (1,4,7,10)
 then raise exception 'Invalid quarterly run date';end if;
 if p_scheduled_for>(now() at time zone 'America/Chicago')::date then raise exception 'Future run is not due';end if;
 insert into public.county_clerk_collection_runs(source_id,jurisdiction_id,district_id,scheduled_for,window_start,window_end,status,message)
 values(src.id,src.jurisdiction_id,src.district_id,p_scheduled_for,date_trunc('year',p_scheduled_for-1)::date,p_scheduled_for-1,'BLOCKED',
 'Quarterly year-to-date pull is planned. Source access is unconfirmed; no unattended collection has been attempted.')
 on conflict(source_id,scheduled_for) do nothing;
 select * into run from public.county_clerk_collection_runs where source_id=src.id and scheduled_for=p_scheduled_for;
 return to_jsonb(run);
end $$;
revoke all on function public.dba_plan_quarterly_run(uuid,date) from public,anon,authenticated;
grant execute on function public.dba_plan_quarterly_run(uuid,date) to service_role;
notify pgrst,'reload schema';
commit;
