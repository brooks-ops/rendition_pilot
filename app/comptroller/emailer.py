"""SMTP delivery of the monthly Comptroller closure export.

RenditionPilot has no general email-sending capability anywhere else in the
codebase (confirmed: the only outbound email is Supabase Auth's built-in
signup/reset emails via GoTrue, which is unrelated). This is a small,
self-contained SMTP client using only the standard library (`smtplib` +
`email`) -- no new third-party dependency -- built specifically to attach and
send the monthly closure export. Defaults to Microsoft 365's SMTP relay
since that's what this deployment uses; override via env vars for a
different provider.

Named `emailer.py`, not `email.py`, to avoid any ambiguity with the stdlib
`email` package this module itself imports from.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

DEFAULT_SMTP_HOST = "smtp.office365.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_RECIPIENT = "bbarrett@lubbockcad.org"
XLSX_CONTENT_TYPE = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class EmailConfigError(RuntimeError):
    pass


class EmailDeliveryError(RuntimeError):
    pass


def _smtp_config() -> dict[str, str | int]:
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    if not username or not password:
        raise EmailConfigError(
            "SMTP_USERNAME and SMTP_PASSWORD must both be set to send the monthly export email."
        )
    return {
        "host": os.getenv("SMTP_HOST", DEFAULT_SMTP_HOST),
        "port": int(os.getenv("SMTP_PORT", str(DEFAULT_SMTP_PORT))),
        "username": username,
        "password": password,
        "from_address": os.getenv("SMTP_FROM", username),
    }


def build_month_end_export_email(
    month_label: str,
    xlsx_bytes: bytes,
    *,
    from_address: str,
    to_address: str,
    review_count: int,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"RenditionPilot Comptroller Closures - {month_label}"
    message["From"] = from_address
    message["To"] = to_address
    message.set_content(
        "Attached is this month's Texas Comptroller sales-tax closure review queue "
        f"for {month_label} ({review_count} record(s)).\n\n"
        "Each row is a possible business/location closure signal, not confirmed "
        "fact -- see the Match Confidence, Match Reason, and Workflow Status "
        "columns before treating any of these as a closed account."
    )
    message.add_attachment(
        xlsx_bytes,
        maintype="application",
        subtype=XLSX_CONTENT_TYPE,
        filename=f"comptroller-closures-{month_label}.xlsx",
    )
    return message


def send_month_end_export_email(
    month_label: str,
    xlsx_bytes: bytes,
    *,
    recipient: str | None = None,
    review_count: int = 0,
) -> None:
    """Raises EmailConfigError (missing credentials) or EmailDeliveryError
    (SMTP connection/auth/send failure) -- callers decide whether that should
    fail the overall job or just be logged (month_end.py treats it as
    best-effort: the review data is already saved regardless of whether the
    notification email goes out)."""

    config = _smtp_config()
    to_address = recipient or os.getenv("COMPTROLLER_EXPORT_EMAIL_TO", DEFAULT_RECIPIENT)
    message = build_month_end_export_email(
        month_label,
        xlsx_bytes,
        from_address=str(config["from_address"]),
        to_address=to_address,
        review_count=review_count,
    )

    try:
        with smtplib.SMTP(str(config["host"]), int(config["port"]), timeout=30) as smtp:
            smtp.starttls()
            smtp.login(str(config["username"]), str(config["password"]))
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailDeliveryError(f"Failed to send the closure export email to {to_address}: {exc}") from exc
