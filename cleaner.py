"""
cleaner.py
----------
Normalizes and validates raw parsed records before export.

Rules applied:
  - Strip extra whitespace from all string fields
  - Title-case names; uppercase addresses
  - Normalize dollar amounts (remove stray characters)
  - Reject records with obviously bad owner names (boilerplate, OCR garbage)
  - Reject records with bogus addresses (attorney offices, garbage text)
  - Clean lender field to remove captured boilerplate
  - Drop records missing owner_name (critical field)
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

CRITICAL_FIELDS = ("owner_name",)

TEXAS_STATE_ABBREVS = re.compile(r"\bTEXAS\b", re.IGNORECASE)

# --- Owner name validation ---
# Phrases that indicate the regex captured boilerplate instead of an actual name
_BAD_NAME_PHRASES = re.compile(
    r'\b(?:'
    r'deed\s+of\s+trust|terms\s+of\s+(?:said|sale)|requirements\s+of|'
    r'collection\s+of\s+this|notice\s+(?:of|is)|foreclosure|'
    r'obligations?\s+secured|now\s+(?:therefore|has\s+in)|'
    r'pursuant\s+to|mortgage\s+servi|in\s+(?:accordance|connection)|'
    r'property\s+(?:code|to\s+be)|instrument\s+to|'
    r'assert\s+and\s+protect|armed\s+forces|military\s+duty|'
    r'substitute\s+trustee|appointment\s+of|'
    r'bell\s+county|hays\s+county|travis\s+county|williamson\s+county'
    r')\b',
    re.IGNORECASE,
)

# Names that are clearly lender/servicer names mistakenly captured as owner
_LENDER_AS_OWNER = re.compile(
    r'^(?:Lakeview\s+Loan|Freedom\s+Mortgage|Phh\s+Mortgage|Pennymac|'
    r'Wells\s+Fargo|Rocket\s+Mortgage|Nationstar|Newrez|Truist|'
    r'Carrington\s+Mortgage|United\s+States\s+Of\s+America|'
    r'Pnc\s+Bank|Midfirst\s+Bank)',
    re.IGNORECASE,
)

# --- Lender field validation ---
# Phrases that indicate the regex captured boilerplate instead of a lender name
_BAD_LENDER_PHRASES = re.compile(
    r'^(?:Of\s+The\s+Deed|Pursuant\s+To|An\s+Officer|'
    r'Under\s+(?:The|A)|Extensions\s+Of|'
    r'Rtgage\s+Servic|Rtgagee|'
    r'In\s+Connection\s+With|'
    r'Original\s+Principal|Or\s+Mortgage\s+Servicer|'
    r'Beneficiary$|Lender$|'
    # Additional patterns seen in output
    r'Of\s+The\s+Deed|'  # "Of The Deed Of Trust And Note..."
    r'The\s+Deed\s+Of|'  # "The Deed Of Trust..."
    r'Allen\s+Patterson,\s+Pursuant)',  # specific false match
    re.IGNORECASE,
)

# --- Address validation ---
_BAD_ADDRESS_PHRASES = re.compile(
    r'NOTICE\s+OF|PAVE\s+EB|AUTHORIZES\s+THE|'
    r'MORTGAGE\s+SERVICER|FORECLOSURE',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Field-level normalizers
# ---------------------------------------------------------------------------

def _clean_str(value: Optional[str]) -> str:
    """Strip and collapse internal whitespace."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip())


def _normalize_name(name: Optional[str]) -> str:
    """Clean and title-case an owner name."""
    name = _clean_str(name)
    if not name:
        return ""
    # Strip OCR artifacts: stray braces, equals signs, copyright symbols
    name = re.sub(r"[{}=©@]", " ", name)
    # Remove leading "And " (OCR artifact from multi-grantor splits)
    name = re.sub(r"^And\s+", "", name, flags=re.IGNORECASE)
    # Remove trailing noise words that leaked in via greedy regex (loop until stable)
    _TRAILING_NOISE = re.compile(
        r"[\s,]+(?:and|or|as|the|a|an|husband|wife|single|woman|man|unmarried|married"
        r"|that\s+it|provides|secures?|securing|its|joint\s+tenants?)\s*$",
        re.IGNORECASE,
    )
    for _ in range(6):
        cleaned = _TRAILING_NOISE.sub("", name).strip()
        if cleaned == name:
            break
        name = cleaned
    # Remove trailing punctuation artifacts
    name = re.sub(r"[,;:.]+$", "", name).strip()
    # Fix common OCR word-split artifacts: standalone "Nd" → "And"
    name = re.sub(r"\bNd\b", "And", name, flags=re.IGNORECASE)
    # Fix lowercase "rand" as connector between name parts (OCR for "and")
    # Only replace when lowercase — "Rand" as a proper name stays unchanged
    name = re.sub(r"\brand\b", "And", name)
    # Collapse whitespace again after substitutions
    name = re.sub(r"\s+", " ", name).strip()
    return name.title()


