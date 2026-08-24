-- New Account Enrichment: the final leg of the pipeline described in
-- docs/property_enrichment.md --
--   New BPP Account -> Address normalization -> Property Enrichment ->
--   PropertyID -> R account -> TUG -> Neighborhood -> Map ->
--   Appraiser assignment -> Suggested property link -> Account card
--
-- Everything through "Suggested property link" already existed (Property
-- Enrichment). This migration adds only what's new: a jurisdiction-scoped
-- appraiser-assignment mapping, and an audit marker on bpp_intelligence_items
-- for when a card was last generated for that item. The card's CONTENT is
-- never separately stored -- it's fully reconstructible on demand from the
-- intelligence item + real_property_records + this mapping, so there is
-- nothing to go stale or need its own migration path.
--
-- No real Lubbock appraiser-assignment rules exist (same honesty as
-- property_field_mapping/real_property_records at this point) -- the
-- mapping defaults to empty, and every account card will show
-- basis="unassigned" until a CAD supplies real TUG/neighborhood -> appraiser
-- rules.

alter table public.jurisdictions
  add column if not exists appraiser_assignment_rules jsonb not null default '{}'::jsonb;

alter table public.bpp_intelligence_items
  add column if not exists account_card_generated_at timestamptz;
