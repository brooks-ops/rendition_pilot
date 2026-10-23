-- Transactional integration checks: no test events remain after rollback.
begin;
do $$
declare d uuid; report jsonb; begin
 select id into d from public.districts order by id limit 1;
 if d is null then raise exception 'Test requires an existing district'; end if;
 insert into public.appraiser_activity_events(district_id,actor_id,actor_email,action,module,entity_type,entity_id,occurred_at)
 values
 (d,'00000000-0000-0000-0000-000000000111','analytics-test-one@example.invalid','review_completed','business_intelligence','analytics_test','one','2099-01-01 05:59:00+00'),
 (d,'00000000-0000-0000-0000-000000000111','analytics-test-one@example.invalid','review_completed','business_intelligence','analytics_test','one','2099-01-01 06:01:00+00'),
 (d,'00000000-0000-0000-0000-000000000111','analytics-test-one@example.invalid','review_completed','business_intelligence','analytics_test','one','2099-01-01 06:02:00+00'),
 (d,'00000000-0000-0000-0000-000000000222','analytics-test-two@example.invalid','review_completed','business_intelligence','analytics_test','one','2099-01-01 06:03:00+00'),
 (d,'00000000-0000-0000-0000-000000000222','analytics-test-two@example.invalid','review_dismissed','mailing_changes','analytics_test','two','2099-01-01 06:04:00+00');
 report := public.appraiser_activity_report(d,'2099-01-01','2099-01-01');
 if (report#>>'{totals,completed}')::int<>1 then raise exception 'Duplicate completion inflation'; end if;
 if (report#>>'{totals,actions}')::int<>4 then raise exception 'Chicago timezone boundary failure'; end if;
 if (report#>>'{totals,dismissed}')::int<>1 then raise exception 'Dismissal classification failure'; end if;
 if jsonb_array_length(report->'appraisers')<>2 then raise exception 'Actor attribution failure'; end if;
 if (public.appraiser_activity_report('00000000-0000-0000-0000-000000000333','2099-01-01','2099-01-01')#>>'{totals,actions}')::int<>0 then raise exception 'District leak'; end if;
 if has_table_privilege('authenticated','public.appraiser_activity_events','SELECT') then raise exception 'Member access leak'; end if;
 if has_function_privilege('authenticated','public.appraiser_activity_report(uuid,date,date)','EXECUTE') then raise exception 'RPC access leak'; end if;
 if has_table_privilege('service_role','public.appraiser_activity_events','DELETE') then raise exception 'Ledger is mutable'; end if;
end $$;
rollback;
select 'Activity aggregation, district isolation, timezone boundaries and access checks passed; no test data retained' as result;
