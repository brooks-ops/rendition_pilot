import re


def parse_money_text(raw: str) -> float | None:
    text = (
        str(raw or "")
        .replace("$", "")
        .replace("O", "0")
        .replace("o", "0")
        .strip("() ")
    )
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\b(\d)\s+(\d{2,3},\d{3}(?:\.\d{1,2})?)\b", r"\1\2", text)
    text = text.replace(" ", "")

    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", text):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")

    try:
        return float(text)
    except ValueError:
        return None


class TargetedRenditionParser:
    def normalize_text(self, text: str) -> str:
        if not text:
            return ""

        text = text.upper()

        replacements = {
            "AFIACIIED": "ATTACHED",
            "SCHIDULE": "SCHEDULE",
            "FUMILURE": "FURNITURE",
            "EQUIPMENL": "EQUIPMENT",
            "EQULPMENT": "EQUIPMENT",
            "MACHLNERY": "MACHINERY",
            "VALUO": "VALUE",
            "N,LA.KEL": "MARKET",
            "SECTTON": "SECTION",
            "ATRMALRON": "AFFIRMATION",
            "PROPORTY": "PROPERTY",
            "TAXABL6": "TAXABLE",
            "PROP€RTY": "PROPERTY",
            "AJSINESS": "BUSINESS",
            "FUMITURE": "FURNITURE",
            "L\\RACHINERY": "MACHINERY",
            "RENDITLON": "RENDITION",
            "RENDILION": "RENDITION",
        }

        for bad, good in replacements.items():
            text = text.replace(bad, good)

        text = re.sub(r"[^\S\r\n]+", " ", text)
        return text

    def parse_page_1_flags(self, text: str) -> dict:
        normalized = self.normalize_text(text)

        result = {
            "see_attached": False,
            "section_5_present": False,
            "section_5_over_20k_detected": False,
            "section_5_125k_language_detected": False,
            "signature_block_detected": False,
        }

        if "SEE ATTACHED" in normalized or "SEE ATTAC" in normalized:
            result["see_attached"] = True

        if "SECTION 5" in normalized:
            result["section_5_present"] = True

        if (
            "$20,000" in normalized
            or "20,000 OR MORE" in normalized
            or "20OOOO" in normalized
            or "UNDER$2OOOO" in normalized
            or "20,000" in normalized
        ):
            result["section_5_over_20k_detected"] = True

        if (
            "$125,000 OR LESS" in normalized
            or "125,000 OR LESS" in normalized
            or "125 OOO OR LESS" in normalized
            or "125000 OR LESS" in normalized
            or "125 OOO OR" in normalized
            or "125,000" in normalized
        ):
            result["section_5_125k_language_detected"] = True

        signature_clues = [
            "SECTION 6",
            "AUTHORIZED INDIVIDUAL",
            "SUBSCRIBED AND SWORN",
            "PRINTED NAME",
            "DATE",
            "SIGN",
        ]

        clue_count = sum(1 for clue in signature_clues if clue in normalized)
        if clue_count >= 2:
            result["signature_block_detected"] = True

        return result

    def parse_schedule_e_total(self, text: str) -> dict:
        normalized = self.normalize_text(text)

        result = {
            "schedule_e_present": False,
            "machinery_and_equipment_present": False,
            "schedule_e_total": None,
        }

        if "SCHEDULE E" in normalized:
            result["schedule_e_present"] = True

        if (
            "MACHINERY AND EQUIPMENT" in normalized
            or "MACHINERY EQUIPMENT" in normalized
        ):
            result["machinery_and_equipment_present"] = True

        total_match = re.search(
            r"TOTAL(?:\s+[A-Z ]+)?[: ]+.*?(\$?\s*\d(?:\s+)?\d{1,3}[,]\d{3}(?:\.\d{1,2})?|\$?\s*\d{1,3}[,]\d{3}(?:\.\d{1,2})?)",
            normalized,
        )
        if total_match:
            amount = parse_money_text(total_match.group(1))
            if amount is not None:
                result["schedule_e_total"] = amount
                return result

        matches = re.findall(r"\b\d{1,3},\d{3}(?:\.\d{1,2})?\b", normalized)
        candidates = []
        for m in matches:
            value = parse_money_text(m)
            if value is not None:
                candidates.append(value)

        if candidates:
            result["schedule_e_total"] = max(candidates)

        return result

    def parse_schedule_e_year_rows_from_words(self, words: list[dict]) -> list[dict]:
        rows = []

        year_words = []
        for w in words:
            text = w["text"]
            if re.fullmatch(r"20\d{2}", text):
                year = int(text)
                if 2015 <= year <= 2025 and 30 <= w["x0"] <= 80 and 410 <= w["top"] <= 560:
                    year_words.append(w)

        year_words = sorted(year_words, key=lambda w: w["top"])

        for yw in year_words:
            year = int(yw["text"])
            row_top = yw["top"]

            candidate_words = []
            for w in words:
                if abs(w["top"] - row_top) <= 6 and w["x0"] > 80:
                    cleaned = w["text"].replace(",", "").replace(".", "")

                    if not cleaned.isdigit():
                        continue

                    try:
                        val = int(cleaned)
                    except ValueError:
                        continue

                    if val < 5000 or val == year:
                        continue

                    if 2400 <= val <= 2600:
                        continue

                    candidate_words.append((val, w))

            if candidate_words:
                best_val, best_word = max(candidate_words, key=lambda x: x[0])

                rows.append({
                    "year_acquired": year,
                    "amount": float(best_val),
                    "source_section": "Schedule E",
                    "amount_word": best_word["text"],
                    "amount_x0": best_word["x0"],
                    "row_top": row_top,
                })

        deduped = {}
        for row in rows:
            deduped[row["year_acquired"]] = row

        final_rows = list(deduped.values())
        final_rows.sort(key=lambda x: x["year_acquired"], reverse=True)

        return final_rows

    def parse_attachment_summary(self, texts: list[str]) -> dict:
        """
        Looks across attachment/support pages for summary-style value signals.
        Version 1: totals and class detection only.
        """
        combined = "\n".join([self.normalize_text(t) for t in texts if t])

        result = {
            "attachment_summary_present": False,
            "machinery_and_equipment_present": False,
            "reported_cost_detected": False,
            "current_value_detected": False,
            "rendered_value_detected": False,
            "attachment_total_candidates": [],
            "best_attachment_total": None,
        }

        if not combined:
            return result

        summary_clues = [
            "SUMMARY",
            "STATE CLASS",
            "REPORTED COST",
            "CURRENT VALUE",
            "RENDERED VALUE",
        ]
        if sum(1 for clue in summary_clues if clue in combined) >= 2:
            result["attachment_summary_present"] = True

        if "MACHINERY AND EQUIPMENT" in combined or "MACHINERY EQUIPMENT" in combined:
            result["machinery_and_equipment_present"] = True

        if "REPORTED COST" in combined:
            result["reported_cost_detected"] = True

        if "CURRENT VALUE" in combined:
            result["current_value_detected"] = True

        if "RENDERED VALUE" in combined:
            result["rendered_value_detected"] = True

        money_matches = re.findall(
            r"\$?\s*(?:\d\s+)?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b",
            combined,
        )

        candidates = []
        for m in money_matches:
            value = parse_money_text(m)
            if value is None:
                continue
            if value in {20000.0, 50000.0, 125000.0, 150000.0}:
                continue
            candidates.append(value)

        total_patterns = [
            r"TOTAL\s+FIXED\s+ASSETS\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?)",
            r"GRAND\s+TOTAL\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?)",
            r"TOTAL\s+ASSETS\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?)",
            r"TOTAL\s+MARKET\s+VALUE\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?)",
        ]
        labeled_totals = []
        for pattern in total_patterns:
            for match in re.finditer(pattern, combined):
                value = parse_money_text(match.group(1))
                if value is not None:
                    labeled_totals.append(value)

        if candidates:
            result["attachment_total_candidates"] = sorted(set(candidates), reverse=True)

        if labeled_totals:
            result["attachment_summary_present"] = True
            result["attachment_total_candidates"] = sorted(
                set(result["attachment_total_candidates"] + labeled_totals),
                reverse=True,
            )
            result["best_attachment_total"] = max(labeled_totals)
        elif result["attachment_summary_present"] and candidates:
            result["best_attachment_total"] = max(candidates)

        return result
