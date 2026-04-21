from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.cli import OverrideSelection, build_cli_summary, prompt_override_selection
from app.pipeline import run_rendition_pipeline
from dotenv import load_dotenv
load_dotenv()

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "Data" / "samples"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="Run BPP rendition parsing + valuation review with clean CLI output.",
    )

    parser.add_argument(
        "input_path",
        nargs="?",
        default=None,
        help="Optional PDF path. If omitted, newest PDF in Data/samples is used.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for override mode in terminal.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print raw JSON output after the summary.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Process all PDFs in Data/samples.",
    )

    return parser.parse_args()


def get_input_file(input_path: str | None) -> Path:
    if input_path:
        resolved = Path(input_path)
        if not resolved.exists():
            raise FileNotFoundError(f"Input file not found: {resolved}")
        return resolved

    pdfs = list(DEFAULT_INPUT_DIR.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDFs found in {DEFAULT_INPUT_DIR}")

    latest = max(pdfs, key=lambda f: f.stat().st_mtime)
    print(f"\nAuto-loading latest file:\n{latest}\n")
    return latest


def get_override_selection(interactive: bool) -> OverrideSelection:
    if interactive:
        return prompt_override_selection()
    return OverrideSelection(mode="auto", manual_override=None)


def get_locked_value(result: dict) -> str:
    assessment_summary = result.get("assessment_summary", {}) or {}

    value = (
        assessment_summary.get("recommended_value")
        or assessment_summary.get("recommended_market_value")
        or assessment_summary.get("recommended_assessed_value")
        or assessment_summary.get("extracted_value")
    )

    if value is None:
        return "-"

    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def prompt_next_action() -> str:
    print("\nWhat would you like to do next?\n")
    print("1) Accept value and finish")
    print("2) Rerun with different override")
    print("3) View raw JSON output")
    print("4) Exit")

    while True:
        choice = input("\nEnter choice (1-4): ").strip()
        if choice in {"1", "2", "3", "4"}:
            return choice
        print("Choose 1, 2, 3, or 4.")


def run_batch_mode() -> None:
    pdfs = list(DEFAULT_INPUT_DIR.glob("*.pdf"))

    if not pdfs:
        print(f"No PDFs found in {DEFAULT_INPUT_DIR}")
        return

    print(f"\nProcessing {len(pdfs)} files...\n")

    for pdf in pdfs:
        print("=" * 60)
        print(f"FILE: {pdf.name}")

        result = run_rendition_pipeline(str(pdf))
        assessment = result.get("assessment_summary", {}) or {}

        value = (
            assessment.get("recommended_value")
            or assessment.get("recommended_market_value")
            or assessment.get("recommended_assessed_value")
            or assessment.get("extracted_value")
        )

        if value is not None:
            try:
                value_str = f"${float(value):,.2f}"
            except (TypeError, ValueError):
                value_str = str(value)
        else:
            value_str = "-"

        path = (
            assessment.get("recommended_path")
            or assessment.get("value_source")
            or "-"
        )

        issues = assessment.get("issues", []) or []
        issues_str = " | ".join(issues) if issues else "-"

        print(f"VALUE: {value_str}")
        print(f"PATH:  {path}")
        print(f"ISSUES: {issues_str}")
        print()

def main() -> None:
    args = parse_args()

    # 🔥 ADD THIS RIGHT HERE
    if args.batch:
        run_batch_mode()
        return

    # normal single-file flow continues below
    input_file = get_input_file(args.input_path)

    print("Using runner: app.pipeline.run_rendition_pipeline")

    while True:
        override_selection = get_override_selection(args.interactive)

        result = run_rendition_pipeline(
            pdf_path=str(input_file),
            manual_override=override_selection.manual_override,
        )

        print()
        print(build_cli_summary(result=result, source_path=str(input_file)))

        if args.json and not args.interactive:
            print("\nRAW JSON")
            print("-" * 78)
            print(json.dumps(result, indent=2, default=str))
            return

        if not args.interactive:
            return

        next_action = prompt_next_action()

        if next_action == "1":
            print(f"\nFinal Value Locked: {get_locked_value(result)}")
            return

        if next_action == "2":
            print("\nRerunning with same file and a new override selection...\n")
            continue

        if next_action == "3":
            print("\nRAW JSON")
            print("-" * 78)
            print(json.dumps(result, indent=2, default=str))
            continue

        if next_action == "4":
            print("\nExited without locking a final value.")
            return


if __name__ == "__main__":
    main()