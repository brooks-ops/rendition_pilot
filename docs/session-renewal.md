# Rendition Pilot session renewal

Login, signup, and district setup retain the Supabase refresh token and expiry
metadata, alongside the existing access token in the same-origin browser session.
`/api/auth/refresh` rotates tokens through Supabase's supported refresh grant and
rechecks current district membership and workspace permissions. Responses are
no-store; credentials are not logged or written to server files. JWT lifetimes,
password rules, and provider session policies are unchanged.

BPP, Commercial, Super Admin, and onboarding use `frontend/auth-session.js`. It
renews within two minutes of expiry, on visible-tab checks, returning focus or
connectivity, and before authenticated requests. Web Locks serialize refreshes
across tabs, with an in-flight promise for concurrent calls within one tab.
Requests receive the latest token in both the Authorization header and JSON
access_token field. A 401 is retried once after renewal; network/5xx/permission
errors never cause automatic replay of a submitted write. A renewal outage
preserves the stored session and on-screen work. Sign-out during renewal cannot
restore the old session. Switching accounts in another tab blocks old-tab saves.

Refresh updates BPP's auth state without resetting the current worksheet or
view. Batch ownership uses appraiser identity rather than token text, so token
rotation does not invalidate a batch or its print confirmation. Binary exports
and PDF downloads use the same renewal-aware fetch.

Older sessions did not store any refresh token. Those users must sign in once
after deployment to obtain a renewable session. No frontend code can recover a
refresh token the old login response discarded. A genuine revoked/expired
refresh credential still requires sign-in. Temporary restore failures do not
erase credentials; rejected credentials show a readable sign-in message.

Reference: [Supabase sessions and token rotation](https://supabase.com/docs/guides/auth/sessions).

Checks: `node --test tests/frontend/auth-session.test.cjs` covers renewal,
concurrent/cross-tab rotation, stale requests, failed refresh, logout races,
account switching, legacy sessions, and binary responses.
`pytest tests/test_auth_refresh.py` covers renewal metadata, no-store responses,
provider errors, and removed district membership.
