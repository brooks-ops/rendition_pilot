from __future__ import annotations

from datetime import date

from app.comptroller import admin, cli, service
from app.comptroller.jurisdictions import Jurisdiction
from app.comptroller.month_end import MonthEndResult
from app.comptroller.property_import import PropertyImportError, PropertyImportResult


def _make_jurisdiction(**overrides) -> Jurisdiction:
    defaults = dict(
        id="jur-lubbock", district_id="district-lubbock", name="Lubbock Central Appraisal District",
        slug="lubbock", county_name="Lubbock", state="TX", timezone="America/Chicago", active=True,
        comptroller_county_code="152", comptroller_dataset_id="3kx8-uryv",
        capabilities={"real_property_linkage": True}, cad_field_mapping={},
        property_field_mapping={"source_property_id": "PropertyID", "situs_address": "SitusAddress"},
    )
    defaults.update(overrides)
    return Jurisdiction(**defaults)


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


def test_property_import_command_reports_summary(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "get_jurisdiction_by_slug", lambda slug: _make_jurisdiction())
    monkeypatch.setattr(
        cli.property_import,
        "import_property_csv",
        lambda jurisdiction, text, **kw: PropertyImportResult(
            jurisdiction_id=jurisdiction.id, rows_read=5, rows_imported=4, rows_skipped=1, import_id="imp-1",
        ),
    )
    csv_path = tmp_path / "props.csv"
    csv_path.write_text("PropertyID,SitusAddress\n1,100 Main St\n")

    exit_code = cli.main(["property-import", "--jurisdiction", "lubbock", "--file", str(csv_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Rows imported: 4" in out
    assert "Rows skipped" in out


def test_property_import_command_missing_file_returns_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_jurisdiction_by_slug", lambda slug: _make_jurisdiction())
    exit_code = cli.main(["property-import", "--jurisdiction", "lubbock", "--file", "/no/such/file.csv"])
    assert exit_code == 1
    assert "not found" in capsys.readouterr().err


def test_property_import_command_reports_capability_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "get_jurisdiction_by_slug", lambda slug: _make_jurisdiction())

    def fail(jurisdiction, text, **kw):
        raise PropertyImportError("Real Property Linkage cannot run.")

    monkeypatch.setattr(cli.property_import, "import_property_csv", fail)
    csv_path = tmp_path / "props.csv"
    csv_path.write_text("PropertyID,SitusAddress\n1,100 Main St\n")

    exit_code = cli.main(["property-import", "--jurisdiction", "lubbock", "--file", str(csv_path)])

    assert exit_code == 1
    assert "cannot run" in capsys.readouterr().err


def test_property_enrich_command_reports_match(monkeypatch, capsys):
    from app.comptroller.address_normalizer import normalize_address
    from app.comptroller.property_adapter import NormalizedRealProperty
    from app.comptroller.property_matching import PropertyMatchResult
    from app.comptroller.property_enrichment import PropertyEnrichmentOutcome

    jurisdiction = _make_jurisdiction()
    monkeypatch.setattr(cli, "get_jurisdiction_by_slug", lambda slug: jurisdiction)
    monkeypatch.setattr(cli.property_adapter, "get_property_adapter", lambda j: type("A", (), {"search_properties": staticmethod(lambda j2: [])})())

    matched = NormalizedRealProperty(
        property_id="row-1", jurisdiction_id="jur-lubbock", source_property_id="813538",
        real_account_number="R163313", situs_address_raw="5807 88TH PL", situs_address_normalized="5807 88TH PLACE",
        situs_city=None, situs_state=None, situs_zip="79424", owner_name=None, tug=None, neighborhood=None,
        map_id=None, latitude=None, longitude=None, source_system=None, source_import_id=None, source_updated_at=None,
    )
    outcome = PropertyEnrichmentOutcome(
        result=PropertyMatchResult(
            classification="EXACT_PROPERTY_MATCH", confidence="HIGH", score=1.0, matched_property=matched,
            candidate_count=1, reasons=["street number matched"], signals={},
            normalized_input=normalize_address("5807 88th Pl", zip_code="79424"),
        ),
        from_cache=False, stored_row_id="er-1",
    )
    monkeypatch.setattr(cli.property_enrichment, "run_property_enrichment", lambda *a, **kw: outcome)

    exit_code = cli.main(["property-enrich", "--jurisdiction", "lubbock", "--address", "5807 88TH PL", "--zip", "79424"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "PropertyID 813538" in out
    assert "R Account R163313" in out
    assert "EXACT_PROPERTY_MATCH" in out
