from __future__ import annotations

import smtplib

import pytest

from app.comptroller import emailer


@pytest.fixture(autouse=True)
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_USERNAME", "renditionpilot@lubbockcad.org")
    monkeypatch.setenv("SMTP_PASSWORD", "fake-app-password")


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args = None
        self.sent_message = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent_message = message


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(emailer.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_missing_credentials_raises_config_error(monkeypatch):
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    with pytest.raises(emailer.EmailConfigError):
        emailer.send_month_end_export_email("2026-08", b"fake-xlsx-bytes")


def test_send_uses_office365_defaults_and_configured_recipient(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.delenv("COMPTROLLER_EXPORT_EMAIL_TO", raising=False)

    emailer.send_month_end_export_email("2026-08", b"fake-xlsx-bytes", review_count=3)

    smtp = FakeSMTP.instances[0]
    assert smtp.host == emailer.DEFAULT_SMTP_HOST
    assert smtp.port == emailer.DEFAULT_SMTP_PORT
    assert smtp.starttls_called is True
    assert smtp.login_args == ("renditionpilot@lubbockcad.org", "fake-app-password")
    assert smtp.sent_message["To"] == emailer.DEFAULT_RECIPIENT
    assert "2026-08" in smtp.sent_message["Subject"]


def test_send_respects_env_overrides(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_FROM", "reports@lubbockcad.org")
    monkeypatch.setenv("COMPTROLLER_EXPORT_EMAIL_TO", "someone-else@lubbockcad.org")

    emailer.send_month_end_export_email("2026-08", b"fake-xlsx-bytes")

    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.org"
    assert smtp.port == 2525
    assert smtp.sent_message["From"] == "reports@lubbockcad.org"
    assert smtp.sent_message["To"] == "someone-else@lubbockcad.org"


def test_explicit_recipient_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv("COMPTROLLER_EXPORT_EMAIL_TO", "env-default@lubbockcad.org")

    emailer.send_month_end_export_email("2026-08", b"fake-xlsx-bytes", recipient="explicit@lubbockcad.org")

    assert FakeSMTP.instances[0].sent_message["To"] == "explicit@lubbockcad.org"


def test_attachment_is_the_xlsx_bytes():
    emailer.send_month_end_export_email("2026-08", b"the-actual-xlsx-bytes")

    message = FakeSMTP.instances[0].sent_message
    attachments = [part for part in message.iter_attachments()]
    assert len(attachments) == 1
    assert attachments[0].get_payload(decode=True) == b"the-actual-xlsx-bytes"
    assert attachments[0].get_filename() == "comptroller-closures-2026-08.xlsx"


def test_smtp_failure_raises_delivery_error(monkeypatch):
    class FailingSMTP(FakeSMTP):
        def login(self, username, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(emailer.smtplib, "SMTP", FailingSMTP)

    with pytest.raises(emailer.EmailDeliveryError):
        emailer.send_month_end_export_email("2026-08", b"fake-xlsx-bytes")
