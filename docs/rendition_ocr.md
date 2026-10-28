# Rendition review OCR

Web review uses embedded PDF text when it is readable. If any page has weak or missing text, fast web review runs local Tesseract; fast mode skips cloud OCR requests, never local OCR. The fast option is passed per request rather than changing a process-wide environment variable.

The Docker runtime installs Tesseract and English language data. Build checks confirm both. `/health` reports `ocr.available`, `ocr.english`, and engine version, so a Python wrapper alone cannot be mistaken for an operational engine. The Render web service must use Docker; the existing free compute plan is retained. Cron service runtimes are unchanged.

OCR renders at 300 DPI and preserves grayscale strokes and punctuation. It compares automatic page segmentation and uniform-block segmentation using word confidence, with rotated retries for weak results. Text and bounding boxes come from the same recognition pass. English dictionary correction is disabled for rendition amounts/account codes. Each pass has a 30-second timeout; errors remain visible as extraction warnings. OpenMP is bounded to one worker to avoid oversubscribing the free instance, without disabling recognition passes.

Printed-number regression coverage uses an image-only PDF containing `184,724.43` and `9,805.07` through fast review. This verifies decimal/comma recognition, not universal accuracy or handwriting recognition. Unreadable source values still require appraiser verification.
