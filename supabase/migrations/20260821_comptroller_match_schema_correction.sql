-- Follow-up to 20260818_comptroller_closure_monitor.sql after live-schema
-- validation (2026-08-21) found that RenditionPilot's production Supabase
-- project has no `accounts` table -- the tables the original 20260429
-- migration assumed already existed (`accounts`, `renditions`, etc.) do not
-- exist. The real schema is `rendition_uploads` -> `rendition_jobs` ->
-- `parsed_rendition_results`, none of which carry a situs address, city,
-- ZIP, or DBA name. app/comptroller/matching.py was rewritten accordingly to
-- match on owner-name similarity only (see its module docstring). This
-- migration only adds a column to expose the resulting ambiguity signal
-- (multiple RenditionPilot records scoring similarly against one closure) to
-- the review queue -- it does not touch any appraisal/CAD data.

alter table public.comptroller_closure_reviews add column if not exists match_ambiguous boolean not null default false;

create index if not exists comptroller_closure_reviews_match_ambiguous_idx
on public.comptroller_closure_reviews (match_ambiguous)
where match_ambiguous is true;
