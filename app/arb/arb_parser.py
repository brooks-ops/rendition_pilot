from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import requests

from app.arb.arb_models import ARBParsedPacket
from app.extractor import PDFExtractor

logger = logging.getLogger(__name__)


def decode_upload_base64(file_base64: str) -> bytes:
    encoded = file_base64.split(",", 1)[-1]
    return base64.b64decode(encoded)


def parse_evidence_packet(file_name: str, file_bytes: bytes, packet_label: str) -> ARBParsedPacket:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf_packet(file_name, file_bytes, packet_label)
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        return _parse_image_packet(file_name, file_bytes, packet_label)
    return ARBParsedPacket(
        file_name=file_name,
        packet_label=packet_label,
        warnings=[f"Unsupported evidence packet type '{suffix or 'unknown'}'. Upload a PDF or common image file."],
    )


def _parse_pdf_packet(file_name: str, file_bytes: bytes, packet_label: str) -> ARBParsedPacket:
    warnings: list[str] = []
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        pdf_path = Path(tmp.name)
    try:
        extractor = PDFExtractor()
        pages = _extract_embedded_pages(extractor, str(pdf_path))
        if _has_usable_text(pages):
            return _packet_from_pages(file_name, packet_label, pages, "embedded_pdf_text", warnings)

        warnings.append("Embedded PDF text was limited; OCR fallback was attempted.")
        ocr_pages, provider, ocr_warnings = _run_pdf_ocr_fallbacks(str(pdf_path))
        warnings.extend(ocr_warnings)
        if _has_usable_text(ocr_pages):
            return _packet_from_pages(file_name, packet_label, ocr_pages, provider, warnings)

        fallback_pages = pages or ocr_pages
        if not any(_page_text(page) for page in fallback_pages):
            warnings.append("No usable text could be extracted. Configure Google Document AI, Google Vision, OpenAI Vision OCR, or local Tesseract for scanned packets.")
        return _packet_from_pages(file_name, packet_label, fallback_pages, provider or "embedded_pdf_text", warnings)
    except Exception as exc:
        logger.exception("ARB PDF parsing failed for %s", file_name)
        return ARBParsedPacket(
            file_name=file_name,
            packet_label=packet_label,
            warnings=[f"Could not parse packet: {type(exc).__name__}: {exc}"],
        )
    finally:
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


def _extract_embedded_pages(extractor: PDFExtractor, pdf_path: str) -> list[dict[str, Any]]:
    pages = extractor.extract_pages(pdf_path)
    for page in pages:
        page_number = int(page.get("page_number") or 1)
        try:
            page["ocr_blocks"] = extractor.extract_page_words(pdf_path, page_number)
        except Exception:
            page["ocr_blocks"] = []
        page.setdefault("text_source", "embedded_pdf_text")
    return pages


def _run_pdf_ocr_fallbacks(pdf_path: str) -> tuple[list[dict[str, Any]], str, list[str]]:
    warnings: list[str] = []
    providers = [
        ("google_document_ai", _google_document_ai_configured, "_ocr_pdf_pages_with_google_document_ai"),
        ("google_cloud_vision", _google_vision_configured, "_ocr_pdf_pages_with_google_vision"),
        ("openai_vision_ocr", _openai_configured, "_ocr_pdf_pages_with_openai_vision"),
        ("pymupdf_tesseract_ocr", lambda: True, "_ocr_pdf_pages_with_pymupdf"),
    ]

    try:
        import app.pipeline as pipeline
    except Exception as exc:
        return [], "", [f"OCR fallback stack could not be loaded: {type(exc).__name__}: {exc}"]

    for provider, is_configured, function_name in providers:
        if not is_configured():
            warnings.append(f"{provider} is not configured.")
            continue
        try:
            pages = getattr(pipeline, function_name)(pdf_path)
        except Exception as exc:
            logger.warning("ARB OCR provider %s failed: %s", provider, exc)
            warnings.append(f"{provider} failed: {type(exc).__name__}: {exc}")
            continue
        errors = [str(page.get("ocr_error")) for page in pages or [] if page.get("ocr_error")]
        warnings.extend(errors)
        if _has_usable_text(pages):
            return pages, provider, warnings
    return [], "", warnings


