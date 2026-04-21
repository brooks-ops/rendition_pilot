import pandas as pd


class CategoryClassifier:
    def __init__(self, mapping_path: str):
        self.df = pd.read_csv(mapping_path)

    def get_life_years(self, description: str | None) -> tuple[int | None, str]:
        if not description:
            return None, "No description"

        desc = description.lower()

        for _, row in self.df.iterrows():
            keyword = row["keyword"]
            if keyword in desc:
                return int(row["life_years"]), "High confidence"

        return None, "No match"