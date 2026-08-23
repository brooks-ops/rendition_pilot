"""Manual/ops entrypoint for the Comptroller closure-monitoring feature.

Examples:

  python -m app.comptroller.cli baseline --county Lubbock
  python -m app.comptroller.cli sync --county Lubbock
  python -m app.comptroller.cli month-end --month 2026-08
  python -m app.comptroller.cli month-end --month 2026-08 --dry-run
  python -m app.comptroller.cli month-end   # defaults to the previous calendar month
  python -m app.comptroller.cli export --month 2026-08
  python -m app.comptroller.cli export      # defaults to the previous calendar month, all districts
  python -m app.comptroller.cli detect-new-business --jurisdiction lubbock --dry-run
  python -m app.comptroller.cli detect-new-business --jurisdiction lubbock
  python -m app.comptroller.cli run-intelligence --jurisdiction lubbock --dry-run
  python -m app.comptroller.cli property-import --jurisdiction lubbock --file lubbock_properties.csv
  python -m app.comptroller.cli property-import --jurisdiction lubbock --file lubbock_properties.csv --dry-run
  python -m app.comptroller.cli property-enrich --jurisdiction lubbock --address "5807 88TH PL" --zip 79424
  python -m app.comptroller.cli account-card --jurisdiction lubbock --source-table bpp_intelligence_items --item-id <id>

`sync` and `baseline` both call the same underlying logic
(app.comptroller.service.sync_county), which auto-detects whether a county
already has state on file. `baseline` additionally refuses to run for a
county that already has permit rows, to make re-running it on purpose (rather
than by accident) an explicit choice.

`detect-new-business`/`run-intelligence` take `--jurisdiction <slug>`, not a
county name -- see app/comptroller/jurisdictions.py. This is the
jurisdiction-aware BPP Intelligence Engine; the sales-tax closure commands
above predate it and still take a plain county name, unchanged.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.comptroller import admin, export, intelligence, new_account_enrichment, new_business, property_adapter, property_enrichment, property_import, service
from app.comptroller.counties import get_monitored_counties
from app.comptroller.jurisdictions import JurisdictionError, get_jurisdiction_by_slug
from app.comptroller.month_end import process_month_end, resolve_target_month
from app.comptroller.new_account_enrichment import NewAccountEnrichmentError, account_card_to_dict
from app.comptroller.new_business import NewBusinessDetectionError
from app.comptroller.property_enrichment import PropertyEnrichmentError
from app.comptroller.property_import import PropertyImportError

# This CLI is meant to run standalone (e.g. as a Render Cron Job command),
# not only inside the FastAPI process -- backend/main.py loads .env at import
# time, but nothing does that for a bare `python -m app.comptroller.cli` call.
# Matches backend/main.py's exact loading order/precedence. On Render (and
# any platform that injects env vars directly into the process), these are
# no-ops since the files won't exist and existing env vars are left alone.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(_PROJECT_ROOT / "app" / ".env", override=True)


def _counties_from_args(county_arg: str | None) -> list[str]:
    if county_arg:
        return [county_arg]
    return get_monitored_counties()


def cmd_baseline(args: argparse.Namespace) -> int:
    exit_code = 0
    for county in _counties_from_args(args.county):
        existing = service.get_existing_locations_by_key(county)
        if existing:
            print(
                f"[baseline] {county}: already has {len(existing)} permit locations on file; "
                "baseline already established. Use `sync` for ongoing checks.",
                file=sys.stderr,
            )
            exit_code = 1
            continue
        try:
            result = service.sync_county(county, requested_run_type=service.RUN_TYPE_BASELINE)
        except Exception as exc:  # noqa: BLE001
            print(f"[baseline] {county}: FAILED - {exc}", file=sys.stderr)
            exit_code = 1
            continue
        print(
            f"[baseline] {county}: imported {result.permits_checked} permit locations "
            f"(run_id={result.run_id})."
        )
    return exit_code


def cmd_sync(args: argparse.Namespace) -> int:
    exit_code = 0
    for county in _counties_from_args(args.county):
        try:
            result = service.sync_county(county, requested_run_type=service.RUN_TYPE_DAILY)
        except Exception as exc:  # noqa: BLE001
            print(f"[sync] {county}: FAILED - {exc}", file=sys.stderr)
            exit_code = 1
            continue
        print(
            f"[sync] {county}: checked={result.permits_checked} "
            f"new={result.permits_new} newly_inactive={result.permits_newly_inactive} "
            f"(run_id={result.run_id}, run_type={result.run_type})"
        )
    return exit_code


def cmd_month_end(args: argparse.Namespace) -> int:
    try:
        target_month = resolve_target_month(args.month)
    except ValueError as exc:
        print(f"[month-end] {exc}", file=sys.stderr)
        return 1

    try:
        result = process_month_end(target_month, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"[month-end] {target_month:%Y-%m} FAILED - {exc}", file=sys.stderr)
        return 1

    prefix = "[month-end:dry-run]" if args.dry_run else "[month-end]"
    print(
        f"{prefix} {target_month:%Y-%m}: candidates={result.candidates_processed} "
        f"high={result.matched_high} medium={result.matched_medium} "
        f"low={result.matched_low} unmatched={result.unmatched} ambiguous={result.ambiguous}"
    )

    if args.dry_run:
        return 0

    if result.email_sent:
        print(f"[month-end] {target_month:%Y-%m}: export email sent.")
        return 0

    # The review data above is already saved regardless -- an email failure
    # is reported as a distinct, non-zero exit so it surfaces as a failed
    # cron run worth checking, without implying the month-end data itself
    # didn't process correctly.
    print(f"[month-end] {target_month:%Y-%m}: export email FAILED - {result.email_error}", file=sys.stderr)
    return 1


def cmd_export(args: argparse.Namespace) -> int:
    try:
        target_month = resolve_target_month(args.month)
    except ValueError as exc:
        print(f"[export] {exc}", file=sys.stderr)
        return 1

    month_label = f"{target_month:%Y-%m}"
    try:
        reviews = admin.list_all_reviews_for_month(month_label)
        workbook_bytes = export.build_review_queue_workbook(reviews, month_label=month_label)
    except Exception as exc:  # noqa: BLE001
        print(f"[export] {month_label} FAILED - {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else Path(f"comptroller-closures-{month_label}.xlsx")
    out_path.write_bytes(workbook_bytes)
    print(f"[export] {month_label}: wrote {len(reviews)} review row(s) to {out_path}")
    return 0


def cmd_detect_new_business(args: argparse.Namespace) -> int:
    try:
        jurisdiction = get_jurisdiction_by_slug(args.jurisdiction)
    except JurisdictionError as exc:
        print(f"[detect-new-business] {exc}", file=sys.stderr)
        return 1

    try:
        result = new_business.run_new_business_detection(
            jurisdiction.id, dry_run=args.dry_run, reevaluate=args.reevaluate,
        )
    except NewBusinessDetectionError as exc:
        print(f"[detect-new-business] {jurisdiction.slug}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[detect-new-business] {jurisdiction.slug}: FAILED - {exc}", file=sys.stderr)
        return 1

    prefix = "[detect-new-business:dry-run]" if args.dry_run else "[detect-new-business]"
    print(
        f"{prefix} {jurisdiction.name}\n"
        f"  Comptroller records evaluated: {result.evaluated}\n"
        f"  Existing account -- High confidence: {result.existing_high_confidence}\n"
        f"  Possible existing account: {result.possible_existing}\n"
        f"  No account found: {result.no_account_found}\n"
        f"  Ambiguous: {result.ambiguous}\n"
        f"  Intelligence items created: {result.items_created}\n"
        f"  Intelligence items updated: {result.items_updated}\n"
        f"  Duplicates suppressed: {result.duplicates_suppressed}"
    )
    return 0


def cmd_run_intelligence(args: argparse.Namespace) -> int:
    """Runs every intelligence module the jurisdiction has enabled. Only
    new_business_detection exists today; this dispatcher is the extension
    point for future modules (relocation, ownership change, etc.) without
    changing how the job is invoked."""

    try:
        jurisdiction = get_jurisdiction_by_slug(args.jurisdiction)
    except JurisdictionError as exc:
        print(f"[run-intelligence] {exc}", file=sys.stderr)
        return 1

    exit_code = 0
    ran_any = False

    if jurisdiction.has_capability("new_business_detection"):
        ran_any = True
        exit_code = max(exit_code, cmd_detect_new_business(argparse.Namespace(
            jurisdiction=args.jurisdiction, dry_run=args.dry_run, reevaluate=False,
        )))

    if not ran_any:
        print(f"[run-intelligence] {jurisdiction.name} has no intelligence modules enabled.", file=sys.stderr)
        return 1

    return exit_code


def cmd_property_import(args: argparse.Namespace) -> int:
    try:
        jurisdiction = get_jurisdiction_by_slug(args.jurisdiction)
    except JurisdictionError as exc:
        print(f"[property-import] {exc}", file=sys.stderr)
        return 1

    csv_path = Path(args.file)
    if not csv_path.exists():
        print(f"[property-import] {jurisdiction.slug}: file not found: {csv_path}", file=sys.stderr)
        return 1

    try:
        result = property_import.import_property_csv(
            jurisdiction, csv_path.read_text(), source_as_of_date=args.as_of, notes=args.notes, dry_run=args.dry_run,
        )
    except PropertyImportError as exc:
        print(f"[property-import] {jurisdiction.slug}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[property-import] {jurisdiction.slug}: FAILED - {exc}", file=sys.stderr)
        return 1

    prefix = "[property-import:dry-run]" if args.dry_run else "[property-import]"
    print(
        f"{prefix} {jurisdiction.name}\n"
        f"  Rows read: {result.rows_read}\n"
        f"  Rows imported: {result.rows_imported}\n"
        f"  Rows skipped (no source property id): {result.rows_skipped}\n"
        f"  Import batch id: {result.import_id or '(dry-run, not recorded)'}"
    )
    return 0


def cmd_property_enrich(args: argparse.Namespace) -> int:
    try:
        jurisdiction = get_jurisdiction_by_slug(args.jurisdiction)
    except JurisdictionError as exc:
        print(f"[property-enrich] {exc}", file=sys.stderr)
        return 1

    try:
        adapter = property_adapter.get_property_adapter(jurisdiction)
        candidates = adapter.search_properties(jurisdiction)
        outcome = property_enrichment.run_property_enrichment(
            jurisdiction,
            subject_type="AD_HOC_LOOKUP",
            subject_id=f"cli:{args.address}:{args.zip or ''}",
            input_address=args.address,
            input_zip=args.zip,
            candidates=candidates,
            dry_run=args.dry_run,
        )
    except PropertyEnrichmentError as exc:
        print(f"[property-enrich] {jurisdiction.slug}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[property-enrich] {jurisdiction.slug}: FAILED - {exc}", file=sys.stderr)
        return 1

    result = outcome.result
    matched = result.matched_property
    print(
        f"[property-enrich] {jurisdiction.name}\n"
        f"  Normalized: {result.normalized_input.normalized if result.normalized_input else ''}\n"
        f"  Candidates evaluated: {result.candidate_count}\n"
        f"  Best match: {'PropertyID ' + matched.source_property_id if matched else 'none'}"
        f"{' / R Account ' + matched.real_account_number if matched and matched.real_account_number else ''}\n"
        f"  Classification: {result.classification}\n"
        f"  Confidence: {result.confidence}\n"
        f"  Reasons: {'; '.join(result.reasons)}"
    )
    if result.alternatives:
        print(f"  Alternative candidates: {', '.join(a.source_property_id for a in result.alternatives)}")
    return 0


def cmd_account_card(args: argparse.Namespace) -> int:
    try:
        jurisdiction = get_jurisdiction_by_slug(args.jurisdiction)
    except JurisdictionError as exc:
        print(f"[account-card] {exc}", file=sys.stderr)
        return 1

    try:
        item = intelligence.get_intelligence_item(args.source_table, args.item_id)
        if item is None:
            print(f"[account-card] Item {args.item_id} was not found in {args.source_table}.", file=sys.stderr)
            return 1
        card = new_account_enrichment.generate_account_card(item, jurisdiction, dry_run=args.dry_run)
    except (NewAccountEnrichmentError, intelligence.IntelligenceQueueError) as exc:
        print(f"[account-card] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[account-card] FAILED - {exc}", file=sys.stderr)
        return 1

    data = account_card_to_dict(card)
    print(f"[account-card] {jurisdiction.name}")
    for key, value in data.items():
        if key == "appraiser":
            print(f"  Appraiser: {value['appraiser'] or 'UNASSIGNED'} ({value['basis']}) -- {value['reason']}")
        else:
            print(f"  {key}: {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.comptroller.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    baseline_parser = subparsers.add_parser(
        "baseline", help="Initial import of a county's current permit universe."
    )
    baseline_parser.add_argument("--county", default=None, help="County name (default: all monitored counties).")
    baseline_parser.set_defaults(func=cmd_baseline)

    sync_parser = subparsers.add_parser(
        "sync", help="Run the idempotent daily Comptroller sync for one or all monitored counties."
    )
    sync_parser.add_argument("--county", default=None, help="County name (default: all monitored counties).")
    sync_parser.set_defaults(func=cmd_sync)

    month_end_parser = subparsers.add_parser(
        "month-end", help="Process a calendar month's unprocessed closure events into the review queue."
    )
    month_end_parser.add_argument(
        "--month",
        default=None,
        help="Target month as YYYY-MM (default: the previous calendar month).",
    )
    month_end_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report match results without writing review-queue rows or marking events processed.",
    )
    month_end_parser.set_defaults(func=cmd_month_end)

    export_parser = subparsers.add_parser(
        "export", help="Write a month's closure review queue to an .xlsx file."
    )
    export_parser.add_argument(
        "--month",
        default=None,
        help="Target month as YYYY-MM (default: the previous calendar month).",
    )
    export_parser.add_argument(
        "--out",
        default=None,
        help="Output file path (default: comptroller-closures-<month>.xlsx in the current directory).",
    )
    export_parser.set_defaults(func=cmd_export)

    detect_parser = subparsers.add_parser(
        "detect-new-business", help="Identify newly-active Comptroller locations without a matching BPP account."
    )
    detect_parser.add_argument("--jurisdiction", required=True, help="Jurisdiction slug, e.g. 'lubbock'.")
    detect_parser.add_argument(
        "--dry-run", action="store_true",
        help="Report classification counts without writing intelligence items or marking permits evaluated.",
    )
    detect_parser.add_argument(
        "--reevaluate", action="store_true",
        help="Re-run against already-evaluated permits too (normally skipped once evaluated).",
    )
    detect_parser.set_defaults(func=cmd_detect_new_business)

    run_intelligence_parser = subparsers.add_parser(
        "run-intelligence", help="Run every intelligence module a jurisdiction has enabled."
    )
    run_intelligence_parser.add_argument("--jurisdiction", required=True, help="Jurisdiction slug, e.g. 'lubbock'.")
    run_intelligence_parser.add_argument("--dry-run", action="store_true")
    run_intelligence_parser.set_defaults(func=cmd_run_intelligence)

    property_import_parser = subparsers.add_parser(
        "property-import", help="Import a county property/CRS export CSV into the normalized property table."
    )
    property_import_parser.add_argument("--jurisdiction", required=True, help="Jurisdiction slug, e.g. 'lubbock'.")
    property_import_parser.add_argument("--file", required=True, help="Path to the property export CSV.")
    property_import_parser.add_argument("--as-of", default=None, help="Source data's as-of date, YYYY-MM-DD.")
    property_import_parser.add_argument("--notes", default=None, help="Free-text note about this import batch.")
    property_import_parser.add_argument(
        "--dry-run", action="store_true", help="Report row counts without writing any property records.",
    )
    property_import_parser.set_defaults(func=cmd_property_import)

    property_enrich_parser = subparsers.add_parser(
        "property-enrich", help="Look up the real-property record most likely associated with an address."
    )
    property_enrich_parser.add_argument("--jurisdiction", required=True, help="Jurisdiction slug, e.g. 'lubbock'.")
    property_enrich_parser.add_argument("--address", required=True, help="Address to match, e.g. '5807 88TH PL'.")
    property_enrich_parser.add_argument("--zip", default=None, help="ZIP code, if known.")
    property_enrich_parser.add_argument(
        "--dry-run", action="store_true", help="Report the match without caching it in property_enrichment_results.",
    )
    property_enrich_parser.set_defaults(func=cmd_property_enrich)

    account_card_parser = subparsers.add_parser(
        "account-card", help="Generate a New Account Enrichment card for a new-business intelligence item."
    )
    account_card_parser.add_argument("--jurisdiction", required=True, help="Jurisdiction slug, e.g. 'lubbock'.")
    account_card_parser.add_argument(
        "--source-table", default="bpp_intelligence_items",
        help="Intelligence item's source table (default: bpp_intelligence_items).",
    )
    account_card_parser.add_argument("--item-id", required=True, help="Intelligence item id.")
    account_card_parser.add_argument(
        "--dry-run", action="store_true", help="Print the card without marking it generated on the item.",
    )
    account_card_parser.set_defaults(func=cmd_account_card)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
