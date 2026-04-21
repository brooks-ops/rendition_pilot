import os
import json
import argparse
from pdf2image import convert_from_path
import pytesseract


def extract_pages_ocr(pdf_path: str, out_folder: str):
    os.makedirs(out_folder, exist_ok=True)

    images = convert_from_path(pdf_path, dpi=300)

    pages = []

    for i, image in enumerate(images):
        text = pytesseract.image_to_string(image)

        page_data = {
            "page_number": i + 1,
            "text": text
        }

        pages.append(page_data)

        # Optional debug text file
        with open(os.path.join(out_folder, f"page{i+1}.txt"), "w", encoding="utf-8") as f:
            f.write(text)

        print(f"OCR processed page {i+1}")

    # Save JSON for pipeline
    json_path = os.path.join(out_folder, "pages.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(pages, f, indent=2)

    print(f"\nSaved OCR output to: {json_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", default="app/page_texts_ocr")

    args = parser.parse_args()

    extract_pages_ocr(args.pdf, args.out)