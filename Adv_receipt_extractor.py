import csv
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber


SCRIPT_DIR = Path(__file__).resolve().parent
PDF_FOLDER = SCRIPT_DIR / "client_receipts"
OUTPUT_CSV = SCRIPT_DIR / "monthly_expense_report.csv"

FIELDS = [
    "File Name",
    "Vendor/Bill Name",
    "Primary Date",
    "All Dates Found",
    "Total Amount",
    "Confidence Score",
    "Evidence",
    "Status",
]

# These are evidence signals, not strict requirements. A new vendor can still
# produce a candidate without using any of these exact words.
TOTAL_HINTS = ("total", "payable", "paid", "due", "settlement", "charged")
AMOUNT_HINTS = ("amount", "invoice", "bill", "payment", "balance", "value")
NON_FINAL_HINTS = (
    "item",
    "subtotal",
    "tax",
    "gst",
    "discount",
    "coupon",
    "saving",
    "delivery",
    "handling",
    "packing",
    "platform",
    "quantity",
    "qty",
    "mrp",
    "previous",
)
IDENTIFIER_HINTS = (
    "order id",
    "invoice no",
    "invoice number",
    "account no",
    "account number",
    "gstin",
    "pan",
    "hsn",
    "sac",
    "phone",
    "mobile",
    "telephone",
    "contact number",
    "pincode",
)

CURRENCY_RE = re.compile(r"(?:\u20B9|rs\.?|inr)", re.IGNORECASE)
AMOUNT_RE = re.compile(
    r"(?<![\w.])(?:(?:\u20B9|rs\.?|inr)\s*)?(-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|-?\d+(?:\.\d{1,2})?)(?![\w.])",
    re.IGNORECASE,
)
DATE_VALUE_PATTERN = r"(?:\d{1,2}[/-](?:\d{1,2}|[A-Za-z]{3,9})[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[ \t]+[A-Za-z]{3,9}[ \t]+\d{2,4})"
DATE_RE = re.compile(r"\b" + DATE_VALUE_PATTERN + r"\b")
PHONE_RE = re.compile(r"(?:\+?91[-\s]?)?\d{10}\b")
STANDALONE_AMOUNT_RE = re.compile(r"(?:\u20B9|rs\.?|inr)?\s*-?[\d,]+(?:\.\d{1,2})?", re.IGNORECASE)


@dataclass
class AmountCandidate:
    amount: float
    raw: str
    page_number: int
    line_index: int
    line: str
    has_currency: bool
    has_decimal: bool
    score: int = 0
    evidence: list[str] = field(default_factory=list)


def normalise_text(text):
    return unicodedata.normalize("NFKC", text).replace("\xa0", " ")


def ocr_page(page):
    """Return OCR text if optional pytesseract is installed, otherwise None."""
    try:
        import pytesseract

        image = page.to_image(resolution=300).original
        return normalise_text(pytesseract.image_to_string(image))
    except Exception:
        return None


def extract_pages(file_path):
    """Extract text one page at a time, preserving page-level context."""
    pages = []
    ocr_used = False
    ocr_available = None

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = normalise_text(page.extract_text() or page.extract_text(layout=True) or "")
            if len(re.sub(r"\s+", "", text)) < 20:
                ocr_text = ocr_page(page)
                ocr_available = ocr_text is not None
                if ocr_text and len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
                    ocr_used = True
            pages.append([line.strip() for line in text.splitlines() if line.strip()])

    return pages, ocr_used, ocr_available


def format_amount(amount):
    return f"{amount:.2f}"


def monetary_candidates(page_lines):
    """Find every plausible numeric amount before deciding which one is final."""
    candidates = []
    for page_number, lines in enumerate(page_lines, start=1):
        for line_index, line in enumerate(lines):
            for match in AMOUNT_RE.finditer(line):
                raw = match.group(1)
                try:
                    amount = float(raw.replace(",", ""))
                except ValueError:
                    continue
                if not -1_000_000 < amount < 1_000_000:
                    continue
                token = match.group(0)
                candidates.append(
                    AmountCandidate(
                        amount=amount,
                        raw=raw,
                        page_number=page_number,
                        line_index=line_index,
                        line=line,
                        has_currency=bool(CURRENCY_RE.search(token)),
                        has_decimal="." in raw,
                    )
                )
    return candidates


def add_score(candidate, points, reason):
    candidate.score += points
    candidate.evidence.append(f"{points:+d} {reason}")


