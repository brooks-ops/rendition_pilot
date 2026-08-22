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

- Run `supabase/migrations/20260429_multi_district_renditions.sql`, `supabase/migrations/20260430_district_user_roles.sql`, `supabase/migrations/20260430_arb_pilot_access.sql`, `supabase/migrations/20260501_cad_onboarding.sql`, `supabase/migrations/20260601_sherman_cad_access.sql`, and `supabase/migrations/20260818_comptroller_closure_monitor.sql` in the Supabase SQL editor before using New CAD Setup, ARB Pilot signup, or Comptroller closure monitoring.
- Set `APP_BASE_URL` to the public Render app URL so Supabase confirmation emails send users back to the deployed HTML app.
- In Supabase Auth settings, make sure both the project Site URL and redirect allow list include that same Render URL.
- The OpenAI OCR/review path only works if the OpenAI project has active quota/billing.
- If OpenAI quota is exhausted, Google OCR can still carry the OCR path when configured.

## Texas Comptroller Closure Monitoring

Detects Texas sales-tax permit closures in monitored counties (Lubbock in v1) as an appraisal review signal. Migrations `supabase/migrations/20260818_comptroller_closure_monitor.sql` and `supabase/migrations/20260821_comptroller_match_schema_correction.sql` have been applied to production and the daily/month-end Render Cron Jobs are active as of 2026-08-21. Get the monthly closure export from the website (sign in as a district admin -> Admin tab -> Comptroller Closures -> pick a month -> Download Excel). See [docs/comptroller_closure_monitor.md](docs/comptroller_closure_monitor.md) for the full design, data source, scheduler setup, manual commands, and the live-schema validation findings (RenditionPilot's real Supabase project has no `accounts` table; matching runs against `parsed_rendition_results` on owner name only). Optional configuration:

```text
COMPTROLLER_MONITORED_COUNTIES="Lubbock"
COMPTROLLER_APP_TOKEN="<optional-socrata-app-token>"
COMPTROLLER_MATCH_TABLE="parsed_rendition_results"
COMPTROLLER_MATCH_OWNER_NAME_PATH="result->metadata->>owner_name"
COMPTROLLER_MATCH_ACCOUNT_NUMBER_PATH="result->metadata->>account_number"

# Optional, currently unused: automatic monthly export email (comptroller-month-end
# cron job only). Not configured as of 2026-08-21 -- the website download is used instead.
SMTP_USERNAME="<sending mailbox>"
SMTP_PASSWORD="<app password>"
SMTP_HOST="smtp.office365.com"
SMTP_PORT="587"
COMPTROLLER_EXPORT_EMAIL_TO="bbarrett@lubbockcad.org"
```

## BPP Intelligence Queue + New Business Detection

Generalized review-item model (`bpp_intelligence_items`) plus a jurisdiction abstraction (`jurisdictions`, `app/comptroller/jurisdictions.py`, `app/comptroller/cad_adapter.py`) so future detection modules aren't Lubbock-specific. First module: **New Business Detection** -- identifies newly-active Comptroller locations without a matching RenditionPilot account. Reuses the existing Comptroller sync and matcher; does not modify the sales-tax closure monitor above. Run manually first (no cron enabled for this feature yet):

```bash
python -m app.comptroller.cli detect-new-business --jurisdiction lubbock --dry-run
python -m app.comptroller.cli detect-new-business --jurisdiction lubbock
```

View results at the website's **BPP Intelligence** tab (district admin). Requires `supabase/migrations/20260822_bpp_intelligence_queue.sql` applied. See [docs/bpp_intelligence_queue.md](docs/bpp_intelligence_queue.md) for the full design, why the closure monitor wasn't migrated onto the new table, and known limitations.
