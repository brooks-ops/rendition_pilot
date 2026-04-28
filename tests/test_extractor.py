from app.extractor import PDFExtractor
from app.pipeline import _extract_pdf_bundle


def test_pdf_extractor_falls_back_to_pymupdf_when_pdfplumber_open_fails(monkeypatch):
    class FakePage:
        def get_text(self, mode):
            if mode == "blocks":
                return [
                    (0, 20, 100, 40, "Total Fixed Assets 184,724.43", 0, 0),
                    (0, 10, 100, 20, "Schedule E", 0, 0),
                ]
            if mode == "text":
                return "unused"
            if mode == "words":
                return [
                    (0, 10, 20, 20, "Schedule", 0, 0, 0),
                    (21, 10, 30, 20, "E", 0, 0, 1),
                    (0, 20, 30, 30, "184,724.43", 0, 1, 0),
                ]
            return []

    class FakeDoc:
        def __init__(self, pages):
            self._pages = pages

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            return iter(self._pages)

        def __len__(self):
            return len(self._pages)

        def __getitem__(self, index):
            return self._pages[index]

    monkeypatch.setattr("app.extractor.pdfplumber.open", lambda path: (_ for _ in ()).throw(RuntimeError("bad pdf")))
    monkeypatch.setattr("app.extractor.fitz.open", lambda path: FakeDoc([FakePage()]))

    extractor = PDFExtractor()
    pages = extractor.extract_pages("broken.pdf")
    words = extractor.extract_page_words("broken.pdf", 1)

    assert pages == [
        {
            "page_number": 1,
            "text": "Schedule E\nTotal Fixed Assets 184,724.43",
        }
    ]
    assert words[0]["text"] == "Schedule"
    assert words[-1]["text"] == "184,724.43"


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
