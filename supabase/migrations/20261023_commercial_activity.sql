-- Commercial reporting scope and work volume. The original RPC remains compatible.
create or replace function public.appraiser_activity_report_v2(p_district_id uuid, p_start date, p_end date, p_workspace text default 'all')
returns jsonb language sql stable security invoker set search_path=public as $$
with scoped as (
 select *, (occurred_at at time zone 'America/Chicago')::date as work_day,
 entity_type || ':' || entity_id as work_key
 from public.appraiser_activity_events
 where district_id=p_district_id
 and (p_workspace='all' or (p_workspace='commercial' and module='commercial') or (p_workspace='bpp' and module<>'commercial'))
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
 count(*) filter(where action='commercial_account_created') as commercial_accounts_created,
 count(*) filter(where action='commercial_update_undone') as commercial_undos,
 coalesce(sum(units) filter(where action='commercial_update_applied'),0) as commercial_rows_updated,
 count(*) filter(where action='commercial_report_generated') as commercial_reports,
 coalesce(sum(units) filter(where action='commercial_report_generated'),0) as commercial_report_rows,
 count(*) filter(where action='commercial_equity_generated') as commercial_equity_analyses,
 count(distinct work_day) as active_days
 from scoped group by actor_id
), daily as (
 select work_day, count(*) as actions,
 count(distinct work_key) filter(where action='review_completed') as completed,
 count(distinct work_key) filter(where action='rendition_locked') as renditions,
 count(*) filter(where action='commercial_update_applied') as commercial_updates,
 coalesce(sum(units) filter(where action='commercial_update_applied'),0) as commercial_rows_updated,
 count(*) filter(where action='commercial_report_generated') as commercial_reports
 from scoped group by work_day
), outcomes as (
 select module, action, outcome, count(*) as actions, count(distinct work_key) as work_items,
 sum(units) as units from scoped group by module, action, outcome
)
select jsonb_build_object(
 'start',p_start,'end',p_end,'timezone','America/Chicago','workspace',p_workspace,
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
   'commercial_undos',count(*) filter(where action='commercial_update_undone'),
   'commercial_reports',count(*) filter(where action='commercial_report_generated'),
   'commercial_report_rows',coalesce(sum(units) filter(where action='commercial_report_generated'),0),
   'commercial_equity_analyses',count(*) filter(where action='commercial_equity_generated'),
   'reports',count(*) filter(where action in ('rendition_report_generated','commercial_report_generated','account_summary_generated')))
   from scoped),
 'appraisers',coalesce((select jsonb_agg(to_jsonb(people) order by actions desc,appraiser) from people),'[]'::jsonb),
 'daily',coalesce((select jsonb_agg(to_jsonb(daily) order by work_day) from daily),'[]'::jsonb),
 'outcomes',coalesce((select jsonb_agg(to_jsonb(outcomes) order by module,action,outcome) from outcomes),'[]'::jsonb)
);
$$;
revoke all on function public.appraiser_activity_report_v2(uuid,date,date,text) from public, anon, authenticated;
grant execute on function public.appraiser_activity_report_v2(uuid,date,date,text) to service_role;
notify pgrst, 'reload schema';
