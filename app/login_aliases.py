"""Public usernames backed by normal Supabase password authentication.

Aliases are identifiers, not credentials or authorization grants. Removing the
associated district membership revokes district access normally. The internal
email is an auth identifier only; reviewers do not need a mailbox or email link.
"""
DEMO_DISTRICT_ID = 'd8c8d098-a2ca-43f8-b097-bde1bb19f1d7'
DEMO_AUTH_EMAIL = 'yc-demo@login.renditionpilot.com'
LOGIN_ALIASES = {'demo': DEMO_AUTH_EMAIL, 'yc-demo': DEMO_AUTH_EMAIL}


def resolve_login_identifier(value: str) -> str:
    identifier = value.strip().lower()
    return LOGIN_ALIASES.get(identifier, identifier)
