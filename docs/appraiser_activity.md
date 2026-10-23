# Appraiser activity

Admin > Appraiser activity reports the signed-in user's district only. Members cannot call the reporting API or read the event table/RPC directly. The event store records verified auth user ID/email, district, server timestamp, action, module, internal entity identity, optional outcome code and units. It excludes notes, documents, addresses, assessed values, passwords and access tokens. Former staff remain in historical totals.

Apply `supabase/migrations/20261023_appraiser_activity.sql` before deploying. Tracking begins at migration activation; no historical events are invented. All dates use America/Chicago, including inclusive start/end boundaries. Reports allow at most 366 days and aggregate in PostgreSQL, avoiding PostgREST row limits. All configured districts use the same implementation.

Instrumented successful operations:
- BPP Intelligence / Mailing: start, complete, dismiss, CAD match decision, new-account summary.
- Rendition: lock final review and generate stamped PDF.
- Commercial: apply a batch, undo a batch, create an account row, generate PDF or individual packet ZIP.

Read-only browsing, page time, clicks, previews, failed operations, imports and background refreshes do not count as appraiser work. Completion represents the user's Mark as complete action, regardless of the legacy closure table's status vocabulary. A request retry can add another action unless the workflow supplies a stable operation ID (Commercial batches/create/undo are deduplicated). Completion and locked rendition totals count distinct entity/year in the selected period. Per-person counts overlap when multiple people handle one item; do not sum them to infer district-unique volume. Commercial batches are distinct from rows updated; the product impact export includes both. Reports/summaries count generation requests, not recipients, downloads, or confirmed CAD updates. An active day is a day with an instrumented action, not hours worked.

Team CSV contains appraiser totals and is internal. Product impact CSV includes aggregate totals and grouped outcomes only, excluding appraiser identities and entity IDs. CSV strings are escaped against formula execution. No automatic email, public sharing, cross-customer access or external analytics provider is added. Counts are activity evidence, not measured time saved, assessed value increases, verified CAD changes, or a staff performance score.

Event insert failures emit a sanitized application error and do not fail the appraiser's already successful operation. Failed telemetry cannot be reconstructed automatically: investigate these logs before using totals as contractual or compensation measures. SQL integration tests use a rolled-back transaction and leave no fake production events.
