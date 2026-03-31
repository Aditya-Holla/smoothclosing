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
import re
from datetime import date, datetime

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
    "Owner Phone 1",
    "Owner Phone 2",
    "Owner Phone 3",
    "Owner Age",
    "Owner Deceased",
]

# Per-relative columns (repeated for Rel 1–6)
for _i in range(1, 7):
    HEADER_ROW.extend([
        f"Rel {_i} Name",
        f"Rel {_i} Relationship",
        f"Rel {_i} Phone 1",
        f"Rel {_i} Phone 2",
        f"Rel {_i} Phone 3",
        f"Rel {_i} Address",
        f"Rel {_i} Same Addr?",
        f"Rel {_i} Deceased",
    ])

HEADER_ROW.extend([
    "Date Added",
    "Status",
    "Notes",
])

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
    "phone_1",
    "phone_2",
    "phone_3",
    "poi_age",
    "poi_deceased",
]

for _i in range(1, 7):
    FIELD_MAP.extend([
        f"rel_{_i}_name",
        f"rel_{_i}_relationship",
        f"rel_{_i}_phone_1",
        f"rel_{_i}_phone_2",
        f"rel_{_i}_phone_3",
        f"rel_{_i}_address",
        f"rel_{_i}_same_address",
        f"rel_{_i}_deceased",
    ])
# date_added is auto-filled
# status + notes are left blank for coworker

NUM_COLS = len(HEADER_ROW)


def _col_letter(n: int) -> str:
    """Convert 1-based column number to Excel-style letter (1=A, 27=AA)."""
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


LAST_COL_LETTER = _col_letter(NUM_COLS)


def _normalize_date(val: str) -> str:
    """Normalize various date formats to MM/DD/YYYY."""
    val = val.strip()
    if not val:
        return ""
    # Try "Month DD, YYYY" (e.g. "August 21, 2013")
    try:
        dt = datetime.strptime(val, "%B %d, %Y")
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        pass
    # Try "Month DD YYYY" without comma
    try:
        dt = datetime.strptime(val, "%B %d %Y")
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        pass
    # Try MM/DD/YYYY or M/D/YYYY (already correct format, just normalize)
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', val)
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    # Try YYYY-MM-DD
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', val)
    if m:
        return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
    return val


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
            val = str(val)
            # Normalize dates to MM/DD/YYYY
            if field in ("filing_date", "sale_date"):
                val = _normalize_date(val)
            row.append(val)

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