def _validate_name(name: str) -> bool:
    """Return True if the name looks like a real person/entity name, not boilerplate."""
    if not name:
        return False
    # Reject names containing legal boilerplate phrases
    if _BAD_NAME_PHRASES.search(name):
        return False
    # Reject known lender/servicer names captured as owner
    if _LENDER_AS_OWNER.match(name):
        return False
    # Reject LLC/Corp/Inc entities — we only want individual homeowners
    if re.search(r'\b(?:LLC|L\.L\.C|Inc\.?|Corp\.?|Ltd\.?|Holdings|Investments|Properties|Ventures|Enterprises|Trust|Foundation|Association)\b', name, re.IGNORECASE):
        return False
    # Reject names that are too short (single short word, not an LLC/Corp/Inc)
    words = name.split()
    if len(words) == 1 and len(name) < 5 and not re.search(r'(?:llc|inc|corp|ltd)\b', name, re.IGNORECASE):
        return False
    # Reject names that are mostly non-alpha (OCR garbage)
    alpha_chars = sum(1 for c in name if c.isalpha())
    if alpha_chars < len(name) * 0.5:
        return False
    return True


def _normalize_address(addr: Optional[str]) -> str:
    """
    Basic address normalization:
      - Uppercase
      - Collapse whitespace
      - Standardize Texas state abbreviation
      - Strip OCR barcode noise (long digit sequences embedded mid-address)
      - Reject garbage addresses
    """
    addr = _clean_str(addr)
    if not addr:
        return ""
    addr = addr.upper()
    # Remove OCR barcode / tracking numbers (6+ consecutive digits not at start)
    addr = re.sub(r'(?<!\A)\b\d{6,}\b', '', addr)
    addr = TEXAS_STATE_ABBREVS.sub("TX", addr)
    # Remove duplicate commas and collapse whitespace
    addr = re.sub(r",\s*,", ",", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    # Remove "IN THE COUNTY OF ..." suffix
    addr = re.sub(r'\s+IN\s+THE\s+COUNTY\s+OF\s+\w+\s*$', '', addr).strip()
    # Reject addresses containing garbage / boilerplate text
    if _BAD_ADDRESS_PHRASES.search(addr):
        logger.debug(f"  Cleaner: rejecting garbage address: '{addr}'")
        return ""
    return addr


def _normalize_date(date: Optional[str]) -> str:
    """Strip and return date string as-is (no reformatting in MVP)."""
    return _clean_str(date)


def _normalize_loan_amount(amount: Optional[str]) -> str:
    """Remove non-numeric chars except decimal point; re-add $ prefix."""
    amount = _clean_str(amount)
    if not amount:
        return ""
    # Keep only digits, commas, and decimal
    cleaned = re.sub(r"[^\d,.]", "", amount)
    return f"${cleaned}" if cleaned else ""


def _normalize_lender(lender: Optional[str]) -> str:
    """Clean lender name, rejecting boilerplate captures."""
    lender = _clean_str(lender)
    if not lender:
        return ""
    lender = re.sub(r"[,;.]+$", "", lender).strip()
    # Reject if it's clearly boilerplate
    if _BAD_LENDER_PHRASES.match(lender):
        logger.debug(f"  Cleaner: rejecting lender boilerplate: '{lender}'")
        return ""
    # Strip trailing boilerplate fragments that leaked in
    # e.g. "Freedom Mortgage Corporation Is The Current" → "Freedom Mortgage Corporation"
    lender = re.sub(
        r'\s+(?:Is\s+The\s+Current|Is\s+Representing|'
        r'Is\s+Acting|Is\s+Mortgage|Whose\s+Address|'
        r'And\s+All\s+Other\s+Sums\s+Of)\b.*$',
        '', lender, flags=re.IGNORECASE
    ).strip()
    lender = re.sub(r"[,;.]+$", "", lender).strip()
    return lender.title()


def _normalize_case_number(case: Optional[str]) -> str:
    return _clean_str(case).upper()


# ---------------------------------------------------------------------------
# Record-level cleaning
# ---------------------------------------------------------------------------

def clean_record(record: dict) -> Optional[dict]:
    """
    Normalize all fields in a single record.

    Returns None if the record is missing any critical field.
    """
    cleaned = {
        "owner_name":       _normalize_name(record.get("owner_name")),
        "property_address": _normalize_address(record.get("property_address")),
        "mailing_address":  _normalize_address(record.get("mailing_address")),
        "filing_date":      _normalize_date(record.get("filing_date")),
        "sale_date":        _normalize_date(record.get("sale_date")),
        "lender":           _normalize_lender(record.get("lender")),
        "case_number":      _normalize_case_number(record.get("case_number")),
        "loan_amount":      _normalize_loan_amount(record.get("loan_amount")),
        "source_file":      _clean_str(record.get("source_file")),
    }

    for field in CRITICAL_FIELDS:
        if not cleaned[field]:
            logger.warning(
                f"  Cleaner: skipping record — missing '{field}' "
                f"(source: {cleaned['source_file']})"
            )
            return None

    # Validate owner name is not boilerplate/garbage
    if not _validate_name(cleaned["owner_name"]):
        logger.warning(
            f"  Cleaner: rejecting bad owner name '{cleaned['owner_name']}' "
            f"(source: {cleaned['source_file']})"
        )
        return None

    return cleaned


def clean_records(records: list[dict]) -> list[dict]:
    """Clean and validate a list of raw records. Returns only valid records."""
    valid = []
    for record in records:
        cleaned = clean_record(record)
        if cleaned:
            valid.append(cleaned)

    logger.info(f"Cleaner: {len(valid)}/{len(records)} records passed validation")
    return valid
