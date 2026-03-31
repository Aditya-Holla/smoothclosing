"""
equity_estimator.py
-------------------
Estimates owner equity based on:
  - Original loan amount (from the foreclosure notice)
  - Year the deed of trust was recorded (proxy for purchase year)
  - Historical average 30-year fixed mortgage rate for that year
  - Assumed loan term: 30 years
  - Months elapsed since origination

Equity = Estimated Current Home Value - Remaining Loan Balance

NOTE on home value: we cannot pull a live AVM here without an external API
(Zillow, ATTOM, Redfin etc.). The column is left blank for you to fill, or
wire in an API call in _estimate_home_value() below.

Usage:
    python equity_estimator.py                 # reads leads.csv, writes leads_with_equity.csv
    python equity_estimator.py --input leads.csv --output leads_with_equity.csv
"""

import argparse
import csv
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Historical average 30-year fixed mortgage rates by year (Freddie Mac PMMS)
# Sources: Freddie Mac Primary Mortgage Market Survey
# ---------------------------------------------------------------------------
HISTORICAL_RATES: dict[int, float] = {
    1990: 10.13, 1991: 9.25,  1992: 8.39,  1993: 7.31,  1994: 8.38,
    1995: 7.93,  1996: 7.81,  1997: 7.60,  1998: 6.94,  1999: 7.44,
    2000: 8.05,  2001: 6.97,  2002: 6.54,  2003: 5.83,  2004: 5.84,
    2005: 5.87,  2006: 6.41,  2007: 6.34,  2008: 6.03,  2009: 5.04,
    2010: 4.69,  2011: 4.45,  2012: 3.66,  2013: 3.98,  2014: 4.17,
    2015: 3.85,  2016: 3.65,  2017: 3.99,  2018: 4.54,  2019: 3.94,
    2020: 3.11,  2021: 2.96,  2022: 5.34,  2023: 6.81,  2024: 6.72,
    2025: 6.85,  2026: 6.90,
}
DEFAULT_RATE = 6.50  # fallback if year not in table


# ---------------------------------------------------------------------------
# Amortization math
# ---------------------------------------------------------------------------

def monthly_payment(principal: float, annual_rate_pct: float, months: int) -> float:
    """Standard amortization monthly payment formula."""
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal / months
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


def remaining_balance(principal: float, annual_rate_pct: float,
                      total_months: int, elapsed_months: int) -> float:
    """
    Remaining principal balance after `elapsed_months` payments
    on a `total_months` amortizing loan.
    """
    if elapsed_months >= total_months:
        return 0.0
    r = annual_rate_pct / 100 / 12
    pmt = monthly_payment(principal, annual_rate_pct, total_months)
    if r == 0:
        return principal - pmt * elapsed_months
    balance = principal * (1 + r) ** elapsed_months - pmt * ((1 + r) ** elapsed_months - 1) / r
    return max(balance, 0.0)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_loan_amount(value: str) -> Optional[float]:
    """Parse '$112,917.00' → 112917.0"""
    cleaned = re.sub(r"[^\d.]", "", value)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_year(date_str: str) -> Optional[int]:
    """
    Extract a 4-digit year from various date formats:
      '10/30/2014', 'August 21, 2013', 'January 27, 2022', 'September 18, 2018'
    """
    if not date_str:
        return None
    # Look for a 4-digit year anywhere in the string
    match = re.search(r"\b(19|20)\d{2}\b", date_str)
    if match:
        return int(match.group())
    return None


def _enrich_from_rentcast(record: dict) -> dict:
    """
    Calls RentCast for both AVM value and property details.
    Returns a dict with all enrichment fields (empty strings on failure).
    """
    empty = {
        "rc_bedrooms": "", "rc_bathrooms": "", "rc_square_feet": "",
        "rc_lot_size_sqft": "", "rc_year_built": "", "rc_property_type": "",
        "rc_last_sale_price": "", "rc_last_sale_date": "",
        "rc_tax_assessed_value": "", "rc_county": "",
        "estimated_home_value_num": None,
    }
    address = record.get("property_address", "").strip()
    if not address:
        return empty

    try:
        from rentcast_client import get_property_value, get_property_details
    except ImportError:
        logger.warning("rentcast_client not found — skipping enrichment.")
        return empty

    # Property characteristics
    details = get_property_details(address) or {}
    result = {k: (str(v) if v is not None else "") for k, v in {
        "rc_bedrooms":           details.get("rc_bedrooms"),
        "rc_bathrooms":          details.get("rc_bathrooms"),
        "rc_square_feet":        details.get("rc_square_feet"),
        "rc_lot_size_sqft":      details.get("rc_lot_size_sqft"),
        "rc_year_built":         details.get("rc_year_built"),
        "rc_property_type":      details.get("rc_property_type"),
        "rc_last_sale_price":    (f"${details['rc_last_sale_price']:,.0f}"
                                  if details.get("rc_last_sale_price") else None),
        "rc_last_sale_date":     details.get("rc_last_sale_date"),
        "rc_tax_assessed_value": (f"${details['rc_tax_assessed_value']:,.0f}"
                                  if details.get("rc_tax_assessed_value") else None),
        "rc_county":             details.get("rc_county"),
    }.items()}

    # AVM value
    home_value = get_property_value(address)
    result["estimated_home_value_num"] = home_value
    return result


