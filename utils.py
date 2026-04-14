"""
utils.py
--------
Tiny shared utilities used across the pipeline. Keep this file small —
only put things here when there are 2+ callers AND the logic should be
identical across them. Anything more specialized belongs in the module
that uses it.
"""

import re


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
