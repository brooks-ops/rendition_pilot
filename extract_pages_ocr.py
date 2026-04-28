import argparse
import json
from pathlib import Path


def extract_pages_ocr(pdf_path: str, out_folder: str, provider: str) -> None:
    out_dir = Path(out_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    if provider == "google-vision":
        from app.pipeline import _ocr_pdf_pages_with_google_vision

        pages = _ocr_pdf_pages_with_google_vision(pdf_path)
    elif provider == "tesseract":
        from app.pipeline import _ocr_pdf_pages_with_pymupdf

        pages = _ocr_pdf_pages_with_pymupdf(pdf_path)
    else:
        raise ValueError(f"Unsupported OCR provider: {provider}")

    if not pages:
        raise RuntimeError(
            f"No OCR output produced with provider '{provider}'. "
            "Check credentials/configuration and confirm the PDF is readable."
        )

    for page in pages:
        page_number = int(page.get("page_number", 1))
        text = page.get("text", "") or ""
        (out_dir / f"page{page_number}.txt").write_text(text, encoding="utf-8")
        print(f"OCR processed page {page_number} with {provider}")

    json_path = out_dir / "pages.json"
    json_path.write_text(json.dumps({"pages": pages}, indent=2), encoding="utf-8")
    print(f"\nSaved OCR output to: {json_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", default="app/page_texts_ocr")
    parser.add_argument(
        "--provider",
        default="google-vision",
        choices=["google-vision", "tesseract"],
        help="OCR backend to use. Google Vision is the default.",
    )

    args = parser.parse_args()

    extract_pages_ocr(args.pdf, args.out, args.provider)
