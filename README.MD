## BPP Project

OCR flow:

- Embedded PDF text is used first.
- If the PDF looks scanned or the text layer is weak, the pipeline now tries `Google Cloud Vision` first.
- If Google Vision is not configured or fails, the pipeline falls back to `Azure Document Intelligence`, then local `Tesseract`, then the existing OpenAI vision OCR path.

## Google Vision setup

Add this to `app/.env` or your environment:

```text
GOOGLE_VISION_API_KEY="<google-cloud-vision-api-key>"
```

Optional tuning:

```text
GOOGLE_VISION_OCR_DPI_SCALE="3.0"
GOOGLE_VISION_OCR_REQUEST_TIMEOUT_SECONDS="20"
GOOGLE_VISION_OCR_MAX_PAGES="0"
```

Notes:

- `GOOGLE_VISION_OCR_MAX_PAGES="0"` means no page limit.
- The Google Vision integration uses `DOCUMENT_TEXT_DETECTION` on rendered PDF pages.

## One-off OCR export

Run Google Vision against a PDF and save page text plus `pages.json`:

```bash
python extract_pages_ocr.py --pdf "Data/samples/sample_rendition.pdf" --out "app/page_texts_ocr"
```

Optional local fallback:

```bash
python extract_pages_ocr.py --pdf "Data/samples/sample_rendition.pdf" --provider tesseract
```

## Streamlit Deployment

The repository now includes a root [streamlit_app.py](/C:/Users/Brooks/Desktop/BPP/BPP%20Project/streamlit_app.py:1) entrypoint for Streamlit Cloud.

Set these app secrets in Streamlit:

```text
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_ANON_KEY="<supabase-anon-key>"
SUPABASE_SERVICE_ROLE_KEY="<supabase-service-role-key>"
GOOGLE_VISION_API_KEY="<google-cloud-vision-api-key>"
OPENAI_API_KEY="<openai-api-key>"
OPENAI_REVIEW_ENABLED="true"
```

Important:

- Run [supabase/migrations/20260429_multi_district_renditions.sql](/C:/Users/Brooks/Desktop/BPP/BPP%20Project/supabase/migrations/20260429_multi_district_renditions.sql:1) in the Supabase SQL editor before using `New CAD Setup`.
- The current OpenAI OCR/review path only works if the OpenAI project has active quota/billing.
- If OpenAI quota is exhausted, Google Vision still carries the OCR path and the app will continue to run.
