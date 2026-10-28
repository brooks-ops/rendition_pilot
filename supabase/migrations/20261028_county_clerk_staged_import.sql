begin;
create table if not exists public.county_clerk_import_batches (
 import_id uuid not null references public.county_clerk_dba_imports(id),
 batch_number integer not null check(batch_number between 0 and 2999),
 filings jsonb not null check(jsonb_typeof(filings)='array' and jsonb_array_length(filings) between 1 and 25),
 primary key(import_id,batch_number)
);
alter table public.county_clerk_import_batches enable row level security;
revoke all on public.county_clerk_import_batches from public,anon,authenticated;
grant select,insert on public.county_clerk_import_batches to service_role;
create or replace function public.dba_stage_import_batch(p_import_id uuid,p_batch_number integer,p_filings jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare imp public.county_clerk_dba_imports%rowtype; saved jsonb;
begin
 select * into imp from public.county_clerk_dba_imports where id=p_import_id for update;
 if not found or imp.status!='PROCESSING' then raise exception 'Import is not processing';end if;
 insert into public.county_clerk_import_batches(import_id,batch_number,filings) values(p_import_id,p_batch_number,p_filings)
 on conflict(import_id,batch_number) do nothing;
 select filings into saved from public.county_clerk_import_batches where import_id=p_import_id and batch_number=p_batch_number;
 if saved!=p_filings then raise exception 'Original import batch differs';end if;
 return jsonb_build_object('batch_number',p_batch_number,'records',jsonb_array_length(saved));
end $$;
create or replace function public.dba_commit_staged_import(p_import_id uuid)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare imp public.county_clerk_dba_imports%rowtype; n integer; first_n integer; last_n integer; total integer; records jsonb;
begin
 select * into imp from public.county_clerk_dba_imports where id=p_import_id for update;
 if not found then raise exception 'Import not found';end if;
 if imp.status in ('COMPLETED','PARTIAL') then return public.dba_commit_import(p_import_id,'[]'::jsonb);end if;
 select count(*),min(batch_number),max(batch_number),sum(jsonb_array_length(filings)) into n,first_n,last_n,total
 from public.county_clerk_import_batches where import_id=p_import_id;
 if n<1 or first_n!=0 or last_n!=n-1 or total!=imp.extracted_count then raise exception 'Incomplete staged filing set';end if;
 select jsonb_agg(f.value order by b.batch_number,f.ordinality) into records
 from public.county_clerk_import_batches b cross join lateral jsonb_array_elements(b.filings) with ordinality f(value,ordinality)
 where b.import_id=p_import_id;
 return public.dba_commit_import(p_import_id,records);
end $$;
revoke all on function public.dba_stage_import_batch(uuid,integer,jsonb) from public,anon,authenticated;
revoke all on function public.dba_commit_staged_import(uuid) from public,anon,authenticated;
grant execute on function public.dba_stage_import_batch(uuid,integer,jsonb) to service_role;
grant execute on function public.dba_commit_staged_import(uuid) to service_role;
notify pgrst,'reload schema';
commit;
