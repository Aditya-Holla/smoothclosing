"""
standardize_sheets.py
---------------------
One-shot script to make all Name cells in both Google Sheets visually
consistent and Title-Cased.

What it touches:
  - Acquisitions sheet: column B (owner Name) and column Q (traced Name)
  - Dispositions sheet: columns C/D/E/F (Name 1/2/3/4) on every metro tab

What it does:
  1. Read every Name cell across all sheets/tabs
  2. Convert to Title Case using utils.title_case_name (preserves LLC,
     Inc, LP, "and", McSomething, etc.)
  3. Write changes back ONLY for cells whose value would actually change
  4. Apply consistent visual formatting:
       - Font: Arial 10pt, dark text
       - Owner rows (Acquisitions col B, Dispositions any Name col): BOLD
       - Relative rows in Acquisitions col Q (rows where col B is empty): regular
       - Same horizontal alignment (left), same vertical (middle)

Safe to re-run — idempotent. Uses --dry-run to preview.

Usage:
    python standardize_sheets.py --dry-run     # preview, no writes
    python standardize_sheets.py               # apply
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _col_letter(n: int) -> str:
    """1-indexed column number -> Excel letter (1=A, 27=AA)."""
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


# Visual style is defined once in sheets_exporter.py so the on-push
# auto-formatter and this one-off cleanup stay in lock-step.
from sheets_exporter import NAME_FORMAT_REGULAR, NAME_FORMAT_BOLD  # noqa: E402


def standardize_acquisitions(client, dry_run: bool = False) -> dict:
    """Standardize the Acquisitions sheet's Name columns (B and Q)."""
    from utils import title_case_name

    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    ws = client.open_by_key(sheet_id).sheet1
    rows = ws.get_all_values()
    if not rows:
        return {"text_updates": 0, "format_owner_rows": 0, "format_relative_rows": 0}

    OWNER_COL = 2     # col B (Name — owner)
    CONTACT_COL = 17  # col Q (Name — traced/relative)
    OWNER_LETTER = _col_letter(OWNER_COL)
    CONTACT_LETTER = _col_letter(CONTACT_COL)

    text_updates = []
    bold_ranges = []     # owner rows: bold name cells
    regular_ranges = []  # relative rows in col Q

    for i, row in enumerate(rows[1:], start=2):  # 1-indexed; skip header
        owner_val = row[OWNER_COL - 1] if OWNER_COL - 1 < len(row) else ""
        contact_val = row[CONTACT_COL - 1] if CONTACT_COL - 1 < len(row) else ""
        is_owner_row = bool(owner_val.strip())  # owner row has col B filled

        # Title-case both name cells
        for col_letter, current in [(OWNER_LETTER, owner_val), (CONTACT_LETTER, contact_val)]:
            new = title_case_name(current)
            if new != current:
                text_updates.append({
                    "range": f"{col_letter}{i}",
                    "values": [[new]],
                })

        # Visual format: owner rows get bold on both Name cells; relative
        # rows (col B empty) get regular weight on col Q (their only Name).
        if is_owner_row:
            bold_ranges.append(f"{OWNER_LETTER}{i}")
            bold_ranges.append(f"{CONTACT_LETTER}{i}")
        elif contact_val.strip():
            regular_ranges.append(f"{CONTACT_LETTER}{i}")

    logger.info(
        "Acquisitions: %d text changes; %d cells -> bold; %d cells -> regular",
        len(text_updates), len(bold_ranges), len(regular_ranges),
    )

    if not dry_run:
        from gspread.utils import ValueInputOption
        if text_updates:
            ws.batch_update(text_updates, value_input_option=ValueInputOption.user_entered)
        # batch_format sends ALL formatting in one API call. Don't loop
        # per-range with ws.format() - that hits the 60/min write quota
        # almost instantly with a few hundred cells.
        format_batch = []
        for rng in bold_ranges:
            format_batch.append({"range": rng, "format": NAME_FORMAT_BOLD})
        for rng in regular_ranges:
            format_batch.append({"range": rng, "format": NAME_FORMAT_REGULAR})
        if format_batch:
            ws.batch_format(format_batch)

    return {
        "text_updates": len(text_updates),
        "format_owner_rows": len(bold_ranges) // 2,
        "format_relative_rows": len(regular_ranges),
    }


def standardize_dispositions(client, dry_run: bool = False) -> dict:
    """Standardize Dispositions Name 1-4 columns across every metro tab."""
    from utils import title_case_name

    sheet_id = os.environ["DISPOSITIONS_SHEET_ID"]
    sheet = client.open_by_key(sheet_id)
    summary = {"tabs_processed": 0, "text_updates": 0, "format_cells": 0}

    for ws in sheet.worksheets():
        rows = ws.get_all_values()
        if not rows:
            continue
        header = rows[0]
        # Find Name 1-4 columns by header (handles future column changes)
        name_cols = []
        for n in ("Name 1", "Name 2", "Name 3", "Name 4"):
            for i, h in enumerate(header):
                if h.strip() == n:
                    name_cols.append((n, i + 1))  # 1-indexed
                    break
        if not name_cols:
            logger.warning("  %s: no Name columns found, skipping.", ws.title)
            continue

        text_updates = []
        bold_ranges = []  # all dispo Name cells are "owner" equivalent — bold

        for i, row in enumerate(rows[1:], start=2):
            for label, col in name_cols:
                col_letter = _col_letter(col)
                current = row[col - 1] if col - 1 < len(row) else ""
                new = title_case_name(current)
                if new != current:
                    text_updates.append({
                        "range": f"{col_letter}{i}",
                        "values": [[new]],
                    })
                if current.strip() or new:
                    bold_ranges.append(f"{col_letter}{i}")

        logger.info(
            "  %s: %d text changes; %d cells -> bold",
            ws.title, len(text_updates), len(bold_ranges),
        )

        if not dry_run:
            from gspread.utils import ValueInputOption
            if text_updates:
                ws.batch_update(text_updates, value_input_option=ValueInputOption.user_entered)
            # Single batch_format call (see Acquisitions function for why)
            if bold_ranges:
                ws.batch_format([
                    {"range": rng, "format": NAME_FORMAT_BOLD}
                    for rng in bold_ranges
                ])

        summary["tabs_processed"] += 1
        summary["text_updates"] += len(text_updates)
        summary["format_cells"] += len(bold_ranges)

    return summary


def main():
    parser = argparse.ArgumentParser(description="Standardize Name cells in both Google Sheets")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change, don't write to sheets.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from sheets_exporter import _get_client
    client = _get_client()

    prefix = "[DRY RUN] " if args.dry_run else ""
    logger.info("%sStandardizing Acquisitions sheet...", prefix)
    acq = standardize_acquisitions(client, dry_run=args.dry_run)
    logger.info("%sStandardizing Dispositions sheet...", prefix)
    dispo = standardize_dispositions(client, dry_run=args.dry_run)

    print()
    print(f"{prefix}Done.")
    print(f"  Acquisitions: {acq['text_updates']} text changes, "
          f"{acq['format_owner_rows']} owner rows bolded, "
          f"{acq['format_relative_rows']} relative rows formatted regular")
    print(f"  Dispositions: {dispo['tabs_processed']} tabs, "
          f"{dispo['text_updates']} text changes, "
          f"{dispo['format_cells']} cells formatted")


if __name__ == "__main__":
    main()
