# Y Combinator demo username

The public usernames `demo` and `yc-demo` resolve to the dedicated Supabase auth identifier `yc-demo@login.renditionpilot.com`. The identifier is internal; no mailbox or email confirmation is needed. The password is generated separately and is never stored in source code. All sign-ins still use Supabase password authentication and the normal district membership checks.

The dedicated account has the owner's district admin role, so it sees the same BPP, Commercial, Admin, Readiness, and activity screens, with live district data and normal admin actions. This is not a read-only sandbox. Its `must_change_password` flag is false so reviewers can use the supplied credentials without taking over the shared review password. Other invited users retain their existing first-login password change requirement.

The district admin can revoke its district access through Admin → Users by removing `yc-demo@login.renditionpilot.com`. Keeping the username mapping in place does not grant access after membership removal. No existing user's password or membership is changed by this setup.
