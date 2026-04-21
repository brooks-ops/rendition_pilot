from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import fitz  # PyMuPDF


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_page_text(page: fitz.Page) -> str:
    """
    Better than raw get_text('text') alone:
    - reads blocks
    - sorts top-to-bottom, left-to-right
    - joins them cleanly
    """
    blocks = page.get_text("blocks")
    if not blocks:
        return normalize_text(page.get_text("text") or "")

    cleaned_blocks = []
    for block in blocks:
        # block = (x0, y0, x1, y1, text, block_no, block_type)
        if len(block) < 5:
            continue
        x0, y0, x1, y1, text = block[:5]
        if not text or not str(text).strip():
            continue
        cleaned_blocks.append((float(y0), float(x0), str(text)))

    cleaned_blocks.sort(key=lambda item: (round(item[0], 1), round(item[1], 1)))
    combined = "\n".join(text for _, _, text in cleaned_blocks)
    return normalize_text(combined)


def extract_page_words(page: fitz.Page) -> List[Dict[str, Any]]:
    """
    Word-level fallback metadata.
    """
    words = page.get_text("words")
    results: List[Dict[str, Any]] = []

    for w in words:
        # (x0, y0, x1, y1, "word", block_no, line_no, word_no)
        if len(w) < 5:
            continue
        x0, y0, x1, y1, text = w[:5]
        if not text:
            continue
        results.append(
            {
                "text": str(text),
                "x0": float(x0),
                "y0": float(y0),
                "x1": float(x1),
                "y1": float(y1),
            }
        )

    results.sort(key=lambda item: (round(item["y0"], 1), round(item["x0"], 1)))
    return results


def extract_pdf_pages_to_txt_and_json(pdf_path: str, output_folder: str) -> None:
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    out_dir = Path(output_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_file)
    page_count = len(doc)

    pages_json: List[Dict[str, Any]] = []

    for i, page in enumerate(doc, start=1):
        text = extract_page_text(page)
        words = extract_page_words(page)

        txt_file = out_dir / f"page{i}.txt"
        txt_file.write_text(text, encoding="utf-8")

        pages_json.append(
            {
                "page_number": i,
                "text": text,
                "ocr_blocks": words,
            }
        )

    doc.close()

    json_file = out_dir / "pages.json"
    json_file.write_text(json.dumps({"pages": pages_json}, indent=2), encoding="utf-8")

    print(f"Extracted {page_count} pages to: {out_dir}")
    print(f"Wrote JSON page bundle: {json_file}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract PDF pages to TXT + JSON")
    parser.add_argument(
        "--pdf",
        required=True,
        help="Path to source PDF",
    )
    parser.add_argument(
        "--out",
        default="app/page_texts",
        help="Output folder for page TXT/JSON files",
    )
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    extract_pdf_pages_to_txt_and_json(args.pdf, args.out)