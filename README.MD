from dataclasses import dataclass

@dataclass
class AssetRow:
    description: str
    acquisition_year: int | None
    original_cost: float | None
    matched_category: str | None = None
    depreciation_percent: float | None = None
    assessed_value: float | None = None
    confidence: str = "Needs review"
    notes: str = ""

## Streamlit deployment secrets

Set these in Streamlit app secrets for scanned/handwritten PDF OCR:

```text
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT="https://<resource-name>.cognitiveservices.azure.com"
AZURE_DOCUMENT_INTELLIGENCE_KEY="<azure-key>"
```

Optional tuning:

```text
AZURE_DOCUMENT_INTELLIGENCE_TIMEOUT_SECONDS="18"
AZURE_DOCUMENT_INTELLIGENCE_REQUEST_TIMEOUT_SECONDS="8"
OPENAI_API_KEY="<openai-key>"
OPENAI_REVIEW_ENABLED="false"
```

The app uses embedded PDF text first. Azure OCR only runs for scanned or low-text PDFs, and failed OCR exits quickly to manual review.
