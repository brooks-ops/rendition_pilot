begin;
-- Separate from review state: research must never reset decisions/reservations.
create table public.returned_mail_research (
 document_id uuid primary key references public.returned_mail_documents(id) on delete cascade,
 district_id uuid not null references public.districts(id),
 status text not null default 'QUEUED' check(status in ('QUEUED','RUNNING','COMPLETE','PARTIAL','FAILED','SKIPPED')),
 attempts integer not null default 0, queued_at timestamptz not null default now(),
 started_at timestamptz, finished_at timestamptz, retry_at timestamptz not null default now(),
 lease_token uuid, lease_until timestamptz, result jsonb not null default '{}', error text
);
create index returned_mail_research_pending on public.returned_mail_research(district_id,retry_at) where status in ('QUEUED','PARTIAL','FAILED','RUNNING');
create table public.returned_mail_research_runs (
 id uuid primary key, document_id uuid not null references public.returned_mail_documents(id) on delete cascade,
 district_id uuid not null, started_at timestamptz not null default now(), finished_at timestamptz,
 status text not null default 'RUNNING', result jsonb not null default '{}', error text
);
create function public.returned_mail_queue_research() returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
 insert into returned_mail_research(document_id,district_id) values(new.id,new.district_id) on conflict do nothing;
 return new;
end $$;
create trigger returned_mail_research_enqueue after insert on public.returned_mail_documents for each row execute function public.returned_mail_queue_research();

create function public.returned_mail_claim_research(p_district_id uuid,p_document_id uuid default null)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare job returned_mail_research; token uuid:=gen_random_uuid();
begin
 update returned_mail_research_runs set status='INTERRUPTED',finished_at=now(),error='Retry limit reached after an interrupted worker.'
 where district_id=p_district_id and status='RUNNING' and id in
 (select lease_token from returned_mail_research where district_id=p_district_id and attempts>=5 and status='RUNNING' and lease_until<now());
 update returned_mail_research set status='FAILED',finished_at=now(),lease_until=null,error='Automatic retry limit reached; administrator attention needed.'
 where district_id=p_district_id and attempts>=5 and status='RUNNING' and lease_until<now();
 select r.* into job from returned_mail_research r join returned_mail_documents d on d.id=r.document_id
 join bpp_intelligence_items i on i.id=d.intelligence_item_id
 where r.district_id=p_district_id and d.district_id=p_district_id and i.district_id=p_district_id
 and (p_document_id is null or r.document_id=p_document_id)
 and d.review_status='PENDING' and i.status in ('NEW','IN_REVIEW') and r.attempts<5
 and ((r.status in ('QUEUED','PARTIAL','FAILED') and r.retry_at<=now()) or (r.status='RUNNING' and r.lease_until<now()))
 order by r.queued_at,r.document_id limit 1 for update of r skip locked;
 if job.document_id is null then return null;end if;
 update returned_mail_research_runs set status='INTERRUPTED',finished_at=now(),error='Previous worker did not finish; retrying.'
 where id=job.lease_token and status='RUNNING';
 update returned_mail_research set status='RUNNING',attempts=attempts+1,started_at=now(),finished_at=null,
 lease_token=token,lease_until=now()+interval '10 minutes',error=null where document_id=job.document_id returning * into job;
 insert into returned_mail_research_runs(id,document_id,district_id) values(token,job.document_id,p_district_id);
 return to_jsonb(job);
end $$;
create function public.returned_mail_finish_research(p_district_id uuid,p_document_id uuid,p_token uuid,p_status text,p_result jsonb,p_error text default null)
returns boolean language plpgsql security definer set search_path=public,pg_temp as $$
begin
 if p_status not in ('COMPLETE','PARTIAL','FAILED','SKIPPED') then raise exception 'Invalid research status';end if;
 update returned_mail_research set status=p_status,result=p_result,error=p_error,finished_at=now(),lease_until=null,
 retry_at=now()+interval '1 day' where document_id=p_document_id and district_id=p_district_id and lease_token=p_token and status='RUNNING' and lease_until>now();
 if not found then return false;end if;
 update returned_mail_research_runs set status=p_status,result=p_result,error=p_error,finished_at=now() where id=p_token and district_id=p_district_id;
 return true;
end $$;
-- Backfill only unfinished evidence. Completed reviews and assignment history stay intact.
insert into returned_mail_research(document_id,district_id)
select d.id,d.district_id from returned_mail_documents d join bpp_intelligence_items i on i.id=d.intelligence_item_id
where d.review_status='PENDING' and i.status in ('NEW','IN_REVIEW') on conflict do nothing;

alter table public.returned_mail_research enable row level security;
alter table public.returned_mail_research_runs enable row level security;
revoke all on public.returned_mail_research,public.returned_mail_research_runs from public,anon,authenticated;
grant select,insert,update,delete on public.returned_mail_research to service_role;
grant select,insert,update on public.returned_mail_research_runs to service_role;
revoke all on function public.returned_mail_queue_research(),public.returned_mail_claim_research(uuid,uuid),public.returned_mail_finish_research(uuid,uuid,uuid,text,jsonb,text) from public,anon,authenticated;
grant execute on function public.returned_mail_claim_research(uuid,uuid),public.returned_mail_finish_research(uuid,uuid,uuid,text,jsonb,text) to service_role;
commit;
