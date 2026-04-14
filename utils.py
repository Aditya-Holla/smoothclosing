"""
utils.py
--------
Tiny shared utilities used across the pipeline. Keep this file small —
only put things here when there are 2+ callers AND the logic should be
identical across them. Anything more specialized belongs in the module
that uses it.
"""

import re


# Tokens that should NOT be Title-cased — keep them as-is or lowercase.
# Entity suffixes and Roman numerals stay uppercase. "Sr"/"Jr" are NOT
# in this set on purpose — convention is Title Case ("Sr", "Jr"), not
# "SR"/"JR" — they get the default capitalize-first-letter treatment.
_UPPERCASE_TOKENS = {
    "LLC", "L.L.C.", "L.L.C", "LLP", "LP", "L.P.", "INC", "INC.",
    "LTD", "LTD.", "CO", "CO.", "PLLC", "II", "III", "IV",
    "DBA", "USA", "DDS", "MD", "PA", "PC",
    # Legal abbreviations that contain slashes — keep uppercase
    "A/K/A", "F/K/A", "D/B/A", "N/K/A",
}
# Words that stay lowercase only in mid-string (not when first word).
# "and" is intentionally NOT here — couple owner names like
# "Javier Delgadillo And Elissa M Anzures" already use capital "And"
# throughout the existing sheet. Keep that convention.
_LOWERCASE_TOKENS = {
    "of", "the", "for", "de", "del", "la", "le", "von", "van",
    "der", "den", "da", "di", "du", "el",
}


def title_case_name(name: str) -> str:
    """Convert a name to consistent Title Case while preserving entity
    suffixes (LLC, Inc, LP, etc.) and lowercase joiners (and, de, van).

    Examples:
        'NELL HOLLAND'              -> 'Nell Holland'
        'darrell bible AND robin'   -> 'Darrell Bible and Robin'
        'DZM CONCEPTS LLC'          -> 'Dzm Concepts LLC'
        'Hcv Partners Llc'          -> 'Hcv Partners LLC'
        'BRIAN HOLLAND II'          -> 'Brian Holland II'
        ''                          -> ''

    Edge cases handled:
      - Hyphens stay hyphenated, each segment titled ('mary-jo' -> 'Mary-Jo')
      - Apostrophes preserved ("o'connor" -> "O'Connor")
      - Names starting with Mc/Mac get the next letter capitalized
      - Sentinel non-word strings like 'NaN', '(no name)' -> empty result
    """
    if not name:
        return ""
    s = str(name).strip()
    if not s or s.lower() == "nan":
        return ""
    # Skip strings that don't have any letters (phone numbers, etc.)
    if not any(c.isalpha() for c in s):
        return s

    def _fix_token(tok: str, is_first: bool) -> str:
        if not tok:
            return tok
        upper = tok.upper().rstrip(",")
        # Entity suffixes / Roman numerals — uppercase no matter what
        if upper in _UPPERCASE_TOKENS:
            return upper + ("," if tok.endswith(",") else "")
        lower = tok.lower()
        # Joiner words ("and", "de", "van") — lowercase, EXCEPT when they
        # start the whole name (capitalize first word always)
        if lower in _LOWERCASE_TOKENS and not is_first:
            return lower
        # Hyphenated names
        if "-" in tok:
            return "-".join(_fix_token(p, False) for p in tok.split("-"))
        # Apostrophes: O'Connor, D'Angelo
        if "'" in tok:
            parts = tok.split("'")
            return "'".join(_fix_token(p, i == 0) for i, p in enumerate(parts))
        # Mc / Mac — capitalize next letter
        low = tok.lower()
        if low.startswith("mc") and len(low) > 2:
            return "Mc" + low[2].upper() + low[3:]
        if low.startswith("mac") and len(low) > 3 and low[3] not in "aeiou":
            return "Mac" + low[3].upper() + low[4:]
        # Default: capitalize first letter
        return tok[0].upper() + tok[1:].lower()

    tokens = s.split()
    result = " ".join(_fix_token(t, i == 0) for i, t in enumerate(tokens))
    return result


def normalize_phone(num: str) -> str:
    """Strip a phone number to its last 10 digits for consistent matching.

    Used as the canonical key in sms_history.csv and for cross-checking
    phone numbers between any two data sources (sheet rows, traced CSVs,
    SMS history). Handles common formats:
        '(512) 555-1234'  -> '5125551234'
        '+1 512 555 1234' -> '5125551234'
        '5125551234'      -> '5125551234'
        '1-512-555-1234'  -> '5125551234'
        ''                -> ''
        'nan'             -> ''           (pandas float artifact)
        '512555123'       -> '512555123'  (short, returned as-is)

    Numbers with fewer than 10 digits are returned as-is rather than
    truncated, so partially-entered data still has a stable key.
    """
    digits = re.sub(r"\D", "", num or "")
    return digits[-10:] if len(digits) >= 10 else digits
