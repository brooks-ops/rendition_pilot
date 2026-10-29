# BPP new-account field worksheet

The supplied LCAD workbook defines the field groups: account identity, mailing,
situs, contacts, geography/property codes, permits, new-value indicators and
valuation segments. Its populated sample business, CBB/BP1 appraisers, account
link, codes and $15,000 value are examples, never app defaults. The card uses one "Permit issued" date, as requested. The source workbook is read-only and remains outside Git.

Cards open automatically on new-business leads, potential-new-business DBA
filings, or saved NEW_ACCOUNT_NEEDED outcomes. Other assumed-name leads offer an
Open new account card button. Abandonment alone does not trigger a new card.
Known evidence is prefilled; missing mailing/contact/code/valuation data stays
blank. Recording dates are not permit dates. County branding comes from the
current jurisdiction; only Lubbock uses the LCAD profile.

Start editing uses the existing BPP reservation. Save persists a versioned JSON
draft on that same intelligence item. A service-only transactional RPC checks
district, owner, review status and version, then appends an audit snapshot.
Concurrent/stale saves fail without overwriting either user's draft. Resolved
and dismissed reviews are read-only. Unsaved changes cannot be silently lost by
finishing or releasing a review. Saved cards can be reopened and printed from
the detail page. Print card opens a printable worksheet with a Print/save PDF
button; it does not launch a printer automatically.

Blank segment values remain unknown; real zeros are retained. Server totals
use decimal arithmetic. No official CAMA account is created or changed and
field-review estimates do not alter CAD matching or Intelligence priorities.

Apply `20261028_bpp_account_cards.sql` before deploying. Python tests validate
prefill boundaries, identifiers, zeros, totals and API concurrency handling.
`tests/sql/bpp_account_cards.sql` uses rolled-back disposable fixtures to check
saved drafts, audit history, ownership/district isolation and stale versions.
Local browser testing covers reserve/edit/save/reopen and the printable view.

## Finishing a card

**Finish & print / PDF** saves the current draft first, then opens the saved
worksheet in a print view. Choose **Print / save PDF**, then select a printer or
Save as PDF in the browser print dialog. A failed save keeps edits in place and
does not export a misleading saved copy. **Save draft** supports unfinished work;
**Print / save PDF** remains available on read-only cards. Completing the
investigation still uses **Mark as complete**.

Known details remain prefilled. The mailing shortcut explicitly copies the
business location only when selected and asks before replacing different mailing
details. Property codes are expandable, New value uses a Yes/No selector, and
Last inspected has a local-date shortcut. None of these populate unknown facts
without appraiser input.
