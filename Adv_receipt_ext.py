import csv
import re
import unicodedata
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
    "Total Rule",
    "Status",
]

TOTAL_LABELS = {
    "total amount payable": 100,
    "total payable amount": 100,
    "amount paid": 100,
    "total paid": 100,
    "paid amount": 100,
    "order total": 95,
    "order amount": 95,
    "total bill": 95,
    "bill total": 95,
    "grand total": 95,
    "net payable": 90,
    "amount payable": 95,
    "total payable": 95,
    "payable amount": 85,
    "total due": 90,
    "payment amount": 85,
    "amount to pay": 85,
    "you paid": 80,
    "invoice total": 80,
    "invoice value": 70,
    "total": 20,
}

NON_FINAL_LABELS = (
    "item total",
    "subtotal",
    "sub total",
    "previous due",
    "previous balance",
    "amount after due",
    "delivery fee",
    "handling fee",
    "packing fee",
    "platform fee",
    "taxable value",
    "gst",
    "tax",
    "discount",
    "savings",
    "coupon",
    "quantity",
    "qty",
    "mrp",
)

AMOUNT_RE = re.compile(
    r"(?<![\w.])(?:₹|rs\.?|inr)?\s*(-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|-?\d+(?:\.\d{1,2})?)(?![\w.])",
    re.IGNORECASE,
)
DATE_VALUE_PATTERN = r"(?:\d{1,2}[/-](?:\d{1,2}|[A-Za-z]{3,9})[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[ \t]+[A-Za-z]{3,9}[ \t]+\d{2,4})"
DATE_RE = re.compile(r"\b" + DATE_VALUE_PATTERN + r"\b")
STANDALONE_AMOUNT_RE = re.compile(
    r"(?:₹|rs\.?|inr)?\s*-?[\d,]+(?:\.\d{1,2})?", re.IGNORECASE
)


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
    """Keep page boundaries so totals cannot use a number from another page."""
    pages = []
    ocr_used = False
    ocr_available = None

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or page.extract_text(layout=True) or ""
            text = normalise_text(text)

            if len(re.sub(r"\s+", "", text)) < 20:
                ocr_text = ocr_page(page)
                ocr_available = ocr_text is not None
                if ocr_text and len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
                    ocr_used = True

            pages.append([line.strip() for line in text.splitlines() if line.strip()])

    return pages, ocr_used, ocr_available


def amounts_in(text):
    amounts = []
    for match in AMOUNT_RE.finditer(text):
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if -1_000_000 < amount < 1_000_000:
            amounts.append(amount)
    return amounts


def format_amount(amount):
    return f"{amount:.2f}"


def preferred_amount(values):
    """Prefer a decimal currency value over date or identifier fragments."""
    decimal_values = [value for value in values if not value.is_integer()]
    return decimal_values[-1] if decimal_values else values[-1]


def label_score(line):
    lower_line = line.casefold()
    if any(label in lower_line for label in NON_FINAL_LABELS):
        return 0
    return max((score for label, score in TOTAL_LABELS.items() if label in lower_line), default=0)


def extract_instamart_total(lines):
    """Handle Instamart invoices with an unlabelled final table cell."""
    for index, line in enumerate(lines):
        if "invoice value" not in line.casefold():
            continue

        for next_line in lines[index + 1 : index + 4]:
            if STANDALONE_AMOUNT_RE.fullmatch(next_line):
                values = amounts_in(next_line)
                if len(values) == 1:
                    return values[0], "Instamart standalone amount after Invoice Value", "Confirmed"

        invoice_values = amounts_in(line)
        if not invoice_values:
            continue
        total = invoice_values[-1]
        fee_found = False
        for next_line in lines[index + 1 : index + 3]:
            if "handling fee" in next_line.casefold():
                fee_values = amounts_in(next_line)
                if fee_values:
                    total += preferred_amount(fee_values)
                    fee_found = True
        if fee_found:
            return total, "Instamart Invoice Value plus Handling Fee", "Review calculated total"

    return None


def extract_total(page_lines):
    candidates = []

    for page_number, lines in enumerate(page_lines, start=1):
        instamart_total = extract_instamart_total(lines)
        if instamart_total is not None:
            amount, rule, status = instamart_total
            return format_amount(amount), rule, status

        for index, line in enumerate(lines):
            score = label_score(line)
            if not score:
                continue

            for offset in (0, 1, 2, -1):
                amount_index = index + offset
                if amount_index < 0 or amount_index >= len(lines):
                    continue
                value_line = lines[amount_index]
                if offset and any(label in value_line.casefold() for label in NON_FINAL_LABELS):
                    continue
                values = amounts_in(value_line)
                if not values:
                    continue
                penalty = 0 if offset == 0 else 5 if offset > 0 else 8
                candidates.append(
                    (
                        score - penalty,
                        page_number,
                        index,
                        preferred_amount(values),
                        f"Matched '{line[:60]}' on page {page_number}",
                    )
                )

    if not candidates:
        return "Not Found", "No recognised total label", "Needs review"

    score, _, _, amount, rule = max(candidates, key=lambda item: item[:3])
    status = "Confirmed" if score >= 80 else "Review low-confidence label"
    return format_amount(amount), rule, status


def extract_vendor(page_lines):
    lines = [line for page in page_lines for line in page]
    seller_pattern = re.compile(r"(?:seller name|merchant|vendor|billed by)\s*:\s*(.+)", re.IGNORECASE)
    for line in lines:
        match = seller_pattern.search(line)
        if match:
            return match.group(1).strip()[:60]

    ignored_headings = {"tax invoice", "invoice", "receipt"}
    for line in lines:
        if line.casefold() not in ignored_headings and len(line) > 2:
            return line[:60]
    return "Unknown Vendor"


def extract_dates(page_lines):
    text = "\n".join(line for page in page_lines for line in page)
    all_dates = list(dict.fromkeys(DATE_RE.findall(text)))

    for page in page_lines:
        for index, line in enumerate(page):
            if not any(label in line.casefold() for label in ("date of invoice", "invoice date", "order date", "bill date", "statement date")):
                continue
            for candidate_line in page[index : index + 3]:
                match = DATE_RE.search(candidate_line)
                if match:
                    return match.group(0), ", ".join(all_dates) or "Not Found"

    return (all_dates[0] if all_dates else "Not Found"), ", ".join(all_dates) or "Not Found"


def process_file(file_path):
    page_lines, ocr_used, ocr_available = extract_pages(file_path)
    if not any(page_lines):
        status = "Scanned PDF: install pytesseract and Tesseract OCR" if ocr_available is False else "Scanned PDF / Empty"
        return [file_path.name, "Unknown Vendor", "Not Found", "Not Found", "Not Found", "No text extracted", status]

    primary_date, all_dates = extract_dates(page_lines)
    amount, rule, status = extract_total(page_lines)
    if ocr_used:
        status = f"{status}; OCR used"

    return [file_path.name, extract_vendor(page_lines), primary_date, all_dates, amount, rule, status]


def main():
    PDF_FOLDER.mkdir(exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(FIELDS)

        for file_path in sorted(PDF_FOLDER.glob("*.pdf")):
            try:
                row = process_file(file_path)
            except Exception as error:
                row = [file_path.name, "Not Found", "Not Found", "Not Found", "Not Found", "Error", str(error)]
            writer.writerow(row)
            print(f"Extracted: {file_path.name} -> Total: {row[4]} ({row[6]})")

    print(f"Script complete. Open '{OUTPUT_CSV}' to see the results.")


if __name__ == "__main__":
    main()