# ---------------------------------------------------------------------------
# Core equity calculation
# ---------------------------------------------------------------------------

# Fields that, if blank, should trigger a manual review flag
_REQUIRED_FIELDS = ["owner_name", "property_address", "sale_date", "lender", "loan_amount", "filing_date"]


def _needs_review(record: dict, equity_computed: bool) -> str:
    """
    Return a pipe-separated list of reasons to manually check this row,
    or empty string if everything looks complete.
    """
    reasons = []
    for field in _REQUIRED_FIELDS:
        if not record.get(field, "").strip():
            reasons.append(f"missing {field}")
    if not equity_computed:
        reasons.append("equity not estimated")
    return " | ".join(reasons)


def calculate_equity(record: dict) -> dict:
    """
    Given a lead record, compute equity fields and return an enriched dict.

    Added fields:
      origination_year    — year extracted from filing_date
      interest_rate_pct   — historical 30yr rate for that year
      loan_amount_num     — numeric loan amount
      elapsed_months      — months since origination (as of today)
      remaining_balance   — estimated remaining loan principal
      estimated_home_value — blank; fill via AVM / RentCast
      estimated_equity    — estimated_home_value - remaining_balance (if available)
      equity_note         — human-readable explanation
      needs_review        — reasons to manually check, or blank if complete
    """
    today = datetime.today()
    loan_str = record.get("loan_amount", "")
    date_str = record.get("filing_date", "")

    loan_amount = _parse_loan_amount(loan_str)
    year = _parse_year(date_str)

    result = dict(record)
    result["origination_year"] = str(year) if year else ""
    result["interest_rate_pct"] = ""
    result["loan_amount_num"] = ""
    result["elapsed_months"] = ""
    result["remaining_balance"] = ""
    result["estimated_home_value"] = ""
    result["estimated_equity"] = ""
    result["equity_note"] = ""
    result["needs_review"] = ""
    # Property detail columns (filled later via RentCast)
    for col in ["rc_property_type", "rc_bedrooms", "rc_bathrooms", "rc_square_feet",
                "rc_lot_size_sqft", "rc_year_built", "rc_county",
                "rc_last_sale_date", "rc_last_sale_price", "rc_tax_assessed_value"]:
        result[col] = ""

    equity_computed = False

    if not loan_amount:
        result["equity_note"] = "No loan amount — cannot calculate balance"
        logger.warning(f"  [{record.get('source_file')}] No loan amount for {record.get('owner_name')}")
        # Still enrich property details even if we can't compute balance
        enrichment = _enrich_from_rentcast(result)
        for col in ["rc_property_type", "rc_bedrooms", "rc_bathrooms", "rc_square_feet",
                    "rc_lot_size_sqft", "rc_year_built", "rc_county",
                    "rc_last_sale_date", "rc_last_sale_price", "rc_tax_assessed_value"]:
            result[col] = enrichment.get(col, "")
        home_value = enrichment.get("estimated_home_value_num")
        result["estimated_home_value"] = f"${home_value:,.2f}" if home_value else ""
        result["needs_review"] = _needs_review(result, equity_computed)
        return result

    if not year:
        result["equity_note"] = "No origination year — cannot calculate balance"
        logger.warning(f"  [{record.get('source_file')}] No year in filing_date: '{date_str}'")
        # Still enrich property details even if we can't compute balance
        enrichment = _enrich_from_rentcast(result)
        for col in ["rc_property_type", "rc_bedrooms", "rc_bathrooms", "rc_square_feet",
                    "rc_lot_size_sqft", "rc_year_built", "rc_county",
                    "rc_last_sale_date", "rc_last_sale_price", "rc_tax_assessed_value"]:
            result[col] = enrichment.get(col, "")
        home_value = enrichment.get("estimated_home_value_num")
        result["estimated_home_value"] = f"${home_value:,.2f}" if home_value else ""
        result["needs_review"] = _needs_review(result, equity_computed)
        return result

    rate = HISTORICAL_RATES.get(year, DEFAULT_RATE)
    # Origination assumed to be Jan 1 of the origination year (conservative)
    origination_date = datetime(year, 1, 1)
    elapsed = (today.year - origination_date.year) * 12 + (today.month - origination_date.month)
    elapsed = max(elapsed, 0)

    loan_term_months = 360  # 30-year
    bal = remaining_balance(loan_amount, rate, loan_term_months, elapsed)

    result["interest_rate_pct"] = f"{rate:.2f}%"
    result["loan_amount_num"] = f"${loan_amount:,.2f}"
    result["elapsed_months"] = str(elapsed)
    result["remaining_balance"] = f"${bal:,.2f}"

    # Pull property details + AVM from RentCast
    enrichment = _enrich_from_rentcast(result)
    result["rc_bedrooms"]           = enrichment["rc_bedrooms"]
    result["rc_bathrooms"]          = enrichment["rc_bathrooms"]
    result["rc_square_feet"]        = enrichment["rc_square_feet"]
    result["rc_lot_size_sqft"]      = enrichment["rc_lot_size_sqft"]
    result["rc_year_built"]         = enrichment["rc_year_built"]
    result["rc_property_type"]      = enrichment["rc_property_type"]
    result["rc_last_sale_price"]    = enrichment["rc_last_sale_price"]
    result["rc_last_sale_date"]     = enrichment["rc_last_sale_date"]
    result["rc_tax_assessed_value"] = enrichment["rc_tax_assessed_value"]
    result["rc_county"]             = enrichment["rc_county"]

    home_value = enrichment["estimated_home_value_num"]
    if home_value:
        equity = home_value - bal
        result["estimated_home_value"] = f"${home_value:,.2f}"
        result["estimated_equity"]     = f"${equity:,.2f}"
        result["equity_note"] = (
            f"AVM=${home_value:,.0f} − balance=${bal:,.0f} = equity=${equity:,.0f}"
        )
        equity_computed = True
    else:
        result["estimated_home_value"] = ""
        result["estimated_equity"]     = ""
        result["equity_note"] = (
            f"Balance after {elapsed}mo at {rate:.2f}% = ${bal:,.2f}. "
            f"AVM lookup failed — add home value manually to compute equity."
        )

    logger.info(
        f"  {record.get('owner_name')}: "
        f"loan=${loan_amount:,.0f} @ {rate:.2f}% → {elapsed}mo → "
        f"balance=${bal:,.0f}" +
        (f" | AVM=${home_value:,.0f} | equity=${home_value - bal:,.0f}" if home_value else "")
    )
    result["needs_review"] = _needs_review(result, equity_computed)
    return result


