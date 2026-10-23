-- Append-only, service-only product activity. Admin reports are scoped by the API.
create table if not exists public.appraiser_activity_events (
  id uuid primary key default gen_random_uuid(),
  district_id uuid not null references public.districts(id),
  actor_id uuid not null,
  actor_email text not null,
  action text not null,
  module text not null,
  entity_type text not null,
  entity_id text not null,
  outcome text,
  units integer not null default 1 check (units > 0),
  dedup_key text unique,
  occurred_at timestamptz not null default now()
);
create index if not exists activity_district_time_idx on public.appraiser_activity_events(district_id, occurred_at);
create index if not exists activity_district_actor_idx on public.appraiser_activity_events(district_id, actor_id, occurred_at);
alter table public.appraiser_activity_events enable row level security;
revoke all on public.appraiser_activity_events from anon, authenticated;
grant select, insert on public.appraiser_activity_events to service_role;
revoke update, delete, truncate on public.appraiser_activity_events from service_role;

create table if not exists public.appraiser_activity_tracking (
  singleton boolean primary key default true check (singleton),
  started_at timestamptz not null default now()
);
insert into public.appraiser_activity_tracking(singleton) values(true) on conflict do nothing;
alter table public.appraiser_activity_tracking enable row level security;
revoke all on public.appraiser_activity_tracking from anon, authenticated;
grant select on public.appraiser_activity_tracking to service_role;

create or replace function public.appraiser_activity_report(p_district_id uuid, p_start date, p_end date)
returns jsonb language sql stable security invoker set search_path=public as $$
with scoped as (
 select *, (occurred_at at time zone 'America/Chicago')::date as work_day,
 entity_type || ':' || entity_id as work_key
 from public.appraiser_activity_events
 where district_id=p_district_id
 and occurred_at >= (p_start::timestamp at time zone 'America/Chicago')
 and occurred_at < ((p_end+1)::timestamp at time zone 'America/Chicago')
), people as (
 select actor_id, (array_agg(actor_email order by occurred_at desc))[1] as appraiser,
 count(*) as actions, count(distinct work_key) as work_items,
 count(distinct work_key) filter(where action='review_completed') as completed,
 count(distinct work_key) filter(where action='review_started') as started,
 count(distinct work_key) filter(where action='review_dismissed') as dismissed,
 count(distinct work_key) filter(where action='rendition_locked') as renditions,
 count(*) filter(where action='cad_match_decided') as cad_decisions,
 count(*) filter(where action='commercial_update_applied') as commercial_updates,
 count(*) filter(where action in ('rendition_report_generated','commercial_report_generated','account_summary_generated')) as reports,
 count(distinct work_day) as active_days
 from scoped group by actor_id
), daily as (
 select work_day, count(*) as actions,
 count(distinct work_key) filter(where action='review_completed') as completed,
 count(distinct work_key) filter(where action='rendition_locked') as renditions
 from scoped group by work_day
), outcomes as (
 select module, action, outcome, count(*) as actions, count(distinct work_key) as work_items,
 sum(units) as units from scoped group by module, action, outcome
)
select jsonb_build_object(
 'start',p_start,'end',p_end,'timezone','America/Chicago',
 'tracking_started_at',(select started_at from public.appraiser_activity_tracking where singleton),
 'totals',(select jsonb_build_object('actions',count(*),'active_appraisers',count(distinct actor_id),
   'work_items',count(distinct work_key),
   'completed',count(distinct work_key) filter(where action='review_completed'),
   'started',count(distinct work_key) filter(where action='review_started'),
   'dismissed',count(distinct work_key) filter(where action='review_dismissed'),
   'renditions',count(distinct work_key) filter(where action='rendition_locked'),
   'cad_decisions',count(*) filter(where action='cad_match_decided'),
   'commercial_updates',count(*) filter(where action='commercial_update_applied'),
   'commercial_rows_updated',coalesce(sum(units) filter(where action='commercial_update_applied'),0),
   'commercial_accounts_created',count(*) filter(where action='commercial_account_created'),
   'reports',count(*) filter(where action in ('rendition_report_generated','commercial_report_generated','account_summary_generated')))
   from scoped),
 'appraisers',coalesce((select jsonb_agg(to_jsonb(people) order by completed+renditions desc,appraiser) from people),'[]'::jsonb),
 'daily',coalesce((select jsonb_agg(to_jsonb(daily) order by work_day) from daily),'[]'::jsonb),
 'outcomes',coalesce((select jsonb_agg(to_jsonb(outcomes) order by module,action,outcome) from outcomes),'[]'::jsonb)
);
$$;
revoke all on function public.appraiser_activity_report(uuid,date,date) from public, anon, authenticated;
grant execute on function public.appraiser_activity_report(uuid,date,date) to service_role;
notify pgrst, 'reload schema';
