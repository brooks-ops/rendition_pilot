"""Excel export of a month's Comptroller closure review queue.

One sheet, one row per `comptroller_closure_reviews` row, covering exactly
what the review record already exposes: Comptroller evidence, the match
result (with its transparent reason string), and the workflow status an
appraiser has (or hasn't yet) set on it. Never touches appraisal/BPP data --
this is a read-only view of the review queue.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pandas as pd
from openpyxl.styles import Font

# (source key on comptroller_closure_reviews, column header)
COLUMNS: list[tuple[str, str]] = [
    ("review_month", "Review Month"),
    ("comptroller_business_name", "Business / DBA"),
    ("comptroller_legal_name", "Legal / Taxpayer Name"),
    ("comptroller_taxpayer_id", "Comptroller Taxpayer ID"),
    ("comptroller_location_number", "Comptroller Location #"),
    ("comptroller_address", "Address"),
    ("comptroller_city", "City"),
    ("comptroller_state", "State"),
    ("comptroller_zip", "ZIP"),
    ("comptroller_permit_start_date", "Permit Start Date"),
    ("comptroller_permit_end_date", "Permit End Date"),
    ("comptroller_previous_status", "Previous Status"),
    ("comptroller_current_status", "Current Status"),
    ("first_detected_at", "First Detected By RenditionPilot"),
    ("matched_account_number", "Matched RenditionPilot Account #"),
    ("matched_owner_name", "Matched Owner Name"),
    ("match_confidence", "Match Confidence"),
    ("match_score", "Match Score"),
    ("match_ambiguous", "Ambiguous Match?"),
    ("match_reason", "Match Reason"),
    ("workflow_status", "Workflow Status"),
    ("reviewer_notes", "Reviewer Notes"),
    ("reviewed_at", "Reviewed At"),
]

SHEET_NAME_MAX_LEN = 31  # Excel's own limit


def build_review_queue_workbook(reviews: list[dict[str, Any]], *, month_label: str) -> bytes:
    rows = [{label: review.get(key) for key, label in COLUMNS} for review in reviews]
    frame = pd.DataFrame(rows, columns=[label for _, label in COLUMNS])

    sheet_name = f"Closures {month_label}"[:SHEET_NAME_MAX_LEN]
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name=sheet_name)
        worksheet = writer.sheets[sheet_name]
        for col_idx, (_, label) in enumerate(COLUMNS, start=1):
            column_letter = worksheet.cell(row=1, column=col_idx).column_letter
            worksheet.column_dimensions[column_letter].width = max(14, min(40, len(label) + 4))
        for cell in worksheet[1]:
            cell.font = Font(bold=True)

    return buffer.getvalue()
