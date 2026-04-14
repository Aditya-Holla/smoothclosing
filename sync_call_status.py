"""
sync_call_status.py
-------------------
Sync `sms_history.csv` into the Google Sheet's "Call Status" column.

For every sheet row whose "Phone Number" appears in sms_history.csv:
  - If Call Status is empty -> fill with "Texted YYYY-MM-DD"
  - If Call Status already has a value -> leave it alone (don't overwrite
    manual entries made by the team)

Usage:
    python sync_call_status.py            # normal run
    python sync_call_status.py --dry-run  # print what would change, no writes

Called from:
    - Acquisitions dashboard "Sync to Sheet" button
    - Ad-hoc after Send Texts if sheet looks stale
"""

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d


def _col_letter(n: int) -> str:
    """0-indexed -> A, B, ..., Z, AA, ..."""
    result = ""
    n += 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def sync(history_csv: str = "sms_history.csv", dry_run: bool = False) -> dict:
    """Sync sms_history.csv -> Google Sheet Call Status.

    Returns a summary dict with counts.
    """
    from sheets_exporter import _get_client

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError(
            "GOOGLE_SHEET_ID not set (check .env). "
            "Cannot sync without knowing which sheet."
        )

    history_path = Path(history_csv)
    if not history_path.exists():
        logger.warning(f"{history_csv} not found -- nothing to sync.")
        return {"updated": 0, "skipped_nonempty": 0, "not_in_sheet": 0}

    # Load history -> { normalized phone -> sent_at timestamp }
    sent_numbers = {}
    with open(history_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            np = _norm_phone(row.get("phone_number", ""))
            if np:
                sent_numbers[np] = row.get("sent_at", "")
    logger.info(f"Loaded {len(sent_numbers)} texted number(s) from {history_csv}")

    # Connect to sheet
    client = _get_client()
    ws = client.open_by_key(sheet_id).sheet1
    rows = ws.get_all_values()
    if not rows:
        logger.warning("Sheet is empty.")
        return {"updated": 0, "skipped_nonempty": 0, "not_in_sheet": 0}

    header = rows[0]

    def last_idx(name):
        idxs = [i for i, h in enumerate(header) if h.strip().lower() == name.lower()]
        return idxs[-1] if idxs else None

    cs_i = last_idx("Call Status")
    ph_i = last_idx("Phone Number")
    if cs_i is None or ph_i is None:
        raise RuntimeError(
            "Sheet is missing 'Call Status' or 'Phone Number' column. "
            f"Found headers: {header}"
        )
    cs_col = _col_letter(cs_i)

    updates = []
    skipped_nonempty = 0
    not_in_sheet = 0
    matched_phones = set()

    for i, r in enumerate(rows[1:], start=2):  # sheet rows are 1-indexed, row 1 = header
        ph = (r[ph_i] if ph_i < len(r) else "").strip()
        if not ph:
            continue
        np = _norm_phone(ph)
        if np not in sent_numbers:
            continue
        matched_phones.add(np)
        current = (r[cs_i] if cs_i < len(r) else "").strip()
        if current:
            skipped_nonempty += 1
            continue
        sent_at_day = sent_numbers[np][:10]  # YYYY-MM-DD
        updates.append({
            "range": f"{cs_col}{i}",
            "values": [[f"Texted {sent_at_day}"]],
        })

    # Numbers in history that never appeared in the sheet (e.g. texted before
    # the lead was added, or trimmed by the team)
    not_in_sheet = len(sent_numbers) - len(matched_phones)

    if dry_run:
        logger.info(f"[DRY RUN] Would update {len(updates)} cell(s) in Call Status.")
    elif updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        logger.info(f"Updated {len(updates)} Call Status cell(s).")
    else:
        logger.info("Nothing to update (everything already in sync).")

    summary = {
        "updated": len(updates),
        "skipped_nonempty": skipped_nonempty,
        "not_in_sheet": not_in_sheet,
        "total_history": len(sent_numbers),
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync sms_history.csv -> Google Sheet Call Status")
    parser.add_argument("--history", default="sms_history.csv",
                        help="Path to sms_history.csv (default: sms_history.csv)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would change without writing to the sheet.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        summary = sync(history_csv=args.history, dry_run=args.dry_run)
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        sys.exit(1)

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{prefix}Sync complete:")
    print(f"  Cells updated:                 {summary['updated']}")
    print(f"  Skipped (Call Status not empty): {summary['skipped_nonempty']}")
    print(f"  In history but not in sheet:   {summary['not_in_sheet']}")
    print(f"  Total numbers in sms_history:  {summary['total_history']}")


if __name__ == "__main__":
    main()