def score_candidate(candidate, lines, duplicate_counts):
    """Score evidence instead of depending on a single exact label."""
    line_lower = candidate.line.casefold()
    nearby_text = " ".join(lines[max(0, candidate.line_index - 2) : candidate.line_index + 3]).casefold()

    if candidate.has_currency:
        add_score(candidate, 18, "currency marker")
    if candidate.has_decimal:
        add_score(candidate, 12, "decimal money format")
    if "," in candidate.raw:
        add_score(candidate, 4, "thousands separator")
    if "total" in line_lower:
        add_score(candidate, 30, "total wording on same row")
        if any(hint in line_lower for hint in ("payable", "due", "amount", "balance")):
            add_score(candidate, 10, "total combined with payable/amount wording")
    elif any(hint in line_lower for hint in TOTAL_HINTS):
        add_score(candidate, 18, "payment wording on same row")
    elif any(hint in nearby_text for hint in TOTAL_HINTS):
        add_score(candidate, 14, "total/payment wording nearby")
    if any(hint in line_lower for hint in AMOUNT_HINTS):
        add_score(candidate, 12, "amount wording on same row")
    if any(hint in line_lower for hint in NON_FINAL_HINTS):
        add_score(candidate, -38, "subtotal, fee, tax, or item wording")
    if any(hint in nearby_text for hint in IDENTIFIER_HINTS):
        add_score(candidate, -75, "identifier context")
    if DATE_RE.search(candidate.line):
        add_score(candidate, -75, "date-shaped row")
    if PHONE_RE.search(candidate.line):
        add_score(candidate, -90, "phone-number-shaped row")
    if candidate.amount < 0:
        add_score(candidate, -15, "negative amount")
    if candidate.amount.is_integer() and 1900 <= candidate.amount <= 2100:
        add_score(candidate, -25, "year-like integer")
    if len(candidate.raw.replace("-", "").replace(",", "")) >= 7 and not candidate.has_currency:
        add_score(candidate, -30, "long unformatted number")

    repeats = duplicate_counts[round(candidate.amount, 2)]
    if repeats > 1:
        add_score(candidate, min((repeats - 1) * 4, 12), f"same value appears {repeats} times")

    # Section endings tend to contain summaries. Keep this a small signal so a
    # footer cannot win solely by being near the bottom of a page.
    if candidate.line_index >= max(1, int(len(lines) * 0.70)):
        add_score(candidate, 5, "near end of page section")


def extract_instamart_total(lines, page_number):
    """High-confidence layout rule for Instamart's unlabelled final table cell."""
    for index, line in enumerate(lines):
        if "invoice value" not in line.casefold():
            continue
        for next_line in lines[index + 1 : index + 4]:
            if STANDALONE_AMOUNT_RE.fullmatch(next_line):
                match = AMOUNT_RE.search(next_line)
                if match:
                    return float(match.group(1).replace(",", "")), page_number
    return None


def extract_total(page_lines):
    for page_number, lines in enumerate(page_lines, start=1):
        instamart_total = extract_instamart_total(lines, page_number)
        if instamart_total is not None:
            amount, page = instamart_total
            return format_amount(amount), 100, f"Instamart final table cell on page {page}", "Confirmed"

    candidates = monetary_candidates(page_lines)
    if not candidates:
        return "Not Found", 0, "No numeric money candidates", "Needs review"

    duplicate_counts = Counter(round(candidate.amount, 2) for candidate in candidates)
    for candidate in candidates:
        score_candidate(candidate, page_lines[candidate.page_number - 1], duplicate_counts)

    best = max(candidates, key=lambda candidate: (candidate.score, candidate.page_number, candidate.line_index))
    evidence = "; ".join(best.evidence)
    if best.score >= 70:
        status = "Confirmed"
    elif best.score >= 40:
        status = "Review suggested amount"
    else:
        return "Not Found", best.score, evidence, "Needs review"

    return format_amount(best.amount), best.score, evidence, status


def extract_vendor(page_lines):
    lines = [line for page in page_lines for line in page]
    seller_pattern = re.compile(r"(?:seller name|merchant|vendor|billed by)\s*:\s*(.+)", re.IGNORECASE)
    for line in lines:
        match = seller_pattern.search(line)
        if match:
            return match.group(1).strip()[:60]

    for line in lines:
        if line.casefold() not in {"tax invoice", "invoice", "receipt"} and len(line) > 2:
            return line[:60]
    return "Unknown Vendor"


def extract_dates(page_lines):
    text = "\n".join(line for page in page_lines for line in page)
    all_dates = list(dict.fromkeys(DATE_RE.findall(text)))
    date_labels = ("date of invoice", "invoice date", "order date", "bill date", "statement date")

    for page in page_lines:
        for index, line in enumerate(page):
            if any(label in line.casefold() for label in date_labels):
                for candidate_line in page[index : index + 3]:
                    match = DATE_RE.search(candidate_line)
                    if match:
                        return match.group(0), ", ".join(all_dates) or "Not Found"
    return (all_dates[0] if all_dates else "Not Found"), ", ".join(all_dates) or "Not Found"


def process_file(file_path):
    page_lines, ocr_used, ocr_available = extract_pages(file_path)
    if not any(page_lines):
        status = "Scanned PDF: install pytesseract and Tesseract OCR" if ocr_available is False else "Scanned PDF / Empty"
        return [file_path.name, "Unknown Vendor", "Not Found", "Not Found", "Not Found", 0, "No text extracted", status]

    primary_date, all_dates = extract_dates(page_lines)
    amount, score, evidence, status = extract_total(page_lines)
    if ocr_used:
        status = f"{status}; OCR used"
    return [file_path.name, extract_vendor(page_lines), primary_date, all_dates, amount, score, evidence, status]


def main():
    PDF_FOLDER.mkdir(exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(FIELDS)
        for file_path in sorted(PDF_FOLDER.glob("*.pdf")):
            try:
                row = process_file(file_path)
            except Exception as error:
                row = [file_path.name, "Not Found", "Not Found", "Not Found", "Not Found", 0, "Error", str(error)]
            writer.writerow(row)
            print(f"Extracted: {file_path.name} -> Total: {row[4]} ({row[7]})")

    print(f"Script complete. Open '{OUTPUT_CSV}' to see the results.")


if __name__ == "__main__":
    main()
