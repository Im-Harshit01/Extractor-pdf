import csv
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber


SCRIPT_DIR = Path(__file__).resolve().parent
RULES_FILE = SCRIPT_DIR / "receipt_rules.json"


def load_rules():
    try:
        with RULES_FILE.open(encoding="utf-8") as rules_file:
            rules = json.load(rules_file)
    except FileNotFoundError as error:
        raise RuntimeError(f"Missing configuration file: {RULES_FILE}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON in {RULES_FILE}: {error.msg} at line {error.lineno}") from error

    required_sections = {
        "paths": dict,
        "output_fields": list,
        "patterns": dict,
        "extraction": dict,
        "hints": dict,
        "scoring": dict,
        "thresholds": dict,
        "vendor_rules": list,
    }
    for name, expected_type in required_sections.items():
        if not isinstance(rules.get(name), expected_type):
            raise RuntimeError(f"receipt_rules.json needs a '{name}' {expected_type.__name__}")

    required_patterns = {"currency", "amount", "date", "phone", "standalone_amount"}
    missing_patterns = required_patterns - set(rules["patterns"])
    if missing_patterns:
        raise RuntimeError(f"receipt_rules.json is missing patterns: {', '.join(sorted(missing_patterns))}")
    if len(rules["output_fields"]) != 8:
        raise RuntimeError("receipt_rules.json needs exactly 8 output_fields")
    return rules


RULES = load_rules()
PATHS = RULES["paths"]
FIELDS = RULES["output_fields"]
PATTERNS = RULES["patterns"]
EXTRACTION = RULES["extraction"]
AMOUNT_SETTINGS = EXTRACTION["amount"]
CONTEXT_SETTINGS = EXTRACTION["context"]
OCR_SETTINGS = EXTRACTION["ocr"]
HINTS = RULES["hints"]
SCORES = RULES["scoring"]
THRESHOLDS = RULES["thresholds"]
VENDOR_RULES = RULES["vendor_rules"]
PDF_FOLDER = SCRIPT_DIR / PATHS["receipts_folder"]
OUTPUT_CSV = SCRIPT_DIR / PATHS["output_csv"]

CURRENCY_RE = re.compile(PATTERNS["currency"], re.IGNORECASE)
AMOUNT_RE = re.compile(PATTERNS["amount"], re.IGNORECASE)
DATE_RE = re.compile(PATTERNS["date"])
PHONE_RE = re.compile(PATTERNS["phone"])
STANDALONE_AMOUNT_RE = re.compile(PATTERNS["standalone_amount"], re.IGNORECASE)


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
    """Use OCR only when it is enabled in receipt_rules.json and available."""
    if not OCR_SETTINGS["enabled"]:
        return None
    try:
        import pytesseract

        image = page.to_image(resolution=OCR_SETTINGS["resolution"]).original
        return normalise_text(pytesseract.image_to_string(image))
    except Exception:
        return None


def extract_pages(file_path):
    """Extract page-by-page text so candidates retain their local context."""
    pages = []
    ocr_used = False
    ocr_available = None

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = normalise_text(page.extract_text() or page.extract_text(layout=True) or "")
            if len(re.sub(r"\s+", "", text)) < OCR_SETTINGS["min_text_characters"]:
                ocr_text = ocr_page(page)
                ocr_available = ocr_text is not None
                if ocr_text and len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
                    ocr_used = True
            pages.append([line.strip() for line in text.splitlines() if line.strip()])

    return pages, ocr_used, ocr_available


def format_amount(amount):
    return f"{amount:.2f}"


def amounts_in(text):
    amounts = []
    for match in AMOUNT_RE.finditer(text):
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if -AMOUNT_SETTINGS["max_abs_amount"] < amount < AMOUNT_SETTINGS["max_abs_amount"]:
            amounts.append(amount)
    return amounts


def monetary_candidates(page_lines):
    candidates = []
    for page_number, lines in enumerate(page_lines, start=1):
        for line_index, line in enumerate(lines):
            for match in AMOUNT_RE.finditer(line):
                raw = match.group(1)
                try:
                    amount = float(raw.replace(",", ""))
                except ValueError:
                    continue
                if not -AMOUNT_SETTINGS["max_abs_amount"] < amount < AMOUNT_SETTINGS["max_abs_amount"]:
                    continue
                candidates.append(
                    AmountCandidate(
                        amount=amount,
                        raw=raw,
                        page_number=page_number,
                        line_index=line_index,
                        line=line,
                        has_currency=bool(CURRENCY_RE.search(match.group(0))),
                        has_decimal="." in raw,
                    )
                )
    return candidates


def add_score(candidate, points, reason):
    candidate.score += points
    candidate.evidence.append(f"{points:+d} {reason}")


