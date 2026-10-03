import re
import unicodedata
import pdfplumber

from config import OCR_SETTINGS


def normalise_text(text):
    return unicodedata.normalize("NFKC", text).replace("\xa0", " ")


def ocr_page(page):
    """Use OCR only when it is enabled in receipt_rules.json and available."""
    if not OCR_SETTINGS["enabled"]:
        return None

    try:
        import pytesseract

        image = page.to_image(
            resolution=OCR_SETTINGS["resolution"]
        ).original

        return normalise_text(
            pytesseract.image_to_string(image)
        )

    except Exception:
        return None


def extract_pages(file_path):
    """Extract page-by-page text so candidates retain their local context."""
    pages = []
    ocr_used = False
    ocr_available = None

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = normalise_text(
                page.extract_text()
                or page.extract_text(layout=True)
                or ""
            )

            if len(re.sub(r"\s+", "", text)) < OCR_SETTINGS["min_text_characters"]:
                ocr_text = ocr_page(page)
                ocr_available = ocr_text is not None

                if ocr_text and len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
                    ocr_used = True

            pages.append(
                [line.strip() for line in text.splitlines() if line.strip()]
            )

    return pages, ocr_used, ocr_available