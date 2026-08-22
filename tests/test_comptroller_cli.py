from __future__ import annotations

from datetime import date

from app.comptroller import admin, cli, service
from app.comptroller.month_end import MonthEndResult


def test_month_end_command_reports_summary(monkeypatch, capsys):
    fake_result = MonthEndResult(
        review_month=date(2026, 8, 1),
        dry_run=False,
        candidates_processed=3,
        matched_high=1,
        matched_medium=1,
        matched_low=0,
        unmatched=1,
        review_ids=["r1", "r2", "r3"],
        email_sent=True,
    )
    monkeypatch.setattr(cli, "process_month_end", lambda month, dry_run=False: fake_result)

    exit_code = cli.main(["month-end", "--month", "2026-08"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "candidates=3" in out
    assert "high=1" in out
    assert "export email sent" in out


def test_month_end_command_invalid_month_returns_error(capsys):
    exit_code = cli.main(["month-end", "--month", "not-a-month"])
    assert exit_code == 1
    assert "not-a-month" in capsys.readouterr().err


def test_month_end_command_reports_email_failure_with_nonzero_exit(monkeypatch, capsys):
    fake_result = MonthEndResult(
        review_month=date(2026, 8, 1),
        dry_run=False,
        candidates_processed=0,
        matched_high=0,
        matched_medium=0,
        matched_low=0,
        unmatched=0,
        email_sent=False,
        email_error="SMTP_USERNAME and SMTP_PASSWORD must both be set.",
    )
    monkeypatch.setattr(cli, "process_month_end", lambda month, dry_run=False: fake_result)

    exit_code = cli.main(["month-end", "--month", "2026-08"])

    assert exit_code == 1
    assert "export email FAILED" in capsys.readouterr().err


def test_month_end_dry_run_does_not_check_email_status(monkeypatch, capsys):
    fake_result = MonthEndResult(
        review_month=date(2026, 8, 1),
        dry_run=True,
        candidates_processed=0,
        matched_high=0,
        matched_medium=0,
        matched_low=0,
        unmatched=0,
    )
    monkeypatch.setattr(cli, "process_month_end", lambda month, dry_run=False: fake_result)

    exit_code = cli.main(["month-end", "--month", "2026-08", "--dry-run"])

    assert exit_code == 0


def test_sync_command_reports_failure_with_nonzero_exit(monkeypatch, capsys):
    def fail_sync(county, requested_run_type):
        raise service.ComptrollerServiceError("boom")

    monkeypatch.setattr(service, "sync_county", fail_sync)

    exit_code = cli.main(["sync", "--county", "Lubbock"])

    assert exit_code == 1
    assert "FAILED" in capsys.readouterr().err


def test_baseline_refuses_when_already_established(monkeypatch, capsys):
    monkeypatch.setattr(service, "get_existing_locations_by_key", lambda county: {"1::1": {}})

    exit_code = cli.main(["baseline", "--county", "Lubbock"])

    assert exit_code == 1
    assert "already has" in capsys.readouterr().err


def test_export_command_writes_a_file(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        admin,
        "list_all_reviews_for_month",
        lambda month: [{"comptroller_business_name": "ACME HARDWARE"}],
    )
    out_path = tmp_path / "out.xlsx"

    exit_code = cli.main(["export", "--month", "2026-08", "--out", str(out_path)])

    assert exit_code == 0
    assert out_path.exists()
    assert out_path.stat().st_size > 0
    assert "wrote 1 review row" in capsys.readouterr().out


def test_export_command_invalid_month_returns_error(capsys):
    exit_code = cli.main(["export", "--month", "not-a-month"])
    assert exit_code == 1
    assert "not-a-month" in capsys.readouterr().err


def test_export_command_reports_failure_with_nonzero_exit(monkeypatch, capsys):
    def fail_list(month):
        raise service.ComptrollerServiceError("boom")

    monkeypatch.setattr(admin, "list_all_reviews_for_month", fail_list)

    exit_code = cli.main(["export", "--month", "2026-08"])

    assert exit_code == 1
    assert "FAILED" in capsys.readouterr().err
