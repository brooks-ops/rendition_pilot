from __future__ import annotations

import re
from typing import Any


class CandidateExtractor:
    def __init__(self) -> None:
        self.money_pattern = re.compile(
            r"""
            (?<!\d)
            \$?
            (?:
                \d{1,3}(?:,\d{3})+(?:\.\d{2})?
                |
                \d{2,}(?:\.\d{2})?
            )
            (?!\d)
            """,
            re.VERBOSE,
        )

        self.label_rules: list[tuple[str, list[str], int]] = [
            ("schedule_e_total", ["schedule e", "total market value", "market value", "inventory"], 12),
            ("rendered_value", ["rendered value", "total rendered value", "value rendered"], 11),
            ("good_faith_value", ["good faith", "good faith estimate"], 10),
            ("market_value", ["market value", "appraised value"], 9),
            ("historical_cost", ["original cost", "historical cost", "reported cost", "purchase cost", "cost new", "cost"], 8),
            ("attachment_total", ["attachment total", "summary total", "schedule total", "grand total", "total cost", "total value"], 8),
            ("total_value", ["total", "value"], 5),
        ]

        self.bad_context_terms = [
            "phone",
            "fax",
            "page",
            "date",
            "year",
            "zip",
            "account number",
            "parcel",
            "statement",
            "form",
            "section 5",
            "20,000 or more",
            "125,000",
        ]

    def extract_candidates(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        for page in pages:
            text = page.get("text", "") or ""
            page_number = page.get("page_number", 1)

            page_type = self._classify_page(text)

            for match in self.money_pattern.finditer(text):
                raw_value = match.group(0)
                value = self._clean_number(raw_value)

                if value is None:
                    continue
                if value < 100:
                    continue

                context = self._get_context_by_span(text, match.start(), match.end(), window=90)
                label = self._guess_label(context)
                score = self._score_candidate(
                    value=value,
                    label=label,
                    context=context,
                    page_type=page_type,
                    page_number=page_number,
                )

                if score <= 0:
                    continue

                candidates.append(
                    {
                        "field": self._field_from_label(label),
                        "value": value,
                        "raw_value": raw_value,
                        "label": label,
                        "source": "candidate_extractor",
                        "rule": f"context_label:{label}",
                        "page_number": page_number,
                        "page_type": page_type,
                        "score": round(score, 2),
                        "confidence": round(min(max(score / 20, 0.05), 0.95), 3),
                        "context": context.strip(),
                        "evidence_text": context.strip(),
                    }
                )

            for match in re.finditer(r"\b(19[5-9]\d|20[0-4]\d)\b", text):
                context = self._get_context_by_span(text, match.start(), match.end(), window=80)
                normalized_context = self._normalize_text(context)
                if not any(term in normalized_context for term in ["year acquired", "acquired", "purchase year", "acquisition year"]):
                    continue
                candidates.append(
                    {
                        "field": "acquisition_year",
                        "value": int(match.group(1)),
                        "raw_value": match.group(1),
                        "label": "acquisition_year",
                        "source": "candidate_extractor",
                        "rule": "year_near_acquisition_label",
                        "page_number": page_number,
                        "page_type": page_type,
                        "score": 15.0,
                        "confidence": 0.75,
                        "context": context.strip(),
                        "evidence_text": context.strip(),
                    }
                )

        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        return candidates

    def select_best_candidate(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        return max(candidates, key=lambda x: x.get("score", 0))

    def _classify_page(self, text: str) -> str:
        normalized = self._normalize_text(text)

        if "schedule e" in normalized or "machinery and equipment" in normalized:
            return "schedule_e"

        if "see attached" in normalized or "attachment" in normalized or "summary" in normalized:
            return "attachment"

        if "rendition" in normalized or "affirmation" in normalized or "section 5" in normalized:
            return "cover_page"

        return "unknown"

    def _clean_number(self, text: str) -> float | None:
        try:
            raw = text.replace("$", "").strip()
            if re.fullmatch(r"\d{1,3}[.,]\d{3}", raw):
                cleaned = raw.replace(",", "").replace(".", "")
            else:
                cleaned = raw.replace(",", "")
            return float(cleaned)
        except Exception:
            return None

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").lower()).strip()

    def _guess_label(self, context: str) -> str:
        normalized = self._normalize_text(context)

        for label, phrases, _base_score in self.label_rules:
            for phrase in phrases:
                if phrase in normalized:
                    return label

        return "unknown"

    def _field_from_label(self, label: str) -> str:
        if label in {"schedule_e_total", "attachment_total", "total_value"}:
            return "attachment_total"
        if label == "market_value":
            return "good_faith_value"
        return label

    def _score_candidate(
        self,
        value: float,
        label: str,
        context: str,
        page_type: str,
        page_number: int,
    ) -> float:
        normalized = self._normalize_text(context)
        score = 0.0

        # Base score by label
        for rule_label, _phrases, base_score in self.label_rules:
            if label == rule_label:
                score += base_score
                break

        # Page-type boosts
        if page_type == "schedule_e" and label in {"schedule_e_total", "market_value", "total_value"}:
            score += 6

        if page_type == "attachment" and label in {"attachment_total", "rendered_value", "total_value"}:
            score += 5

        if page_type == "cover_page" and label in {"good_faith_value", "rendered_value"}:
            score += 4

        # Keyword boosts
        keyword_boosts = [
            ("total market value", 5),
            ("rendered value", 5),
            ("good faith", 5),
            ("original cost", 4),
            ("historical cost", 4),
            ("market value", 3),
            ("total", 2),
            ("value", 1),
        ]

        for phrase, pts in keyword_boosts:
            if phrase in normalized:
                score += pts

        # Penalize obvious junk contexts
        for bad_term in self.bad_context_terms:
            if bad_term in normalized:
                score -= 6

        # Penalize suspicious small values
        if value < 100:
            score -= 12
        elif value < 500:
            score -= 4

        # Mild preference for first pages, but not enough to overpower labels
        if page_number == 1:
            score += 1.5

        # Penalize years and common form amounts
        if self._looks_like_year(value):
            score -= 10

        if value in {20000.0, 125000.0} and ("or more" in normalized or "section 5" in normalized):
            score -= 15

        # Strong boost if number appears near target phrases
        if self._phrase_near_value(normalized, ["schedule e", "rendered value", "good faith", "market value", "original cost"]):
            score += 4

        return score

    def _looks_like_year(self, value: float) -> bool:
        if value.is_integer():
            year = int(value)
            return 1900 <= year <= 2100
        return False

    def _phrase_near_value(self, context: str, phrases: list[str]) -> bool:
        return any(phrase in context for phrase in phrases)

    def _get_context_by_span(self, text: str, start_idx: int, end_idx: int, window: int = 75) -> str:
        start = max(start_idx - window, 0)
        end = min(end_idx + window, len(text))
        return text[start:end]
