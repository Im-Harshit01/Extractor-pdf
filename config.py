import json
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
RULES_FILE = SCRIPT_DIR / "receipt_rules.json"


def load_rules():
    try:
        with RULES_FILE.open(encoding="utf-8") as rules_file:
            rules = json.load(rules_file)

    except FileNotFoundError as error:
        raise RuntimeError(
            f"Missing configuration file: {RULES_FILE}"
        ) from error

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid JSON in {RULES_FILE}: "
            f"{error.msg} at line {error.lineno}"
        ) from error

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
            raise RuntimeError(
                f"receipt_rules.json needs a "
                f"'{name}' {expected_type.__name__}"
            )

    required_patterns = {
        "currency",
        "amount",
        "date",
        "phone",
        "standalone_amount",
        "invoice_number",
    }

    missing_patterns = required_patterns - set(rules["patterns"])

    if missing_patterns:
        raise RuntimeError(
            "receipt_rules.json is missing patterns: "
            + ", ".join(sorted(missing_patterns))
        )

    if len(rules["output_fields"]) != 9:
        raise RuntimeError(
            "receipt_rules.json needs exactly 9 output_fields"
        )

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