from __future__ import annotations


class AssessmentSummaryBuilder:
    def build_summary(
        self,
        rendition_result: dict,
        manual_override: dict | None = None,
        depreciated_override_result: dict | None = None,
    ) -> dict:
        form_flags = rendition_result.get("form_flags", {}) or {}
        schedule_e = rendition_result.get("schedule_e", {}) or {}
        attachments = rendition_result.get("attachments", {}) or {}
        review_flags = rendition_result.get("review_flags", {}) or {}
        ocr_reconciliation = rendition_result.get("ocr_reconciliation", {}) or {}

        manual_override = manual_override or {}
        depreciated_override_result = depreciated_override_result or {}

        attachment_total_override = manual_override.get("attachment_total")
        good_faith_value_override = manual_override.get("good_faith_value")
        historical_cost_override = manual_override.get("historical_cost")
        acquisition_year = manual_override.get("acquisition_year")
        life_years = manual_override.get("life_years")
        override_notes = manual_override.get("notes", "") or ""

        schedule_e_total = schedule_e.get("total")
        attachment_total = attachments.get("best_attachment_total")
        rendered_value = (
            rendition_result.get("rendered_value")
            or (rendition_result.get("resolved_values", {}) or {}).get("rendered_value")
        )
        schedule_rule_value = rendition_result.get("recommended_value")
        recommended_value_source = rendition_result.get("recommended_value_source")
        good_faith_value = (
            rendition_result.get("good_faith_value")
            or (rendition_result.get("resolved_values", {}) or {}).get("good_faith_value")
            or (rendition_result.get("schedule_values", {}) or {}).get("good_faith_total")
        )
        depreciated_value = depreciated_override_result.get("depreciated_value")
        percent_good = depreciated_override_result.get("percent_good")
        valuation_flags = list(rendition_result.get("valuation_flags", []) or [])

        extracted_value = None
        value_source = None
        recommended_path = "manual_review"

        # ------------------------------------------------------------
        # LOCKED PRIORITY ORDER
        # 1) Manual attachment total override
        # 2) Manual good faith override
        # 3) Manual historical cost less depreciation
        # 4) Deterministic schedule rule engine
        # 5) Extracted rendered value
        # 6) Extracted attachment total
        # 7) Extracted Schedule E total
        # 8) Extracted good faith value
        # 9) Manual review
        # ------------------------------------------------------------

        if attachment_total_override is not None:
            extracted_value = attachment_total_override
            value_source = "manual_override_attachment_total"
            recommended_path = "use_manual_attachment_total"

        elif good_faith_value_override is not None:
            extracted_value = good_faith_value_override
            value_source = "manual_override_good_faith_value"
            recommended_path = "use_manual_good_faith_value"

        elif depreciated_value is not None:
            extracted_value = depreciated_value
            value_source = "manual_override_historical_cost_depreciated"
            recommended_path = "use_manual_historical_cost_depreciated"

        elif schedule_rule_value is not None:
            extracted_value = schedule_rule_value
            value_source = recommended_value_source or "schedule_rule_engine"
            recommended_path = "use_schedule_rule_engine"

        elif rendered_value is not None:
            extracted_value = rendered_value
            value_source = "rendered_value"
            recommended_path = "use_rendered_value_pending_review"

        elif attachment_total is not None:
            extracted_value = attachment_total
            value_source = "attachment_summary_total"
            recommended_path = "use_attachment_total_pending_review"

        elif schedule_e_total is not None:
            extracted_value = schedule_e_total
            value_source = "schedule_e_total"
            recommended_path = "use_schedule_total_pending_review"

        elif good_faith_value is not None:
            extracted_value = good_faith_value
            value_source = "schedule_good_faith_value"
            recommended_path = "use_good_faith_value_pending_review"

        issues: list[str] = []

        if review_flags.get("needs_manual_row_review"):
            issues.append("Schedule E row-level values were not extractable.")

        if review_flags.get("needs_attachment_review"):
            issues.append("Attachment pages require manual review.")

        if review_flags.get("ocr_unavailable"):
            issues.append("OCR engine unavailable for scanned-image PDF.")

        if review_flags.get("provider_disagreement"):
            issues.append("OCR providers disagreed on one or more valuation-critical totals.")

        for ocr_error in review_flags.get("ocr_errors", []) or []:
            issues.append(str(ocr_error))

        for valuation_flag in valuation_flags:
            issues.append(str(valuation_flag))

        if not form_flags.get("signature_block_detected"):
            issues.append("Signature block not detected.")

        if not form_flags.get("section_5_present"):
            issues.append("Section 5 was not clearly detected.")

        # Helpful context, but not a blocker by itself
        if (
            historical_cost_override is not None
            and (acquisition_year is None or life_years is None)
        ):
            issues.append("Historical cost override is missing acquisition year or life years.")

        # Build a clean reason string that matches the chosen path
        if recommended_path == "use_manual_attachment_total":
            reason = "Manual attachment total override applied."

        elif recommended_path == "use_manual_good_faith_value":
            reason = "Manual good faith value override applied."

        elif recommended_path == "use_manual_historical_cost_depreciated":
            if percent_good is not None:
                reason = (
                    f"Historical cost override depreciated using LCAD schedule "
                    f"at {percent_good:.2%} percent good."
                )
            else:
                reason = "Historical cost override applied using LCAD depreciation schedule."

        elif recommended_path == "use_schedule_rule_engine":
            reason = "Schedule-specific rule engine calculated the recommended value from Schedules A-E."

        elif recommended_path == "use_attachment_total_pending_review":
            reason = "Attachment total selected as the best available extracted value."
            if review_flags.get("provider_agreement"):
                reason = "Attachment total selected and cross-checked across OCR providers."

        elif recommended_path == "use_rendered_value_pending_review":
            reason = "Rendered value selected as the best available extracted value."
            if review_flags.get("provider_agreement"):
                reason = "Rendered value selected and cross-checked across OCR providers."

        elif recommended_path == "use_schedule_total_pending_review":
            reason = "Schedule E total selected as the best available extracted value."
            if review_flags.get("provider_agreement"):
                reason = "Schedule E total selected and cross-checked across OCR providers."

        elif recommended_path == "use_good_faith_value_pending_review":
            reason = "Good faith estimate values were summed and selected as the best available extracted value."

        else:
            reason = "No reliable value source was found. Manual review required."

        confidence = "low"
        if recommended_path in {
            "use_manual_attachment_total",
            "use_manual_good_faith_value",
            "use_manual_historical_cost_depreciated",
        }:
            confidence = "high"
        elif recommended_path == "use_schedule_rule_engine":
            confidence = rendition_result.get("rendition_valuation", {}).get("confidence", "medium")
        elif recommended_path in {
            "use_rendered_value_pending_review",
            "use_attachment_total_pending_review",
            "use_schedule_total_pending_review",
            "use_good_faith_value_pending_review",
        } and not issues:
            confidence = "medium"
            if review_flags.get("provider_agreement"):
                confidence = "high"

        return {
            "extracted_value": extracted_value,
            "recommended_value": extracted_value,
            "value_source": value_source,
            "recommended_path": recommended_path,
            "reason": reason,
            "issues": issues,
            "confidence": confidence,
            "override_notes": override_notes,
            "ocr_provider_used": review_flags.get("ocr_provider_used"),
            "ocr_secondary_providers": review_flags.get("ocr_secondary_providers", []),
            "provider_agreement_fields": review_flags.get("provider_agreement_fields", []),
            "ocr_reconciliation": ocr_reconciliation,
            "valuation_flags": valuation_flags,
        }
