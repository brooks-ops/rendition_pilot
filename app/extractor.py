import pdfplumber


class PDFExtractor:
    def extract_pages(self, pdf_path: str) -> list[dict]:
        pages = []

        with pdfplumber.open(pdf_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text() or ""
                pages.append({
                    "page_number": page_number,
                    "text": page_text
                })

        return pages

    def extract_page_words(self, pdf_path: str, page_number: int) -> list[dict]:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[page_number - 1]
            return page.extract_words()