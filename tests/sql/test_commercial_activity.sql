-- Integration checks run in a transaction; no fake activity is retained.
begin;
insert into public.appraiser_activity_events(district_id,actor_id,actor_email,action,module,entity_type,entity_id,units,occurred_at)
select district.id, events.actor::uuid, 'analytics-test@example.invalid', events.action, events.module,
 'analytics_test', events.entity, events.units, '2099-02-01 12:00:00+00'
from (select id from public.districts order by id limit 1) district
cross join (values
 ('00000000-0000-0000-0000-000000000111','review_completed','business_intelligence','bpp',1),
 ('00000000-0000-0000-0000-000000000222','commercial_update_applied','commercial','batch',50),
 ('00000000-0000-0000-0000-000000000222','commercial_update_undone','commercial','batch',1),
 ('00000000-0000-0000-0000-000000000222','commercial_account_created','commercial','account',1),
 ('00000000-0000-0000-0000-000000000222','commercial_report_generated','commercial','report',3),
 ('00000000-0000-0000-0000-000000000222','commercial_equity_generated','commercial','account',1)
) events(actor,action,module,entity,units);
with reports as (
 select public.appraiser_activity_report_v2((select id from public.districts order by id limit 1),'2099-02-01','2099-02-01','commercial') as commercial,
 public.appraiser_activity_report_v2((select id from public.districts order by id limit 1),'2099-02-01','2099-02-01','bpp') as bpp,
 public.appraiser_activity_report_v2((select id from public.districts order by id limit 1),'2099-02-01','2099-02-01','all') as all_work,
 public.appraiser_activity_report_v2('00000000-0000-0000-0000-000000000333','2099-02-01','2099-02-01','commercial') as other_district
)
select 1 / case when
 commercial#>>'{totals,commercial_rows_updated}'='50'
 and commercial#>>'{totals,commercial_updates}'='1'
 and commercial#>>'{totals,commercial_undos}'='1'
 and commercial#>>'{totals,commercial_reports}'='1'
 and commercial#>>'{totals,commercial_report_rows}'='3'
 and commercial#>>'{totals,commercial_equity_analyses}'='1'
 and commercial#>>'{totals,commercial_accounts_created}'='1'
 and commercial#>>'{totals,actions}'='5'
 and commercial#>>'{appraisers,0,commercial_rows_updated}'='50'
 and commercial#>>'{daily,0,commercial_rows_updated}'='50'
 and jsonb_array_length(commercial->'appraisers')=1
 and bpp#>>'{totals,actions}'='1'
 and all_work#>>'{totals,actions}'='6'
 and other_district#>>'{totals,actions}'='0'
 and not has_function_privilege('authenticated','public.appraiser_activity_report_v2(uuid,date,date,text)','EXECUTE')
 then 1 else 0 end as all_commercial_checks_passed
from reports;
rollback;
