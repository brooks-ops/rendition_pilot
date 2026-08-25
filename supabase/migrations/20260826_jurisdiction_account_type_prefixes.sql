-- Makes the P/R (personal-property vs real-property) account-number prefix
-- convention jurisdiction-configurable, per a portability audit finding:
-- classify_account_type() hardcoded "P" = BPP account, "R" = real-property
-- account -- that's Lubbock's CAMA vendor's (True Automation's) convention,
-- not a universal Texas-CAD standard. A county whose software uses a
-- different (or no) prefix scheme would previously have had every account
-- silently classify as "unknown," quietly disabling HIGH-confidence
-- corroboration and the Account Card's "existing P-account found" flag
-- with no error raised.
--
-- Defaults to Lubbock's actual real-world convention ({"personal": "P",
-- "real": "R"}) so existing behavior is completely unchanged unless a
-- jurisdiction is explicitly reconfigured -- same additive, zero-risk
-- pattern as every other jurisdiction-config column added so far.

alter table public.jurisdictions
  add column if not exists account_type_prefixes jsonb not null default '{"personal": "P", "real": "R"}'::jsonb;
