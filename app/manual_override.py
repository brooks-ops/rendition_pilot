import json
import os


class ManualOverrideLoader:
    def load_override(self, override_path: str) -> dict:
        if not os.path.exists(override_path):
            return {
                "attachment_total": None,
                "good_faith_value": None,
                "historical_cost": None,
                "acquisition_year": None,
                "life_years": None,
                "notes": "",
            }

        with open(override_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return {
            "attachment_total": data.get("attachment_total"),
            "good_faith_value": data.get("good_faith_value"),
            "historical_cost": data.get("historical_cost"),
            "acquisition_year": data.get("acquisition_year"),
            "life_years": data.get("life_years"),
            "notes": data.get("notes", ""),
        }