def _parse_image_packet(file_name: str, file_bytes: bytes, packet_label: str) -> ARBParsedPacket:
    warnings: list[str] = []
    text = ""
    provider = "none"
    warnings.append("Google Vision and OpenAI Vision OCR are disabled on local-no-ai branch.")
    warnings.append("No usable image text could be extracted from image-only ARB evidence.")

    pages = [{"page_number": 1, "text": text, "ocr_blocks": [], "text_source": provider}]
    return _packet_from_pages(file_name, packet_label, pages, provider, warnings)


def _ocr_image_with_google_vision(file_bytes: bytes) -> tuple[str, str]:
    return "", "Google Vision OCR disabled on local-no-ai branch."

    api_key = os.getenv("GOOGLE_VISION_API_KEY") or os.getenv("GOOGLE_CLOUD_VISION_API_KEY")
    if not api_key:
        return "", "Google Vision API key is not configured."
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(file_bytes).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            }
        ]
    }
    try:
        response = requests.post(
            "https://vision.googleapis.com/v1/images:annotate",
            params={"key": api_key},
            json=payload,
            timeout=float(os.getenv("GOOGLE_VISION_OCR_REQUEST_TIMEOUT_SECONDS") or "20"),
        )
        if response.status_code >= 400:
            return "", f"Google Vision OCR unavailable: HTTP {response.status_code}: {response.text[:300]}"
        data = response.json()
        result = (data.get("responses") or [{}])[0]
        if result.get("error"):
            return "", f"Google Vision OCR error: {result.get('error')}"
        return str((result.get("fullTextAnnotation") or {}).get("text") or "").strip(), ""
    except Exception as exc:
        return "", f"Google Vision OCR failed: {type(exc).__name__}: {exc}"


def _ocr_image_with_openai(file_name: str, file_bytes: bytes) -> tuple[str, str]:
    return "", "OpenAI vision OCR disabled on local-no-ai branch."

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "", "OPENAI_API_KEY is not configured."
    try:
        from openai import OpenAI
    except Exception as exc:
        return "", f"OpenAI SDK unavailable: {exc}"
    mime_type = mimetypes.guess_type(file_name)[0] or "image/png"
    image_b64 = base64.b64encode(file_bytes).decode("ascii")
    client = OpenAI(api_key=api_key, timeout=float(os.getenv("OPENAI_VISION_OCR_TIMEOUT_SECONDS") or "12"), max_retries=0)
    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_VISION_OCR_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Extract all readable text from this ARB evidence image. Preserve tables and labels when possible."},
                        {"type": "input_image", "image_url": f"data:{mime_type};base64,{image_b64}"},
                    ],
                }
            ],
        )
        return (response.output_text or "").strip(), ""
    except Exception as exc:
        return "", f"OpenAI vision OCR failed: {type(exc).__name__}: {exc}"


def _packet_from_pages(
    file_name: str,
    packet_label: str,
    pages: list[dict[str, Any]],
    provider: str,
    warnings: list[str],
) -> ARBParsedPacket:
    text = "\n\n".join(
        f"--- Page {page.get('page_number', index)} ---\n{_page_text(page)}".strip()
        for index, page in enumerate(pages or [], start=1)
        if _page_text(page)
    ).strip()
    return ARBParsedPacket(
        file_name=file_name,
        packet_label=packet_label,
        text=text,
        pages=pages or [],
        extraction_provider=provider or "none",
        warnings=_dedupe(warnings),
    )


def _has_usable_text(pages: list[dict[str, Any]] | None) -> bool:
    text = "\n".join(_page_text(page) for page in pages or [])
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < 200:
        return False
    alpha = sum(ch.isalpha() for ch in normalized)
    digits = sum(ch.isdigit() for ch in normalized)
    return alpha >= 80 or (alpha >= 40 and digits >= 20)


def _page_text(page: dict[str, Any]) -> str:
    return str(page.get("text") or "").strip()


def _google_document_ai_configured() -> bool:
    processor = os.getenv("GOOGLE_DOCUMENT_AI_PROCESSOR_NAME") or (
        os.getenv("GOOGLE_DOCUMENT_AI_PROJECT_ID")
        and os.getenv("GOOGLE_DOCUMENT_AI_LOCATION")
        and os.getenv("GOOGLE_DOCUMENT_AI_PROCESSOR_ID")
    )
    auth = os.getenv("GOOGLE_DOCUMENT_AI_API_KEY") or os.getenv("GOOGLE_DOCUMENT_AI_ACCESS_TOKEN")
    return bool(processor and auth)


def _google_vision_configured() -> bool:
    return False


def _openai_configured() -> bool:
    return False


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result
