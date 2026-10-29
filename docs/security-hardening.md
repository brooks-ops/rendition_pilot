# Security hardening — October 29, 2026

## Authentication and authorization

District administrators and the single owner Super Admin must verify a TOTP authenticator before protected district tools or cross-district tools can be used. Sign-in/restore remain available at AAL1 so a user can enroll or challenge a factor. Every authorization decision first verifies the bearer token with Supabase `/user`, resolves current district membership, and checks the verified JWT subject and `aal2` claim. Ordinary appraisers do not acquire additional MFA requirements.

The main sign-in page offers enrollment and code verification. Commercial directs administrators there when a second factor is needed. Enrollment only deletes abandoned **unverified** TOTP factors; verified factors cannot be removed through these endpoints. Losing a device requires an authorized operator to verify identity and reset the factor in the provider dashboard. There is no hidden bypass or universal recovery password. Provider dashboard/GitHub/Render account MFA is separate and must be managed by each account owner.

The legacy district setup and CAD onboarding write endpoints are owner-only and MFA-protected. CAD profile/schedule reads enforce the caller's district, and updates/deletions require a same-district administrator. PDF preview requires BPP access.

## Browser sessions

Refresh credentials are returned only in an HttpOnly, Secure, SameSite=Strict cookie scoped to `/api/auth` on HTTPS. Browser JSON responses do not contain refresh tokens. Access tokens remain in each tab's memory; localStorage contains identity/display metadata and a `cookie-session` marker, never token credentials. On reload, the tab exchanges the cookie before calling protected APIs. Older localStorage credentials are removed immediately and exchanged once while still in memory. Old sessions without a renewal token may need one fresh login.

Refresh and sign-out are serialized with browser Web Locks; requests within a tab share a single renewal. Without Web Locks, within-tab serialization still applies; Supabase's refresh-token reuse handling remains necessary across older browser tabs. Signing out revokes the current device’s Supabase session and clears the HttpOnly cookie and shared local identity. During a provider outage the browser credential is still cleared, but provider revocation cannot be confirmed. Cross-site sign-in/refresh/sign-out requests are rejected. Bearer API support remains for authorized non-browser clients, whose credentials must be handled securely.

## Rendering and request controls

Extracted metadata, document errors, review reasoning, candidate evidence, imported table cells and calculator names are HTML-escaped. Content Security Policy permits only same-origin scripts and hashes of the application's own inline scripts, without `unsafe-inline`/`unsafe-eval` for scripts. Print buttons bind trusted handlers rather than inline attributes so worksheet printing continues under CSP. Inline styles remain permitted for existing layout/printing; removing that exception is future work.

Responses include CSP/frame restrictions, nosniff, a referrer policy, permissions policy, and HSTS on HTTPS. Sensitive API responses are no-store. CORS has no cross-origin grants by default; any needed development origins must be explicitly configured with `CORS_ALLOWED_ORIGINS`. The app does not trust arbitrary forwarded client headers for rate limiting; the hosting proxy must provide accurate, trusted client addresses.

All API request bodies, including streamed bodies, are capped at 48 MiB before JSON parsing. Rendition PDFs are additionally limited to 24 million base64 characters (about 18 MB binary). Preview output is capped at 40 pages and 2000×2600 pixels/page and 20 million pixels across the document. Document analysis/preview jobs are capped at two simultaneous jobs per web process. Requests above these limits return a clear error; appraisers must retry or split an upload. Rate limits are per-process: 30 sign-in attempts/minute per client IP, 180 API calls/minute per bearer/IP, 15 authenticator calls/minute per bearer/IP, and 600 total API calls/minute per IP. Provider auth limits also remain in force. Multi-instance hosting will require a shared limiter.

## Demo isolation

See [demo_login.md](demo_login.md). The existing shared credentials now resolve only to a dedicated synthetic demonstration district. A strict read-operation allowlist blocks changes by the demo identity, including using old JWTs. The backend refuses any demo membership moved back to a live district. Only this restricted shared demo is exempt from personal MFA.

## Encrypted recovery exports

`python -m app.comptroller.cli sync` now performs a logical backup after its county sync work, even if sync reports a handled county failure. This reuses the existing daily Render cron service and introduces no additional cron subscription. The cron must deploy this revision; changing `render.yaml` alone does not update a manually configured service. Backup failure returns a nonzero cron exit status. Counts only are logged. After syncing, the cron replaces its process with the standalone backup command to release the sync libraries and working memory before encryption on the 512-MiB worker. The original sync failure exit status is preserved.

`python -m app.security_backup` can run a manual backup. It exports every public table discovered through PostgREST, the API schema, and every object in private source buckets. Source storage buckets unexpectedly marked public cause a failure. Exports are paginated, individually checksummed, compressed, encrypted with a fresh AES-256-GCM key, and wrapped with the deployed RSA public key. Private recovery key material is **never deployed or committed**. The private `security-backups` bucket accepts service-only uploads. 256-KiB parts avoid large-upload reliability problems; a completion descriptor is published only after every part is downloaded and checksum-verified. Seven days of completed/partial archives are retained after a new successful upload. The bucket itself is excluded to prevent recursive exports.

The initial private key is `/Users/brooksjeanette/.rendition-pilot-security/recovery-private.pem` (owner read/write only). Keep an offline copy in an authorized secure location; losing the key makes these exports unreadable. The initial local encrypted archive is in the same protected directory. Never put the private key in source control, a web response, or the backup bucket.

Recovery verification:

```
createdb -h /tmp rendition_security_recovery_20261029
python -m app.security_backup \
  --verify-file /absolute/path/archive.rpbackup \
  --private-key /absolute/path/recovery-private.pem \
  --restore-drill-db rendition_security_recovery_20261029
```

For a cloud archive, `app.security_backup.download(descriptor_name)` reconstructs and verifies its encrypted parts; decrypt only on an authorized recovery computer. `--restore-drill-db` restores every row to isolated PostgreSQL JSONB recovery tables and restores original file bytes into a recovery table. It checks counts/checksums and refuses a database name outside `rendition_security_recovery_*`. It never writes production.

**Limits:** These are paginated logical exports, not a transaction-consistent `pg_dump`, point-in-time recovery, or a full application restore. Auth passwords, WAL, provider configuration, database functions/policies and live database DDL are not exported. Preserve migration source and provider backups independently. A data recovery drill does not prove that a replacement Supabase project can run the whole application without further reconstruction. Retention/size monitoring and a full provider-backed restore drill remain operational work.

## Validation

Automated tests cover cookie rotation/CSRF/sign-out, token persistence, tenant checks, owner checks, MFA enrollment and verification, safe rendering, streaming upload caps, limits, demo write restrictions, pagination and backup encryption/tamper detection. Live checks use the demonstration district and temporary synthetic identities; no live taxpayer records are modified. Full provider backup configuration and dashboard MFA have not been certified.
