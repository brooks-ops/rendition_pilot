## BPP Project

FastAPI serves the browser app from `frontend/index.html` and exposes the API endpoints used by that page. Streamlit is not the deployment target.

## Render Deployment

Create a Render Web Service from this repo.

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

The deployed app opens at the Render service URL. The HTML uses the same origin for API calls, so no separate frontend service is required.

## Required Environment Variables

Set these in Render's Environment tab:

```text
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_ANON_KEY="<supabase-anon-key>"
SUPABASE_SERVICE_ROLE_KEY="<supabase-service-role-key>"
APP_BASE_URL="https://your-render-service.onrender.com"
OPENAI_API_KEY="<openai-api-key>"
OPENAI_VISION_OCR_MODEL="gpt-4.1-mini"
GOOGLE_VISION_API_KEY="<google-cloud-vision-api-key>"
GOOGLE_DOCUMENT_AI_PROJECT_ID="<google-project-id>"
GOOGLE_DOCUMENT_AI_LOCATION="us"
GOOGLE_DOCUMENT_AI_PROCESSOR_ID="<processor-id>"
GOOGLE_DOCUMENT_AI_PROCESSOR_NAME="projects/<project-id>/locations/us/processors/<processor-id>"
GOOGLE_DOCUMENT_AI_API_KEY="<google-document-ai-api-key>"
OPENAI_REVIEW_ENABLED="true"
```

Optional tuning:

```text
GOOGLE_VISION_OCR_DPI_SCALE="3.0"
GOOGLE_VISION_OCR_REQUEST_TIMEOUT_SECONDS="20"
GOOGLE_VISION_OCR_MAX_PAGES="0"
OPENAI_VISION_OCR_TIMEOUT_SECONDS="12"
OPENAI_VISION_OCR_MAX_PAGES="8"
OPENAI_REVIEW_TIMEOUT_SECONDS="8"
```

## OCR Flow

- Embedded PDF text is used first.
- If the PDF looks scanned or the text layer is weak, the pipeline tries Google Document AI and Google Cloud Vision.
- If Google OCR is unavailable, the pipeline can fall back to Azure Document Intelligence, local Tesseract, and OpenAI vision OCR depending on configured credentials.

## One-Off OCR Export

Run Google Vision against a PDF and save page text plus `pages.json`:

```bash
python extract_pages_ocr.py --pdf "Data/samples/sample_rendition.pdf" --out "app/page_texts_ocr"
```

Optional local fallback:

```bash
python extract_pages_ocr.py --pdf "Data/samples/sample_rendition.pdf" --provider tesseract
```

## Supabase Setup

- Run `supabase/migrations/20260429_multi_district_renditions.sql`, `supabase/migrations/20260430_district_user_roles.sql`, and `supabase/migrations/20260430_arb_pilot_access.sql` in the Supabase SQL editor before using New CAD Setup or ARB Pilot signup.
- Set `APP_BASE_URL` to the public Render app URL so Supabase confirmation emails send users back to the deployed HTML app.
- In Supabase Auth settings, make sure both the project Site URL and redirect allow list include that same Render URL.
- The OpenAI OCR/review path only works if the OpenAI project has active quota/billing.
- If OpenAI quota is exhausted, Google OCR can still carry the OCR path when configured.
