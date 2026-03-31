"""
sheets_exporter.py
------------------
Appends enriched foreclosure leads to a shared Google Sheet.

The sheet is the coworker's daily working document:
  - Pipeline only APPENDS new rows at the bottom
  - Pipeline NEVER touches status/notes columns — those belong to the caller
  - Rows never move or get deleted

Columns include the full enriched data: equity estimates, phone numbers,
relative contacts, and SMS status from the complete pipeline.
"""

import logging
import os
from datetime import date

logger = logging.getLogger(__name__)

# The columns that get pushed to the sheet — enriched data + coworker columns
HEADER_ROW = [
    "Owner Name",
    "Property Address",
    "Mailing Address",
    "County",
    "Filing Date",
    "Sale Date",
    "Lender",
    "Loan Amount",
    "Estimated Home Value",
    "Estimated Equity",
    "Equity Note",
    "Phone Numbers",
    "Relative Phones",
    "Relatives",
    "Date Added",
    "Status",
    "Notes",
]

# Maps from CSV column names → row position
FIELD_MAP = [
    "owner_name",
    "property_address",
    "mailing_address",
    "rc_county",
    "filing_date",
    "sale_date",
    "lender",
    "loan_amount",
    "estimated_home_value",
    "estimated_equity",
    "equity_note",
    "phones_subject",
    "phones_relatives",
    "relatives_names",
    # date_added is auto-filled
    # status + notes are left blank for coworker
]

NUM_COLS = len(HEADER_ROW)  # 17 columns (A through Q)
LAST_COL_LETTER = "Q"


def _get_client():
    """
    Authenticate with Google Sheets using OAuth2 (user login).

    On first run, opens a browser for you to log in and grant access.
    After that, the token is cached and reused automatically.
    """
    import gspread

    creds_path = os.environ.get("GOOGLE_SHEETS_CREDS", "credentials.json")
    # Look for credentials.json in the project root if not absolute
    if not os.path.isabs(creds_path):
        project_root = os.path.dirname(os.path.abspath(__file__))
        creds_path = os.path.join(project_root, creds_path)

    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"OAuth credentials not found at '{creds_path}'. "
            "Download from Google Cloud Console > APIs & Services > Credentials."
        )

    return gspread.oauth(
        credentials_filename=creds_path,
        authorized_user_filename=os.path.join(
            os.path.dirname(creds_path), "token.json"
        ),
    )


def _ensure_header(worksheet):
    """Add header row and freeze it if the sheet is empty."""
    existing = worksheet.row_values(1)
    if not existing or existing[0] != HEADER_ROW[0]:
        cell_range = f"A1:{LAST_COL_LETTER}1"
        worksheet.update(cell_range, [HEADER_ROW])
        worksheet.format(cell_range, {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
        })
        worksheet.freeze(rows=1)
        logger.info("Sheet header row created and frozen.")


def _get_existing_keys(worksheet) -> set[tuple]:
    """Read columns A and B to get all existing (owner_name, property_address) keys."""
    names = worksheet.col_values(1)[1:]     # column A
    addresses = worksheet.col_values(2)[1:]  # column B

    keys = set()
    for i in range(max(len(names), len(addresses))):
        name = names[i].strip().lower() if i < len(names) else ""
        addr = addresses[i].strip().lower() if i < len(addresses) else ""
        if name or addr:
            keys.add((name, addr))
    return keys


def _highlight_rows(worksheet, start_row: int, end_row: int):
    """Apply light yellow highlight to new rows."""
    if start_row > end_row:
        return
    cell_range = f"A{start_row}:{LAST_COL_LETTER}{end_row}"
    worksheet.format(cell_range, {
        "backgroundColor": {"red": 1.0, "green": 0.98, "blue": 0.8},
    })


def export_to_sheets(new_records: list[dict], sheet_id: str = None, creds_path: str = None) -> int:
    """
    Append enriched leads to the Google Sheet.

    Args:
        new_records: List of enriched lead dicts (post-equity, post-skip-trace).
        sheet_id:    Google Sheet ID (defaults to GOOGLE_SHEET_ID env var).
        creds_path:  Path to credentials JSON (defaults to GOOGLE_SHEETS_CREDS env var).

    Returns:
        Number of rows actually appended (after dedup against sheet).
    """
    if not new_records:
        logger.info("Sheets: no new records to export.")
        return 0

    if creds_path:
        os.environ["GOOGLE_SHEETS_CREDS"] = creds_path

    sheet_id = sheet_id or os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError(
            "No Google Sheet ID provided. Set GOOGLE_SHEET_ID env var "
            "or pass sheet_id parameter."
        )

    client = _get_client()
    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.sheet1

    # Ensure header exists
    _ensure_header(worksheet)

    # Get existing keys to avoid duplicates
    existing_keys = _get_existing_keys(worksheet)
    logger.info(f"Sheets: {len(existing_keys)} existing leads in sheet.")

    # Filter to truly new records
    today = date.today().isoformat()
    rows_to_add = []
    for record in new_records:
        key = (
            record.get("owner_name", "").strip().lower(),
            record.get("property_address", "").strip().lower(),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)  # prevent within-batch dupes

        row = []
        for field in FIELD_MAP:
            val = record.get(field, "")
            # Clean up "nan" strings from pandas
            if val is None or str(val).strip().lower() == "nan":
                val = ""
            row.append(str(val))

        row.append(today)  # date_added
        row.append("")     # status (coworker fills in)
        row.append("")     # notes (coworker fills in)
        rows_to_add.append(row)

    if not rows_to_add:
        logger.info("Sheets: all records already in sheet, nothing to append.")
        return 0

    # Find the first empty row
    all_values = worksheet.col_values(1)
    start_row = len(all_values) + 1

    # Batch append
    end_row = start_row + len(rows_to_add) - 1
    cell_range = f"A{start_row}:{LAST_COL_LETTER}{end_row}"
    worksheet.update(cell_range, rows_to_add)

    # Highlight new rows
    _highlight_rows(worksheet, start_row, end_row)

    logger.info(f"Sheets: appended {len(rows_to_add)} new lead(s) (rows {start_row}-{end_row}).")
    print(f"\n✓ Appended {len(rows_to_add)} new lead(s) to Google Sheet")
    return len(rows_to_add)
