-- Mailing Address Intelligence V1.
--
-- Adds the taxpayer MAILING address (distinct from the situs/location
-- address `comptroller_permit_locations.address` already carries) to the
-- Comptroller sync, a lightweight cross-source observation history table,
-- and the new `mailing_address_change` signal type in the existing BPP
-- Intelligence Queue -- reusing that queue's whole read/write/review
-- lifecycle rather than building a second one.
--
-- Confirmed via a live Comptroller API query before writing this: the
-- taxpayer mailing address (tp_address/tp_city/tp_state/tp_zip) is a real,
-- independently-sourced field from the outlet's situs address, and
-- genuinely differs for real records (including PO-Box-vs-street cases,
-- e.g. a national retailer's Lubbock store vs. its out-of-state corporate
-- mailing address). It has always been present in the raw Socrata
-- response but was previously discarded before reaching any RenditionPilot
-- table -- see app/comptroller/client.py's PermitRecord docstring.
--
-- Deliberately additive-only: comptroller_permit_locations gains 5 nullable
-- columns, bpp_intelligence_items gains 2 nullable columns plus new
-- CHECK-constraint values (old values untouched), and mailing_address_observations
-- is a brand-new table. Nothing about Sales Tax Inactive monitoring, New
-- Business Detection, or Property Enrichment changes shape.

alter table public.comptroller_permit_locations add column if not exists mailing_address text;
alter table public.comptroller_permit_locations add column if not exists mailing_city text;
alter table public.comptroller_permit_locations add column if not exists mailing_state text;
alter table public.comptroller_permit_locations add column if not exists mailing_zip text;
alter table public.comptroller_permit_locations add column if not exists mailing_zip4 text;

-- Lightweight, append-only history of observed mailing addresses per
-- (jurisdiction, account identity, source). One row per DISTINCT address
-- ever observed for that identity+source -- re-observing the SAME address
-- on a later sync is a no-op (dedup index below), not a new row every day;
-- observing a genuinely DIFFERENT address for the same identity+source
-- creates a new row, preserving full history (spec: never suppress a
-- genuinely newer address just because an older one is on file).
--
-- `account_identifier` is deliberately a plain text key, not a foreign key
-- to any "BPP account" table -- no such master-account table exists in
-- this schema (confirmed via inspection); RenditionPilot's own account
-- identity is a string (a Comptroller taxpayer_id today; a BPP account
-- number once rendition-driven comparison has real data to work with).
create table if not exists public.mailing_address_observations (
  id uuid primary key default gen_random_uuid(),
  jurisdiction_id uuid not null references public.jurisdictions(id) on delete cascade,

  account_identifier_type text not null,
  account_identifier text not null,

  source text not null,
  source_record_id text,
  -- The source's own as-of date for this address, when it provides one --
  -- e.g. a rendition's signed date. Distinct from observed_at (when
  -- RenditionPilot's own ingestion actually processed it) -- never invented
  -- when the source doesn't supply one (spec item 22).
  source_effective_date date,
  observed_at timestamptz not null default now(),

  raw_address_line text,
  address_type text,
  po_box_number text,
  unit text,
  city text,
  state text,
  zip text,
  zip4 text,
  normalized_full_address text not null,

  created_at timestamptz not null default now()
);

do $$
begin
  begin
    alter table public.mailing_address_observations
      add constraint mailing_address_observations_identifier_type_check
      check (account_identifier_type in ('comptroller_taxpayer', 'bpp_account'));
  exception
    when duplicate_object then null;
  end;
  begin
    alter table public.mailing_address_observations
      add constraint mailing_address_observations_address_type_check
      check (address_type is null or address_type in ('STREET', 'PO_BOX', 'UNKNOWN'));
  exception
    when duplicate_object then null;
  end;
end
$$;

create unique index if not exists mailing_address_observations_dedup_idx
on public.mailing_address_observations (jurisdiction_id, account_identifier_type, account_identifier, source, normalized_full_address);

create index if not exists mailing_address_observations_identity_idx
on public.mailing_address_observations (jurisdiction_id, account_identifier_type, account_identifier, observed_at desc);

-- bpp_intelligence_items: new signal type + resolution/classification values,
-- plus two columns for direct display (source_address/matched_address on
-- this table are already situs-semantic per New Business Detection/Property
-- Enrichment -- reusing them for mailing addresses would silently overload
-- an existing meaning, so these are genuinely new columns instead).
alter table public.bpp_intelligence_items add column if not exists mailing_address_current text;
alter table public.bpp_intelligence_items add column if not exists mailing_address_observed text;

alter table public.bpp_intelligence_items drop constraint if exists bpp_intelligence_items_signal_type_check;
alter table public.bpp_intelligence_items
  add constraint bpp_intelligence_items_signal_type_check
  check (signal_type in ('sales_tax_inactive', 'new_business', 'mailing_address_change'));

alter table public.bpp_intelligence_items drop constraint if exists bpp_intelligence_items_classification_check;
alter table public.bpp_intelligence_items
  add constraint bpp_intelligence_items_classification_check
  check (classification in (
    'EXISTING_ACCOUNT_HIGH_CONFIDENCE', 'POSSIBLE_EXISTING_ACCOUNT', 'NO_ACCOUNT_FOUND', 'AMBIGUOUS',
    'LIKELY_MAILING_ADDRESS_CHANGE', 'POSSIBLE_MAILING_ADDRESS_CHANGE', 'CONFIRMED_MAILING_ADDRESS_CHANGE'
  ));

alter table public.bpp_intelligence_items drop constraint if exists bpp_intelligence_items_resolution_check;
alter table public.bpp_intelligence_items
  add constraint bpp_intelligence_items_resolution_check
  check (resolution in (
    'ACCOUNT_EXISTS', 'NEW_ACCOUNT_NEEDED', 'BUSINESS_CLOSED', 'RELOCATION',
    'OWNERSHIP_CHANGE', 'DUPLICATE', 'FALSE_MATCH', 'NO_TAXABLE_BPP', 'OTHER',
    'ADDRESS_UPDATED', 'CURRENT_ADDRESS_CORRECT', 'SOURCE_OUTDATED',
    'DUPLICATE_SIGNAL', 'ACCOUNT_MISMATCH', 'INSUFFICIENT_EVIDENCE'
  ));