def score_candidate(candidate, lines, duplicate_counts):
    """Combine independent evidence rather than requiring a fixed label."""
    line_lower = candidate.line.casefold()
    radius = CONTEXT_SETTINGS["nearby_lines"]
    nearby_text = " ".join(lines[max(0, candidate.line_index - radius) : candidate.line_index + radius + 1]).casefold()

    if candidate.has_currency:
        add_score(candidate, SCORES["currency_marker"], "currency marker")
    if candidate.has_decimal:
        add_score(candidate, SCORES["decimal_money_format"], "decimal money format")
    if "," in candidate.raw:
        add_score(candidate, SCORES["thousands_separator"], "thousands separator")
    if "total" in line_lower:
        add_score(candidate, SCORES["total_wording"], "total wording on same row")
        if any(hint in line_lower for hint in HINTS["combined_total"]):
            add_score(candidate, SCORES["combined_total_wording"], "total combined with payable/amount wording")
    elif any(hint in line_lower for hint in HINTS["total"]):
        add_score(candidate, SCORES["payment_wording"], "payment wording on same row")
    elif any(hint in nearby_text for hint in HINTS["total"]):
        add_score(candidate, SCORES["nearby_total_wording"], "total/payment wording nearby")
    if any(hint in line_lower for hint in HINTS["amount"]):
        add_score(candidate, SCORES["amount_wording"], "amount wording on same row")
    if any(hint in line_lower for hint in HINTS["non_final"]):
        add_score(candidate, SCORES["non_final_context"], "subtotal, fee, tax, or item wording")
    if any(hint in nearby_text for hint in HINTS["identifier"]):
        add_score(candidate, SCORES["identifier_context"], "identifier context")
    if DATE_RE.search(candidate.line):
        add_score(candidate, SCORES["date_context"], "date-shaped row")
    if PHONE_RE.search(candidate.line):
        add_score(candidate, SCORES["phone_context"], "phone-number-shaped row")
    if candidate.amount < 0:
        add_score(candidate, SCORES["negative_amount"], "negative amount")

    year_start, year_end = AMOUNT_SETTINGS["year_range"]
    if candidate.amount.is_integer() and year_start <= candidate.amount <= year_end:
        add_score(candidate, SCORES["year_like_integer"], "year-like integer")
    if len(candidate.raw.replace("-", "").replace(",", "")) >= AMOUNT_SETTINGS["long_number_digits"] and not candidate.has_currency:
        add_score(candidate, SCORES["long_unformatted_number"], "long unformatted number")

    repeats = duplicate_counts[round(candidate.amount, 2)]
    if repeats > 1:
        bonus = min((repeats - 1) * SCORES["repeat_amount"], SCORES["maximum_repeat_bonus"])
        add_score(candidate, bonus, f"same value appears {repeats} times")
    if candidate.line_index >= max(1, int(len(lines) * CONTEXT_SETTINGS["section_end_ratio"])):
        add_score(candidate, SCORES["section_end"], "near end of page section")


def apply_vendor_rules(page_lines):
    """Run JSON layout rules before the general candidate scorer."""
    for rule in VENDOR_RULES:
        required_text = tuple(rule["required_text"])
        for page_number, lines in enumerate(page_lines, start=1):
            for index, line in enumerate(lines):
                if not all(text.casefold() in line.casefold() for text in required_text):
                    continue

                look_ahead = rule["look_ahead_lines"]
                if rule["strategy"] == "next_standalone_amount":
                    for next_line in lines[index + 1 : index + 1 + look_ahead]:
                        if STANDALONE_AMOUNT_RE.fullmatch(next_line):
                            values = amounts_in(next_line)
                            if len(values) == 1:
                                return format_amount(values[0]), rule["score"], f"{rule['name']} on page {page_number}", rule["status"]

                fallback = rule.get("fallback")
                if fallback and fallback["strategy"] == "base_plus_following_amount":
                    base_values = amounts_in(line)
                    if not base_values:
                        continue
                    for next_line in lines[index + 1 : index + 1 + fallback["look_ahead_lines"]]:
                        if fallback["following_text"].casefold() in next_line.casefold():
                            following_values = amounts_in(next_line)
                            if following_values:
                                total = base_values[-1] + following_values[-1]
                                return format_amount(total), fallback["score"], f"{rule['name']} fallback on page {page_number}", fallback["status"]
    return None


def extract_total(page_lines):
    rule_result = apply_vendor_rules(page_lines)
    if rule_result is not None:
        return rule_result

    candidates = monetary_candidates(page_lines)
    if not candidates:
        return "Not Found", 0, "No numeric money candidates", "Needs review"

    duplicate_counts = Counter(round(candidate.amount, 2) for candidate in candidates)
    for candidate in candidates:
        score_candidate(candidate, page_lines[candidate.page_number - 1], duplicate_counts)

    best = max(candidates, key=lambda candidate: (candidate.score, candidate.page_number, candidate.line_index))
    evidence = "; ".join(best.evidence)
    if best.score >= THRESHOLDS["confirmed"]:
        status = "Confirmed"
    elif best.score >= THRESHOLDS["review"]:
        status = "Review suggested amount"
    else:
        return "Not Found", best.score, evidence, "Needs review"
    return format_amount(best.amount), best.score, evidence, status


def extract_vendor(page_lines):
    lines = [line for page in page_lines for line in page]
    seller_pattern = re.compile(r"(?:" + "|".join(re.escape(label) for label in HINTS["seller_labels"]) + r")\s*:\s*(.+)", re.IGNORECASE)
    for line in lines:
        match = seller_pattern.search(line)
        if match:
            return match.group(1).strip()[:60]
    for line in lines:
        if line.casefold() not in set(HINTS["ignored_headings"]) and len(line) > 2:
            return line[:60]
    return "Unknown Vendor"


def extract_dates(page_lines):
    text = "\n".join(line for page in page_lines for line in page)
    all_dates = list(dict.fromkeys(DATE_RE.findall(text)))
    for page in page_lines:
        for index, line in enumerate(page):
            if any(label in line.casefold() for label in HINTS["date_labels"]):
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
