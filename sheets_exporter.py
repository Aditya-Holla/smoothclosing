"""
sheets_exporter.py
------------------
Appends enriched foreclosure leads to a shared Google Sheet.

The sheet is the coworker's daily working document:
  - Pipeline only APPENDS new rows at the bottom
  - Pipeline NEVER touches status/notes columns — those belong to the caller
  - Rows never move or get deleted

Column layout matches the Austin Foreclosure Spreadsheet format (18 columns).
"""

import logging
import os
import re
from datetime import date, datetime

logger = logging.getLogger(__name__)

# Column layout matching the Austin Foreclosure Spreadsheet format
HEADER_ROW = [
    "Date Posted",
    "Name",
    "Property Address",
    "Property City",
    "Property State",
    "Property Zip",
    "Mailing Address",
    "Lender",
    "Active",
    "In Crm",
    "Loan Secured",
    "Loan Amount",
    "Estimated Value",
    "Estimated Equity",
    "Equity Note",
    "Remarks",
    "Name",
    "Phone Number",
    "Relationship",
    "Call Status",
]

NUM_COLS = len(HEADER_ROW)


def _parse_address(addr: str) -> tuple[str, str, str, str]:
    """Split '3814 TWILIGHT DR, TEMPLE, TX 76502' into (street, city, state, zip)."""
    addr = addr.strip()
    if not addr:
        return ("", "", "", "")

    # Normalize periods to commas (e.g. "TEMPLE. TX 76504")
    addr = addr.replace(". ", ", ").replace(".,", ",")

    # Normalize full state names to abbreviations (e.g. "Texas" → "TX")
    _STATE_MAP = {
        "texas": "TX", "california": "CA", "florida": "FL", "georgia": "GA",
        "new york": "NY", "oklahoma": "OK", "louisiana": "LA", "arkansas": "AR",
        "arizona": "AZ", "colorado": "CO", "tennessee": "TN", "alabama": "AL",
        "mississippi": "MS", "missouri": "MO", "ohio": "OH", "virginia": "VA",
    }
    for full, abbr in _STATE_MAP.items():
        # Match ", Texas 78640" or ", Texas"
        addr = re.sub(rf',\s*{full}\s', f', {abbr} ', addr, flags=re.IGNORECASE)
        addr = re.sub(rf',\s*{full}$', f', {abbr}', addr, flags=re.IGNORECASE)

    # Try pattern: STREET, CITY, ST ZIP
    m = re.match(
        r'^(.+?),\s*(.+?),\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)$', addr
    )
    if m:
        return (m.group(1).strip(), m.group(2).strip(), m.group(3).upper(), m.group(4))

    # Try pattern: STREET, CITY, ST (no zip)
    m = re.match(r'^(.+?),\s*(.+?),\s*([A-Za-z]{2})\.?$', addr)
    if m:
        return (m.group(1).strip(), m.group(2).strip(), m.group(3).upper(), "")

    # No comma between street and city — try known multi-word TX cities first,
    # then fall back to single-word city before ", ST ZIP"
    _MULTI_WORD_CITIES = [
        "GRANITE SHOALS", "HARKER HEIGHTS", "LAGO VISTA", "LIBERTY HILL",
        "SAN ANTONIO", "SAN MARCOS", "BELL COUNTY", "COTTONWOOD SHORES",
        "CEDAR PARK", "ROUND ROCK", "PFLUGERVILLE", "DRIPPING SPRINGS",
    ]
    addr_upper = addr.upper()
    for city in _MULTI_WORD_CITIES:
        idx = addr_upper.rfind(city)
        if idx > 0:
            rest = addr[idx + len(city):].strip().lstrip(",").strip()
            m_rest = re.match(r'([A-Za-z]{2})\s*(\d{5}(?:-\d{4})?)?\.?\s*$', rest)
            if m_rest:
                return (addr[:idx].strip().rstrip(","), city, m_rest.group(1).upper(), m_rest.group(2) or "")

    # Single-word city: greedy street, 1 word before ", ST ZIP"
    m = re.match(r'^(.+)\s+(\S+),\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)$', addr)
    if m:
        return (m.group(1).strip(), m.group(2).strip(), m.group(3).upper(), m.group(4))

    # Single-word city, no zip
    m = re.match(r'^(.+)\s+(\S+),\s*([A-Za-z]{2})\.?\s*$', addr)
    if m:
        return (m.group(1).strip(), m.group(2).strip(), m.group(3).upper(), "")

    # Fallback: entire string as street
    return (addr, "", "", "")


def _clean_val(val) -> str:
    """Clean a value: strip whitespace and convert nan/None to empty string."""
    val = str(val).strip() if val is not None else ""
    return "" if val.lower() == "nan" else val


def _format_dollar(val) -> str:
    """Ensure dollar amounts have $ prefix."""
    val = _clean_val(val)
    if val and val[0].isdigit():
        val = "$" + val
    return val


def _clean_phone(val: str) -> str:
    """Clean phone: strip .0 from float format, remove nan."""
    val = str(val).strip()
    if not val or val.lower() == "nan":
        return ""
    # Strip trailing .0 from pandas float format
    if val.endswith(".0"):
        val = val[:-2]
    return val


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


ACTIVE_OPTIONS = [
    "Canceled",
    "Active",
    "No Info Given",
    "Unable To Reach",
]

IN_CRM_OPTIONS = [
    "In Crm",
]

CALL_STATUS_OPTIONS = [
    "Correct Number, CM",
    "Correct Number, NCM",
    "Wrong Number",
    "NCM",
    "Non-working number",
]


