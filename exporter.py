"""
exporter.py
-----------
Exports validated records to a CSV file.
"""

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "owner_name",
    "property_address",
    "mailing_address",
    "filing_date",
    "sale_date",
    "lender",
    "case_number",
    "loan_amount",
    "source_file",
]


def export_to_csv(records: list[dict], output_path: str, append: bool = False) -> None:
    """
    Write a list of cleaned record dicts to a CSV file.

    Args:
        records:     List of cleaned record dicts.
        output_path: Destination file path (e.g. "leads.csv").
        append:      If True and file exists, append without rewriting header.
    """
    if not records:
        logger.warning("Exporter: no records to export — CSV not written.")
        return

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if append and out.exists():
        with open(out, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writerows(records)
        logger.info(f"Exporter: appended {len(records)} record(s) to '{out}'")
        print(f"\n✓ Appended {len(records)} new lead(s) → {out.resolve()}")
    else:
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
        logger.info(f"Exporter: wrote {len(records)} record(s) to '{out}'")
        print(f"\n✓ Exported {len(records)} lead(s) → {out.resolve()}")
