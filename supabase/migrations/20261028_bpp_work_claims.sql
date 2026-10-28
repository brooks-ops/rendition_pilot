-- Persistent account reservations, scoped by district. Only the server may access these.
create table if not exists public.bpp_work_claims (
 district_id uuid not null references public.districts(id),
 work_key text not null,
 actor_id uuid not null,
 actor_email text not null,
 source_table text not null,
 item_id text not null,
 claimed_at timestamptz not null default now(),
 primary key(district_id,work_key)
);
alter table public.bpp_work_claims enable row level security;
revoke all on public.bpp_work_claims from anon,authenticated;
grant select,insert,delete on public.bpp_work_claims to service_role;
create or replace function public.bpp_claim_work(p_district_id uuid,p_work_key text,p_actor_id uuid,p_actor_email text,p_source_table text,p_item_id text)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare result public.bpp_work_claims;
begin
 -- Serialize account and source-item aliases, including an unmatched item that gains a match.
 perform pg_advisory_xact_lock(hashtextextended(p_district_id::text,0));
 select * into result from public.bpp_work_claims where district_id=p_district_id
 and source_table=p_source_table and item_id=p_item_id and actor_id<>p_actor_id limit 1;
 if found then return to_jsonb(result); end if;
 -- The unique key arbitrates concurrent starts across all application instances.
 insert into public.bpp_work_claims(district_id,work_key,actor_id,actor_email,source_table,item_id)
 values(p_district_id,p_work_key,p_actor_id,p_actor_email,p_source_table,p_item_id)
 on conflict(district_id,work_key) do nothing;
 select * into result from public.bpp_work_claims where district_id=p_district_id and work_key=p_work_key;
 return to_jsonb(result);
end $$;
revoke all on function public.bpp_claim_work(uuid,text,uuid,text,text,text) from public,anon,authenticated;
grant execute on function public.bpp_claim_work(uuid,text,uuid,text,text,text) to service_role;