def _ensure_header(worksheet):
    """Add header row and freeze it if the sheet is empty or columns changed."""
    existing = worksheet.row_values(1)
    if not existing or existing[0] != HEADER_ROW[0] or len(existing) != len(HEADER_ROW):
        cell_range = f"A1:{LAST_COL_LETTER}1"
        worksheet.update(cell_range, [HEADER_ROW])
        worksheet.format(cell_range, {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
        })
        worksheet.freeze(rows=1)
        logger.info("Sheet header row created and frozen.")


def _get_existing_keys(worksheet) -> set[tuple]:
    """Read Name (col B) and Property Address (col C) for dedup keys."""
    names = worksheet.col_values(2)[1:]      # column B = Name
    addresses = worksheet.col_values(3)[1:]  # column C = Property Address

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


def _add_dropdowns(worksheet, start_row: int, end_row: int):
    """Add data validation dropdowns for Active, In Crm, and Call Status columns."""
    dropdowns = [
        ("Active", ACTIVE_OPTIONS),
        ("In Crm", IN_CRM_OPTIONS),
        ("Call Status", CALL_STATUS_OPTIONS),
    ]
    requests = []
    for col_name, options in dropdowns:
        col_idx = HEADER_ROW.index(col_name) + 1
        requests.append({
            "setDataValidation": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": start_row - 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": col_idx - 1,
                    "endColumnIndex": col_idx,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in options],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        })
    worksheet.spreadsheet.batch_update({"requests": requests})


def records_to_sheet_rows(records: list[dict]) -> list[list]:
    """Expand pipeline records into sheet-style rows.

    Each lead becomes 1 owner row + up to 6 relative rows underneath.
    Each row is a list of length NUM_COLS matching HEADER_ROW exactly,
    so callers can zip with HEADER_ROW to build a DataFrame.

    No dedup here, no network calls — safe to use for display anywhere
    (dashboard, CLI reports, etc). `export_to_sheets` below reuses this
    for the actual push to Google.
    """
    rows = []
    for record in records:
        prop_street, prop_city, prop_state, prop_zip = _parse_address(
            record.get("property_address", "")
        )
        mail_street, mail_city, mail_state, mail_zip = _parse_address(
            record.get("mailing_address", "")
        )
        owner_name = _clean_val(record.get("owner_name", ""))
        lender = _clean_val(record.get("lender", ""))

        # Owner row (all columns filled)
        owner_row = [
            _normalize_date(record.get("filing_date", "")),        # Date Posted
            owner_name,                                             # Name
            prop_street,                                            # Property Address
            prop_city,                                              # Property City
            prop_state,                                             # Property State
            prop_zip,                                               # Property Zip
            mail_street,                                            # Mailing Address
            lender,                                                 # Lender
            "",                                                     # Active
            "",                                                     # In Crm
            _clean_val(record.get("origination_year", "")),         # Loan Secured
            _format_dollar(record.get("loan_amount", "")),          # Loan Amount
            _clean_val(record.get("estimated_home_value", "")),     # Estimated Value
            _clean_val(record.get("estimated_equity", "")),         # Estimated Equity
            _clean_val(record.get("equity_note", "")),              # Equity Note
            "",                                                     # Remarks
            owner_name,                                             # Name (traced)
            _clean_phone(record.get("phone_1", "")),                # Phone Number
            "",                                                     # Relationship
            "",                                                     # Call Status
        ]
        rows.append(owner_row)

        # Relative rows underneath
        for ri in range(1, 7):
            rel_name = _clean_val(record.get(f"rel_{ri}_name", ""))
            rel_phone = _clean_phone(record.get(f"rel_{ri}_phone_1", ""))
            if not rel_name and not rel_phone:
                continue
            rel_relationship = _clean_val(record.get(f"rel_{ri}_relationship", ""))
            rel_same_addr = _clean_val(record.get(f"rel_{ri}_same_address", ""))
            label = rel_relationship if rel_relationship else "Relative"
            if rel_same_addr.lower() in ("yes", "true", "1"):
                label += " (same addr)"

            rel_row = [""] * NUM_COLS
            rel_row[HEADER_ROW.index("Name", 2)] = rel_name
            rel_row[HEADER_ROW.index("Phone Number")] = rel_phone
            rel_row[HEADER_ROW.index("Relationship")] = label
            rows.append(rel_row)

    return rows


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

    # Filter to truly new records (dedup by owner + street against existing sheet)
    truly_new = []
    for record in new_records:
        prop_street, *_ = _parse_address(record.get("property_address", ""))
        key = (
            record.get("owner_name", "").strip().lower(),
            prop_street.strip().lower(),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)  # prevent within-batch dupes
        truly_new.append(record)

    leads_appended = len(truly_new)
    # Expand to sheet-style rows (owner row + relative rows underneath).
    # Same helper the dashboard uses to preview what's about to be pushed.
    rows_to_add = records_to_sheet_rows(truly_new)

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

    # Highlight new rows and add Call Status dropdown
    _highlight_rows(worksheet, start_row, end_row)
    _add_dropdowns(worksheet, start_row, end_row)

    logger.info(f"Sheets: appended {leads_appended} lead(s) ({len(rows_to_add)} rows incl. relatives, rows {start_row}-{end_row}).")
    print(f"\n✓ Appended {leads_appended} lead(s) ({len(rows_to_add)} rows) to Google Sheet")
    return leads_appended
