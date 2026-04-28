import pdfplumber

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover - optional import guard
    fitz = None


class PDFExtractor:
    def extract_pages(self, pdf_path: str) -> list[dict]:
        pages = []

        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page_number, page in enumerate(pdf.pages, start=1):
                    page_text = page.extract_text() or ""
                    pages.append({
                        "page_number": page_number,
                        "text": page_text
                    })
        except Exception:
            return self._extract_pages_with_pymupdf(pdf_path)

        return pages

    def extract_page_words(self, pdf_path: str, page_number: int) -> list[dict]:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                page = pdf.pages[page_number - 1]
                return page.extract_words()
        except Exception:
            return self._extract_page_words_with_pymupdf(pdf_path, page_number)

    def _extract_pages_with_pymupdf(self, pdf_path: str) -> list[dict]:
        if fitz is None:
            raise RuntimeError("PyMuPDF is unavailable for PDF extraction fallback.")

        pages: list[dict] = []
        with fitz.open(pdf_path) as doc:
            for page_number, page in enumerate(doc, start=1):
                page_text = self._extract_pymupdf_page_text(page)
                pages.append({
                    "page_number": page_number,
                    "text": page_text,
                })
        return pages

    def _extract_page_words_with_pymupdf(self, pdf_path: str, page_number: int) -> list[dict]:
        if fitz is None:
            return []

        with fitz.open(pdf_path) as doc:
            if page_number < 1 or page_number > len(doc):
                return []
            page = doc[page_number - 1]
            words = []
            for word in page.get_text("words") or []:
                if len(word) < 5:
                    continue
                x0, y0, x1, y1, text = word[:5]
                cleaned = str(text or "").strip()
                if not cleaned:
                    continue
                words.append(
                    {
                        "text": cleaned,
                        "x0": float(x0),
                        "x1": float(x1),
                        "top": float(y0),
                        "y0": float(y0),
                        "y1": float(y1),
                    }
                )
            words.sort(key=lambda item: (round(item["top"], 1), round(item["x0"], 1)))
            return words

    def _extract_pymupdf_page_text(self, page) -> str:
        blocks = page.get_text("blocks") or []
        if not blocks:
            return (page.get_text("text") or "").strip()

        cleaned_blocks = []
        for block in blocks:
            if len(block) < 5:
                continue
            x0, y0, _x1, _y1, text = block[:5]
            cleaned = str(text or "").strip()
            if not cleaned:
                continue
            cleaned_blocks.append((float(y0), float(x0), cleaned))

        cleaned_blocks.sort(key=lambda item: (round(item[0], 1), round(item[1], 1)))
        return "\n".join(text for _, _, text in cleaned_blocks).strip()