# ---------------------------------------------------------------------------
# CSV pipeline
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS = [
    # Lead info from PDF
    "owner_name", "property_address", "mailing_address",
    "filing_date", "sale_date", "lender", "case_number",
    "loan_amount", "source_file",
    # Property details from RentCast
    "rc_property_type", "rc_bedrooms", "rc_bathrooms",
    "rc_square_feet", "rc_lot_size_sqft", "rc_year_built",
    "rc_county", "rc_last_sale_date", "rc_last_sale_price",
    "rc_tax_assessed_value",
    # Equity calculation
    "origination_year", "interest_rate_pct", "loan_amount_num",
    "elapsed_months", "remaining_balance",
    "estimated_home_value", "estimated_equity",
    "equity_note",
    # Review flag
    "needs_review",
]


def run(input_csv: str, output_csv: str) -> None:
    in_path = Path(input_csv)
    out_path = Path(output_csv)

    if not in_path.exists():
        logger.error(f"Input file not found: {in_path}")
        sys.exit(1)

    with open(in_path, newline="", encoding="utf-8") as f:
        records = list(csv.DictReader(f))

    logger.info(f"Read {len(records)} record(s) from '{in_path}'")
    enriched = [calculate_equity(r) for r in records]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(enriched)

    logger.info(f"Wrote {len(enriched)} record(s) to '{out_path}'")
    print(f"\n✓ Equity estimates written → {out_path.resolve()}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Equity Estimator for Foreclosure Leads")
    parser.add_argument("--input",  default="leads.csv",             help="Input CSV (default: leads.csv)")
    parser.add_argument("--output", default="leads_with_equity.csv", help="Output CSV (default: leads_with_equity.csv)")
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    run(args.input, args.output)


if __name__ == "__main__":
    main()
