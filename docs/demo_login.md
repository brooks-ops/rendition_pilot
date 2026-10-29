# Read-only demonstration login

`demo` and `yc-demo` still authenticate using the dedicated existing Supabase login. Its password is unchanged.

The account belongs only to `Rendition Pilot Demo — Synthetic Data` (`d8c8d098-a2ca-43f8-b097-bde1bb19f1d7`). The BPP queue contains three labeled fictional businesses. No live Lubbock or Dallam data or commercial files are copied. The server resolves current membership for every request, so older JWTs cannot reconnect to Lubbock.

HTTP middleware uses an explicit allowlist of read operations for the shared demo identity. Claims, completions, card saves, imports, administrative changes, ARB access and password changes are blocked. The admin presentation role permits showing district screens but does not enable writes. This specific read-only account does not require a personal authenticator. Normal district administrators and the owner require MFA.

Never move this membership back to a live district. Removing the membership disables the login's district access. The Super Admin claim is not granted to this account.
