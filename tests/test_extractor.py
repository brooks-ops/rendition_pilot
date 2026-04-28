from app.pipeline import _extract_pdf_bundle


def test_extract_pdf_bundle_uses_ocr_fallback_when_embedded_extraction_fails(monkeypatch):
    class FailingExtractor:
        def extract_pages(self, pdf_path: str):
            raise RuntimeError("broken xref table")

        def extract_page_words(self, pdf_path: str, page_number: int):
            return []

    monkeypatch.setattr("app.pipeline.PDFExtractor", FailingExtractor)
    monkeypatch.setattr("app.pipeline._ocr_pdf_pages_with_google_vision", lambda pdf_path: [])
    monkeypatch.setattr("app.pipeline._ocr_pdf_pages_with_openai_vision", lambda pdf_path: [])
    monkeypatch.setattr("app.pipeline._ocr_pdf_pages_with_azure_document_intelligence", lambda pdf_path: [])
    monkeypatch.setattr(
        "app.pipeline._ocr_pdf_pages_with_pymupdf",
        lambda pdf_path: [
            {
                "page_number": 1,
                "text": "Recovered with OCR",
                "ocr_blocks": [],
                "text_source": "pymupdf_tesseract_ocr",
            }
        ],
    )

    result = _extract_pdf_bundle("broken.pdf")

    assert result["pages"][0]["text"] == "Recovered with OCR"
    assert result["pages"][0]["extraction_provider"] == "pymupdf_tesseract_ocr"
    assert "ocr_unavailable" not in result["pages"][0]
    assert "ocr_error" not in result["pages"][0]


def test_extract_pdf_bundle_returns_structured_error_when_all_extractors_fail(monkeypatch):
    class FailingExtractor:
        def extract_pages(self, pdf_path: str):
            raise RuntimeError("broken xref table")

        def extract_page_words(self, pdf_path: str, page_number: int):
            return []

    monkeypatch.setattr("app.pipeline.PDFExtractor", FailingExtractor)
    monkeypatch.setattr("app.pipeline._ocr_pdf_pages_with_google_vision", lambda pdf_path: [])
    monkeypatch.setattr("app.pipeline._ocr_pdf_pages_with_openai_vision", lambda pdf_path: [])
    monkeypatch.setattr("app.pipeline._ocr_pdf_pages_with_azure_document_intelligence", lambda pdf_path: [])
    monkeypatch.setattr("app.pipeline._ocr_pdf_pages_with_pymupdf", lambda pdf_path: [])

    result = _extract_pdf_bundle("broken.pdf")

    assert result["pages"][0]["page_number"] == 1
    assert result["pages"][0]["text"] == ""
    assert result["pages"][0]["ocr_unavailable"] is True
    assert "Embedded PDF text extraction failed" in result["pages"][0]["ocr_error"]
