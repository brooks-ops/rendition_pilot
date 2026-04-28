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
    # Only repair the common OCR split for leading "1" values like "1 84,724.43".
    # Broad digit-merge rules create nonsense like "6 45,442" -> "645,442".
    text = re.sub(r"\b1\s+(\d{2,3},\d{3}(?:\.\d{1,2})?)\b", r"1\1", text)
    # If OCR leaves a stray leading digit before a valid amount, prefer the valid amount.
    text = re.sub(r"\b[2-9]\s+(\d{2,3},\d{3}(?:\.\d{1,2})?)\b", r"\1", text)
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
    SCHEDULE_E_SUBSECTIONS = {
        (0, 0): "furniture_fixtures",
        (1, 0): "machinery_equipment",
        (2, 0): "office_equipment",
        (0, 1): "computer_equipment",
        (1, 1): "pos_servers_mainframes",
        (2, 1): "other",
    }

    @staticmethod
    def _word_text(word: dict) -> str:
        return str(word.get("text", "") or "").strip()

    @staticmethod
    def _word_top(word: dict) -> float:
        return float(word.get("top", word.get("y0", 0)) or 0)

    @staticmethod
    def _word_x0(word: dict) -> float:
        return float(word.get("x0", 0) or 0)

    def _schedule_e_y_split(self, words: list[dict]) -> float:
        valid_words = [word for word in words if self._word_text(word)]
        tops = sorted(self._word_top(word) for word in valid_words)
        if not tops:
            return 0.0

        total_row_tops = sorted(
            {
                self._word_top(word)
                for word in valid_words
                if "TOTAL" in self._word_text(word).upper()
            }
        )
        non_total_tops = sorted(
            {
                self._word_top(word)
                for word in valid_words
                if "TOTAL" not in self._word_text(word).upper()
            }
        )
        for total_top in total_row_tops:
            later_tops = [top for top in non_total_tops if top > total_top + 20.0]
            if later_tops:
                return (total_top + later_tops[0]) / 2.0

        clustered_tops: list[float] = []
        for top in tops:
            if not clustered_tops or abs(top - clustered_tops[-1]) > 12.0:
                clustered_tops.append(top)

        if len(clustered_tops) < 2:
            return (min(tops) + max(tops)) / 2.0

        gaps = [
            (clustered_tops[idx + 1] - clustered_tops[idx], idx)
            for idx in range(len(clustered_tops) - 1)
        ]
        largest_gap, gap_idx = max(gaps, key=lambda item: item[0])
        if largest_gap < 40.0:
            return (min(tops) + max(tops)) / 2.0

        return (clustered_tops[gap_idx] + clustered_tops[gap_idx + 1]) / 2.0

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

    def parse_schedule_e_subsection_rows(self, words: list[dict]) -> list[dict]:
        if not words:
            return []

        valid_words = [word for word in words if self._word_text(word)]
        if not valid_words:
            return []

        min_x = min(self._word_x0(word) for word in valid_words)
        max_x = max(self._word_x0(word) for word in valid_words)
        width = max(max_x - min_x, 1.0)
        x_band = width / 3.0
        y_split = self._schedule_e_y_split(valid_words)

        rows: list[dict] = []
        clusters: list[dict] = []
        for word in sorted(valid_words, key=lambda item: self._word_top(item)):
            top = self._word_top(word)
            for cluster in clusters:
                if abs(top - cluster["top"]) <= 8.0:
                    cluster["words"].append(word)
                    cluster["top"] = (cluster["top"] * (len(cluster["words"]) - 1) + top) / len(cluster["words"])
                    break
            else:
                clusters.append({"top": top, "words": [word]})

        for cluster in clusters:
            ordered = sorted(cluster["words"], key=lambda item: self._word_x0(item))
            segments: list[list[dict]] = []
            segment_gap_threshold = max(160.0, x_band * 0.6)
            for word in ordered:
                if not segments:
                    segments.append([word])
                    continue
                prev_word = segments[-1][-1]
                if self._word_x0(word) - self._word_x0(prev_word) > segment_gap_threshold:
                    segments.append([word])
                else:
                    segments[-1].append(word)

            for segment in segments:
                row_text = " ".join(self._word_text(word) for word in segment).strip()
                upper_row = row_text.upper()
                if not row_text or "TOTAL" in upper_row:
                    continue

                year_words = [word for word in segment if re.fullmatch(r"20\d{2}", self._word_text(word))]
                money_words: list[dict] = []
                for word in segment:
                    value = self._money_word_value(word)
                    if value is None:
                        text_value = parse_money_text(self._word_text(word))
                        if text_value is None or text_value < 1:
                            continue
                        if float(text_value).is_integer() and 1900 <= int(text_value) <= 2100:
                            continue
                        value = text_value
                    money_words.append({**word, "_money_value": value})

                if not year_words and not money_words:
                    continue

                anchor_word = year_words[0] if year_words else (money_words[0] if money_words else segment[0])
                anchor_x = self._word_x0(anchor_word)
                x_index = min(2, max(0, int((anchor_x - min_x) / x_band)))
                y_index = 0 if float(cluster["top"]) <= y_split else 1
                subsection = self.SCHEDULE_E_SUBSECTIONS.get((x_index, y_index))
                if subsection is None:
                    continue

                year_acquired = None
                if year_words:
                    try:
                        year_acquired = int(self._word_text(year_words[0]))
                    except ValueError:
                        year_acquired = None

                historical_cost = None
                good_faith_value = None
                if year_acquired is not None:
                    if money_words:
                        historical_cost = float(money_words[0]["_money_value"])
                    if len(money_words) >= 2:
                        good_faith_value = float(money_words[-1]["_money_value"])
                elif money_words:
                    good_faith_value = float(money_words[-1]["_money_value"])

                if historical_cost is None and good_faith_value is None:
                    continue

                rows.append(
                    {
                        "subsection": subsection,
                        "year_acquired": year_acquired,
                        "historical_cost": historical_cost,
                        "good_faith_value": good_faith_value,
                        "raw_text": row_text,
                        "raw_values": {
                            "money_tokens": [self._word_text(word) for word in money_words],
                            "year_tokens": [self._word_text(word) for word in year_words],
                            "region": {"x_index": x_index, "y_index": y_index},
                            "anchor_x": anchor_x,
                        },
                        "confidence": 0.86 if year_acquired is not None else 0.74,
                        "flags": [],
                    }
                )

        deduped: dict[tuple[str, int | None, float | None, float | None, str], dict] = {}
        for row in rows:
            key = (
                str(row.get("subsection")),
                row.get("year_acquired"),
                row.get("historical_cost"),
                row.get("good_faith_value"),
                str(row.get("raw_text", "")).upper(),
            )
            existing = deduped.get(key)
            if existing is None or float(row.get("confidence") or 0) > float(existing.get("confidence") or 0):
                deduped[key] = row

        return list(deduped.values())

    def parse_schedule_e_subsection_totals(self, words: list[dict]) -> dict[str, float]:
        if not words:
            return {}

        valid_words = [word for word in words if self._word_text(word)]
        if not valid_words:
            return {}

        min_x = min(self._word_x0(word) for word in valid_words)
        max_x = max(self._word_x0(word) for word in valid_words)
        min_top = min(self._word_top(word) for word in valid_words)
        max_top = max(self._word_top(word) for word in valid_words)
        width = max(max_x - min_x, 1.0)
        x_band = width / 3.0
        y_split = self._schedule_e_y_split(valid_words)

        totals: dict[str, float] = {}
        for word in valid_words:
            if "TOTAL" not in self._word_text(word).upper():
                continue

            row_top = self._word_top(word)
            x_index = min(2, max(0, int((self._word_x0(word) - min_x) / x_band)))
            y_index = 0 if row_top <= y_split else 1
            subsection = self.SCHEDULE_E_SUBSECTIONS.get((x_index, y_index))
            if subsection is None:
                continue

            column_left = min_x + (x_index * x_band) - 25.0
            column_right = min_x + ((x_index + 1) * x_band) + 25.0

            row_words = [
                candidate
                for candidate in valid_words
                if abs(self._word_top(candidate) - row_top) <= 10.0
                and column_left <= self._word_x0(candidate) <= column_right
            ]
            if not row_words:
                continue

            money_candidates: list[float] = []
            for row_word in row_words:
                value = parse_money_text(self._word_text(row_word))
                if value is None or value < 1:
                    continue
                if float(value).is_integer() and 1900 <= int(value) <= 2100:
                    continue
                money_candidates.append(float(value))

            if money_candidates:
                totals[subsection] = max(money_candidates)

        return totals

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
