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
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        text = text.replace(".", "")
    elif text.count(".") > 1:
        text = text.replace(".", "")

    try:
        return float(text)
    except ValueError:
        return None


class TargetedRenditionParser:
    @staticmethod
    def _word_text(word: dict) -> str:
        return str(word.get("text", "") or "").strip()

    @staticmethod
    def _word_top(word: dict) -> float:
        return float(word.get("top", word.get("y0", 0)) or 0)

    @staticmethod
    def _word_x0(word: dict) -> float:
        return float(word.get("x0", 0) or 0)

    def _money_word_value(self, word: dict) -> float | None:
        text = self._word_text(word)
        if not text:
            return None

        if not re.fullmatch(r"[$]?\d[\d,.\s]{2,}", text):
            return None

        value = parse_money_text(text)
        if value is None:
            return None
        if value < 5000:
            return None
        if 1900 <= int(value) <= 2100 and float(value).is_integer():
            return None
        return value

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
        flat_text = re.sub(r"\s+", " ", normalized)

        result = {
            "see_attached": False,
            "section_3_present": False,
            "section_3_prior_year_checked": False,
            "section_5_present": False,
            "section_5_over_20k_detected": False,
            "section_5_125k_language_detected": False,
            "section_5_under_20k_checked": False,
            "section_5_20k_or_more_checked": False,
            "section_5_125k_or_less_checked": False,
            "section_5_more_than_125k_checked": False,
            "signature_block_detected": False,
        }

        if "SEE ATTACHED" in normalized or "SEE ATTAC" in normalized:
            result["see_attached"] = True

        if "SECTION 3" in normalized:
            result["section_3_present"] = True

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

        section_3_block = self._extract_section_block(flat_text, "SECTION 3", "SECTION 4")
        section_5_block = self._extract_section_block(flat_text, "SECTION 5", "SECTION 6")

        result["section_3_prior_year_checked"] = self._matches_checked_pattern(
            section_3_block,
            [r"[☑✓✔☒🗹X\?]\s*BY CHECKING THIS BOX"],
        )
        result["section_5_under_20k_checked"] = self._matches_checked_pattern(
            section_5_block,
            [r"[☑✓✔☒🗹X\?]\s*UNDER\s*\$?\s*20,000"],
        )
        result["section_5_20k_or_more_checked"] = self._matches_checked_pattern(
            section_5_block,
            [r"[☑✓✔☒🗹X\?]\s*\$?\s*20,000\s+OR\s+MORE"],
        )
        result["section_5_125k_or_less_checked"] = self._matches_checked_pattern(
            section_5_block,
            [r"[☑✓✔☒🗹X\?]\s*\$?\s*125,000\s+OR\s+LESS"],
        )
        result["section_5_more_than_125k_checked"] = self._matches_checked_pattern(
            section_5_block,
            [r"[☑✓✔☒🗹X\?]\s*MORE\s+THAN\s+\$?\s*125,000"],
        )

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

    def _extract_section_block(self, text: str, section_start: str, next_section: str) -> str:
        start = text.find(section_start)
        if start < 0:
            return ""
        end = text.find(next_section, start)
        if end < 0:
            end = len(text)
        return text[start:end]

    def _matches_checked_pattern(self, section_text: str, patterns: list[str]) -> bool:
        if not section_text:
            return False
        return any(re.search(pattern, section_text) for pattern in patterns)

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

        if not result["schedule_e_present"]:
            return result

        labeled_candidates = []
        total_patterns = [
            r"TOTAL(?:\s+[A-Z ]+)?[: ]+.*?(\$?\s*\d(?:\s+)?\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?|\$?\s*\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?)",
            r"GRAND\s+TOTAL(?:\s+[A-Z ]+)?[: ]+.*?(\$?\s*\d(?:\s+)?\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?|\$?\s*\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?)",
        ]
        for pattern in total_patterns:
            for match in re.finditer(pattern, normalized):
                amount = parse_money_text(match.group(1))
                if amount is not None and amount not in {20000.0, 50000.0, 125000.0, 150000.0}:
                    labeled_candidates.append(amount)

        if labeled_candidates:
            result["schedule_e_total"] = max(labeled_candidates)
            return result

        matches = re.findall(r"\b\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?\b", normalized)
        candidates = []
        for m in matches:
            value = parse_money_text(m)
            if value is not None and value not in {20000.0, 50000.0, 125000.0, 150000.0}:
                candidates.append(value)

        if candidates:
            result["schedule_e_total"] = max(candidates)

        return result

    def parse_schedule_e_year_rows_from_words(self, words: list[dict]) -> list[dict]:
        if not words:
            return []

        year_words: list[dict] = []
        money_words: list[dict] = []
        for word in words:
            text = self._word_text(word)
            if re.fullmatch(r"20\d{2}", text):
                year = int(text)
                if 1900 <= year <= 2100:
                    year_words.append(word)
                    continue

            money_value = self._money_word_value(word)
            if money_value is not None:
                money_words.append({**word, "_money_value": money_value})

        if not year_words or not money_words:
            return []

        min_year_top = min(self._word_top(word) for word in year_words)
        total_row_candidates = [
            self._word_top(word)
            for word in words
            if "TOTAL" in self._word_text(word).upper() and self._word_top(word) > min_year_top + 40
        ]
        total_row_top = min(total_row_candidates) if total_row_candidates else None

        filtered_year_words = [
            word for word in year_words
            if total_row_top is None or self._word_top(word) < total_row_top - 10
        ]
        filtered_money_words = [
            word for word in money_words
            if total_row_top is None or self._word_top(word) < total_row_top - 10
        ]

        if not filtered_year_words or not filtered_money_words:
            return []

        row_clusters: list[dict] = []
        row_tolerance = 8.0
        for word in sorted(filtered_year_words, key=lambda item: self._word_top(item)):
            top = self._word_top(word)
            for cluster in row_clusters:
                if abs(top - cluster["top"]) <= row_tolerance:
                    cluster["words"].append(word)
                    cluster["top"] = (cluster["top"] * (len(cluster["words"]) - 1) + top) / len(cluster["words"])
                    break
            else:
                row_clusters.append({"top": top, "words": [word]})

        rows = []
        for cluster in row_clusters:
            row_top = float(cluster["top"])
            row_years = sorted(cluster["words"], key=lambda item: self._word_x0(item))
            row_amounts = [
                word for word in filtered_money_words
                if abs(self._word_top(word) - row_top) <= row_tolerance
            ]
            if not row_amounts:
                continue

            used_year_keys: set[tuple[int, int]] = set()
            for amount_word in sorted(row_amounts, key=lambda item: self._word_x0(item)):
                amount_x0 = self._word_x0(amount_word)
                amount_value = float(amount_word["_money_value"])

                best_year_word = None
                best_distance = None
                for year_word in row_years:
                    year_x0 = self._word_x0(year_word)
                    distance = abs(year_x0 - amount_x0)
                    if distance > 220:
                        continue
                    if best_distance is None or distance < best_distance:
                        best_distance = distance
                        best_year_word = year_word

                if best_year_word is None:
                    continue

                year = int(self._word_text(best_year_word))
                year_key = (year, round(self._word_x0(best_year_word)))
                if year_key in used_year_keys:
                    continue
                used_year_keys.add(year_key)

                rows.append({
                    "year_acquired": year,
                    "amount": amount_value,
                    "source_section": "Schedule E",
                    "amount_word": self._word_text(amount_word),
                    "amount_x0": amount_x0,
                    "year_x0": self._word_x0(best_year_word),
                    "row_top": row_top,
                })

        deduped: dict[tuple[int, int], dict] = {}
        for row in rows:
            key = (int(row["year_acquired"]), int(round(float(row["amount"]))))
            existing = deduped.get(key)
            if existing is None or float(row["amount_x0"]) > float(existing["amount_x0"]):
                deduped[key] = row

        final_rows = sorted(
            deduped.values(),
            key=lambda item: (int(item["year_acquired"]), float(item["amount"])),
            reverse=True,
        )

        return final_rows

    def parse_attachment_summary(self, texts: list[str]) -> dict:
        """
        Looks across attachment/support pages for summary-style value signals.
        Version 1: totals and class detection only.
        """
        normalized_pages = [self.normalize_text(t) for t in texts if t]
        combined = "\n".join(normalized_pages)

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

        money_pattern = r"\$?\s*(?:\d\s+)?\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?\b"
        threshold_values = {20000.0, 50000.0, 125000.0, 150000.0}
        page_totals: list[float] = []
        rendered_total_candidates: list[float] = []

        for page_text in normalized_pages:
            page_summary_clues = [
                "SUMMARY",
                "STATE CLASS",
                "REPORTED COST",
                "CURRENT VALUE",
                "RENDERED VALUE",
            ]
            page_is_summary = sum(1 for clue in page_summary_clues if clue in page_text) >= 2
            lines = [line.strip() for line in page_text.splitlines() if line.strip()]

            if page_is_summary:
                triples: list[tuple[float, float, float]] = []
                sequential_values: list[float] = []
                for line in lines:
                    values = []
                    for match_text in re.findall(money_pattern, line):
                        value = parse_money_text(match_text)
                        if value is None or value in threshold_values:
                            continue
                        values.append(value)
                    if len(values) >= 3:
                        triples.append((values[-3], values[-2], values[-1]))
                    elif len(values) == 1 and re.fullmatch(rf"{money_pattern}", line):
                        sequential_values.append(values[0])

                if len(sequential_values) >= 3:
                    usable_count = len(sequential_values) - (len(sequential_values) % 3)
                    for idx in range(0, usable_count, 3):
                        triple = sequential_values[idx: idx + 3]
                        if len(triple) == 3:
                            triples.append((triple[0], triple[1], triple[2]))

                if triples:
                    best_triple = max(triples, key=lambda item: (item[2], item[1], item[0]))
                    page_totals.extend(best_triple)
                    rendered_total_candidates.append(best_triple[2])

            total_patterns = [
                r"TOTAL\s+FIXED\s+ASSETS\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?)",
                r"GRAND\s+TOTALS?\s*:?\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?)",
                r"TOTALS?\s*:?\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?)",
                r"TOTAL\s+ASSETS\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?)",
                r"TOTAL\s+MARKET\s+VALUE\s*(\$?\s*(?:\d\s+)?\d{1,3}(?:[,.]\d{3})+(?:\.\d{1,2})?)",
            ]
            for pattern in total_patterns:
                for match in re.finditer(pattern, page_text):
                    value = parse_money_text(match.group(1))
                    if value is not None and value not in threshold_values:
                        page_totals.append(value)

        if page_totals:
            result["attachment_total_candidates"] = sorted(set(page_totals), reverse=True)

        if rendered_total_candidates:
            result["attachment_summary_present"] = True
            result["best_attachment_total"] = max(rendered_total_candidates)
        elif result["attachment_summary_present"] and page_totals:
            result["best_attachment_total"] = max(page_totals)

        return result
