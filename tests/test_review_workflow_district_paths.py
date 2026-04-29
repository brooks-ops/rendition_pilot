import json

import pandas as pd

from app.review_workflow import (
    append_queue_row,
    backfill_legacy_outputs,
    get_output_paths,
    save_review_outputs,
)


def _sample_result() -> dict:
    return {
        "metadata": {
            "tax_year": 2026,
            "owner_name": "Test Owner",
            "account_number": "P12345",
        },
        "assessment_summary": {
            "recommended_value": 1000,
            "value_source": "schedule_e",
            "recommended_path": "schedule_e",
            "confidence": 0.9,
            "issues": [],
        },
        "agent_review": {
            "status": "ok",
            "confidence": 0.8,
            "review_flags": [],
        },
        "form_flags": {},
        "attachments": {},
        "schedule_e": {},
    }


def _district(slug: str, district_id: str, name: str) -> dict:
    return {
        "district_id": district_id,
        "district_slug": slug,
        "district_name": name,
    }


def test_save_review_outputs_writes_into_district_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("app.review_workflow.OUTPUT_DIR", tmp_path)

    paths = save_review_outputs(
        file_name="sample.pdf",
        result=_sample_result(),
        district_context=_district("lubbock-cad", "lubbock-id", "Lubbock Central Appraisal District"),
    )

    assert paths["json"].parent == get_output_paths("lubbock-cad")["root"]
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["district"]["slug"] == "lubbock-cad"
    assert not (tmp_path / "sample_review.json").exists()


def test_append_queue_row_isolated_per_district(tmp_path, monkeypatch):
    monkeypatch.setattr("app.review_workflow.OUTPUT_DIR", tmp_path)

    append_queue_row(
        file_name="lubbock.pdf",
        result=_sample_result(),
        status="Locked",
        district_context=_district("lubbock-cad", "lubbock-id", "Lubbock Central Appraisal District"),
    )
    append_queue_row(
        file_name="dallam.pdf",
        result=_sample_result(),
        status="Locked",
        district_context=_district("dallam-cad", "dallam-id", "Dallam County Appraisal District"),
    )

    lubbock_df = pd.read_csv(get_output_paths("lubbock-cad")["queue_csv"])
    dallam_df = pd.read_csv(get_output_paths("dallam-cad")["queue_csv"])

    assert list(lubbock_df["file_name"]) == ["lubbock.pdf"]
    assert list(dallam_df["file_name"]) == ["dallam.pdf"]


def test_backfill_legacy_outputs_copies_root_files_to_lubbock_only(tmp_path, monkeypatch):
    monkeypatch.setattr("app.review_workflow.OUTPUT_DIR", tmp_path)
    legacy_root = get_output_paths(None)["root"]
    legacy_completed = get_output_paths(None)["completed"]
    legacy_completed.mkdir(parents=True, exist_ok=True)
    (legacy_root / "review_queue.csv").write_text("processed_at,file_name\n2026-01-01,sample.pdf\n", encoding="utf-8")
    (legacy_root / "sample_review.json").write_text("{}", encoding="utf-8")
    (legacy_completed / "sample_final.json").write_text("{}", encoding="utf-8")

    backfill_legacy_outputs("lubbock-cad")
    backfill_legacy_outputs("dallam-cad")

    assert get_output_paths("lubbock-cad")["queue_csv"].exists()
    assert (get_output_paths("lubbock-cad")["root"] / "sample_review.json").exists()
    assert (get_output_paths("lubbock-cad")["completed"] / "sample_final.json").exists()
    assert not get_output_paths("dallam-cad")["queue_csv"].exists()
