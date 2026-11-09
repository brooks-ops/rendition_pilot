-- Disposable local DB only, after mailing_phase1_fixture.sql and both mailing migrations.
create schema if not exists storage;
create table if not exists storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint,allowed_mime_types text[]);
alter table public.bpp_intelligence_items add column if not exists is_ambiguous boolean not null default false;
create table if not exists public.bpp_work_claims(district_id uuid not null,work_key text not null,actor_id uuid not null,primary key(district_id,work_key));
