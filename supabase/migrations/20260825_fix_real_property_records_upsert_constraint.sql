-- Fix: real_property_records' two PARTIAL unique indexes
-- (real_property_records_dated_unique_idx WHERE tax_year is not null,
-- real_property_records_undated_unique_idx WHERE tax_year is null) cannot
-- be targeted by PostgREST's upsert. PostgREST's `on_conflict` parameter is
-- a plain column list -- Postgres only infers a matching unique
-- index/constraint from a bare column list when that index has NO partial
-- predicate; a partial index requires the predicate to be specified
-- alongside the conflict target, which PostgREST has no way to send. Every
-- import attempt against a real property export (real Lubbock 2027 data,
-- 240,626 rows, 100% with a tax_year) failed immediately with "there is no
-- unique or exclusion constraint matching the ON CONFLICT specification" --
-- discovered the moment real data was used, exactly the scenario this
-- project's process exists to catch before it reaches more jurisdictions.
--
-- real_property_records was empty when this was found (the failed import
-- wrote zero rows), so this is a same-day, zero-data-loss fix, not a
-- migration of populated data.
--
-- Fix: one plain (non-partial) unique index on all three columns, which
-- PostgREST's on_conflict=jurisdiction_id,source_property_id,tax_year can
-- target directly. Trade-off, documented and accepted: Postgres treats NULL
-- as distinct from every other NULL in a unique index, so multiple
-- "yearless" rows (a source with no tax-year column at all) would no
-- longer be deduplicated against each other. No real jurisdiction's export
-- lacks a tax year today; if one ever does, revisit with a generated
-- sentinel year rather than reintroducing a partial index PostgREST can't
-- upsert against.

drop index if exists public.real_property_records_dated_unique_idx;
drop index if exists public.real_property_records_undated_unique_idx;

create unique index if not exists real_property_records_upsert_unique_idx
on public.real_property_records (jurisdiction_id, source_property_id, tax_year);
