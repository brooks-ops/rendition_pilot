-- LOCAL/DISPOSABLE DATABASE ONLY. Minimal pre-migration contract, not an install script.
-- Supabase roles must exist on the local PostgreSQL cluster.
create extension if not exists pgcrypto;
create table districts(id uuid primary key default gen_random_uuid(),name text not null,slug text not null unique);
create table jurisdictions(id uuid primary key default gen_random_uuid(),district_id uuid references districts(id),
 name text not null,slug text not null unique,county_name text not null,state text not null default 'TX');
create table real_property_records(id uuid primary key default gen_random_uuid(),jurisdiction_id uuid not null references jurisdictions(id),
 source_property_id text not null);
create table mailing_address_observations(
 id uuid primary key default gen_random_uuid(),jurisdiction_id uuid not null references jurisdictions(id),
 account_identifier_type text not null,account_identifier text not null,source text not null,source_record_id text,
 source_effective_date date,observed_at timestamptz not null default now(),raw_address_line text,address_type text,po_box_number text,
 unit text,city text,state text,zip text,zip4 text,normalized_full_address text not null,created_at timestamptz not null default now());
create unique index mailing_address_observations_dedup_idx on mailing_address_observations
 (jurisdiction_id,account_identifier_type,account_identifier,source,normalized_full_address);
create table bpp_intelligence_items(
 id uuid primary key default gen_random_uuid(),jurisdiction_id uuid not null references jurisdictions(id),district_id uuid not null references districts(id),
 signal_type text not null,source text not null,source_record_id text not null,source_taxpayer_id text,
 status text not null default 'NEW' check(status in ('NEW','IN_REVIEW','RESOLVED','DISMISSED')),
 classification text not null,priority text not null default 'MEDIUM' check(priority in ('HIGH','MEDIUM','LOW')),
 confidence text not null default 'UNMATCHED' check(confidence in ('HIGH','MEDIUM','LOW','UNMATCHED')),
 business_name text,legal_name text,first_detected_at timestamptz,matched_record_id text,matched_account_number text,matched_owner_name text,
 mailing_address_current text,mailing_address_observed text,match_reason text,match_signals jsonb,recommended_action text,evidence jsonb,
 resolution text,resolution_notes text,reviewed_by uuid,reviewed_at timestamptz,assigned_to uuid,created_at timestamptz not null default now());
create unique index bpp_intelligence_items_dedup_idx on bpp_intelligence_items(signal_type,source,source_record_id);
