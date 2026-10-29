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
**Print / save PDF** remains available on read-only cards. Finishing also resolves the review as **New account needed**, using the existing
completion endpoint, work ownership checks, and audit trail. Save draft and
standalone reprints do not complete it. Browsers cannot verify whether printing
or PDF saving actually succeeded; completion records that the appraiser finished
the card and opened its output. Failed completion remains visible for retry.

Known details remain prefilled. The mailing shortcut explicitly copies the
business location only when selected and asks before replacing different mailing
details. Property codes are expandable, New value uses a Yes/No selector, and
Last inspected has a local-date shortcut. None of these populate unknown facts
without appraiser input.

## Batch output

Choose **Finish — print later** to save and complete without opening output. In
BPP Intelligence, expand **Print completed account cards**, load your cards,
select any subset, and choose **Print selected / save PDF**. Select all loaded
and Load more support larger batches; pages load 100 records at a time. The
result is one printable document with a page break before each account. Saved
worksheet values are reused without enrichment or changes to review status.
The endpoint enforces the authenticated district and completing appraiser;
regular appraisers cannot list other appraisers' completed cards. District admins can view their district's team cards. Standalone reprints are safe.

## Print confirmation and duplicate prevention

After a successful print or PDF save, choose **I printed / saved this PDF** in
the output window. Opening a print preview or cancelling a print dialog does
not set the flag. Single-card and batch output use the same confirmation.
The saved status is per card version, with confirming appraiser and timestamp.
Repeated confirmation preserves the original timestamp. The service-only RPC
validates the whole batch against completed reviews, district, completing
appraiser, and saved version before writing anything.

Completed cards display Not printed or Printed / PDF saved with date/time.
Printed cards are hidden by default; **Select unprinted** skips them. Unhide
them for an intentional reprint, which asks for confirmation. Prior print jobs
cannot be detected retroactively and remain unconfirmed until acknowledged.
Deploy migration `20261029_bpp_account_card_prints.sql` before the API update.

## Appraiser access

All card actions require BPP workspace access, not the admin role. Regular
member and appraiser roles can reserve, fill, save/reopen, finish, list their
completed cards, batch print, and confirm printing. The completed-card list is
scoped to the signed-in completing appraiser and district. Admin/user management
and Readiness retain their separate restrictions. HTTP regression tests in
`tests/test_workspace_access.py` exercise this flow with the real BPP authorization
guard for both roles, and verify users without BPP access are rejected.

## Admin oversight

District admins see all completed saved cards in their own district. The
Appraiser selector includes completing appraisers (with an identifier fallback
for former staff). Print status offers All, Not printed, and Printed / PDF saved.
Filters are applied in PostgreSQL before pagination, so they cover the full
collection rather than just loaded rows. Regular appraisers retain own-only
access even when sending filter requests directly. Admins can also batch print
and confirm the team's cards; the confirming admin is retained in the print
record without changing who completed the review.
Deploy `20261029_bpp_completed_card_admin.sql` before this API version.
