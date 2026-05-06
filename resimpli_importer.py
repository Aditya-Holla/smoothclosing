"""
resimpli_importer.py
--------------------
Parse REsimpli CSV exports and push them to a Google Sheet.

REsimpli has no public API and no Zapier triggers, so the workflow is:
  1. Manually export leads CSV from REsimpli (Settings -> Export Leads)
  2. Upload it through the "REsimpli Sync" tab in the dashboard
  3. We parse, normalize, and push to a "REsimpli Leads" tab on the
     team's existing Google Sheet

We also cross-reference REsimpli leads sourced from "Foreclosure Auction"
against our scraped leads (leads.csv) so the team can see which scraped
leads turned into actual deals.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path

import gspread
from gspread.exceptions import WorksheetNotFound

from sheets_exporter import _get_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV columns we keep. REsimpli exports 65 columns but most are blank.
# These are the ones with real data + the role columns we care about.
# ---------------------------------------------------------------------------
CORE_COLUMNS = [
    "Property ID",
    "First Name",
    "Last Name",
    "Phone Number",
    "Email Address",
    "Lead Status",
    "Lead Source",
    "Campaign Name",
    "Property Street Address",
    "Property City",
    "Property State",
    "Property Zip",
    "Mailing Address",
    "House Type",
    "Bedroom",
    "Bathroom",
    "Apporx Sqft",
    "Lot Size Sqft",
    "Year Buit",
    "Tax Assessed Value",
    "Tax Billed Amount",
    "Lead Created Date",
    "Appointment Date",
    "Offer Price",
    "Offer Date",
    "Under Contract Date",
    "Under Contract Price",
    "Schedule Closing Date",
    "Expected Profit",
    "Assignment Contract Date",
    "Buyer Name",
    "Buyer Phone Number",
    "Buyer Email",
    "Tags",
    "Dead Lead Reason",
    "Acquisition Manager",
    "Disposition Manager",
    "Lead Manager",
    "Owner",
    "Transaction Coordinator",
]


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_resimpli_csv(file_obj_or_path) -> list[dict]:
    """Parse a REsimpli export CSV into a list of dict rows.

    Accepts a file path, an open file object, or raw bytes/str.
    Strips whitespace, normalizes empty strings to "".
    """
    if isinstance(file_obj_or_path, (str, Path)):
        with open(file_obj_or_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    elif isinstance(file_obj_or_path, bytes):
        text = file_obj_or_path.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    elif isinstance(file_obj_or_path, str):
        reader = csv.DictReader(io.StringIO(file_obj_or_path))
        rows = list(reader)
    else:
        # File-like object (e.g. Streamlit UploadedFile)
        try:
            file_obj_or_path.seek(0)
        except Exception:
            pass
        text = file_obj_or_path.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)

    # Normalize whitespace
    cleaned = []
    for r in rows:
        cleaned.append({k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()})
    return cleaned


# ---------------------------------------------------------------------------
# Address normalization (for cross-referencing with our leads.csv)
# ---------------------------------------------------------------------------

def _norm_addr(addr: str) -> str:
    """Aggressive address normalization for matching.

    Lowercases, strips punctuation, collapses whitespace, removes common
    suffixes like 'rd', 'road', 'street'. Designed for fuzzy fallback —
    not perfect, but catches the obvious matches.
    """
    if not addr:
        return ""
    s = addr.lower()
    # Strip everything after the first comma (drop city/state/zip)
    s = s.split(",")[0]
    # Remove punctuation
    s = re.sub(r"[^\w\s]", " ", s)
    # Common abbreviation normalization
    replacements = {
        r"\bstreet\b": "st",
        r"\bavenue\b": "ave",
        r"\bdrive\b": "dr",
        r"\broad\b": "rd",
        r"\blane\b": "ln",
        r"\bcourt\b": "ct",
        r"\bplace\b": "pl",
        r"\bboulevard\b": "blvd",
        r"\bcircle\b": "cir",
        r"\bparkway\b": "pkwy",
        r"\bhighway\b": "hwy",
    }
    for pat, repl in replacements.items():
        s = re.sub(pat, repl, s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def cross_reference_with_pipeline(
    resimpli_rows: list[dict],
    pipeline_csv_path: str = "leads.csv",
) -> list[dict]:
    """Add a 'pipeline_match' field to REsimpli rows that came from
    'Foreclosure Auction' source — set to the matching pipeline lead's
    filing date if found, else None.

    Returns the same list with new keys added.
    """
    pipeline_path = Path(pipeline_csv_path)
    if not pipeline_path.exists():
        for r in resimpli_rows:
            r["pipeline_match"] = None
        return resimpli_rows

    # Build address -> pipeline record lookup
    pipeline_by_addr: dict[str, dict] = {}
    with open(pipeline_path, encoding="utf-8") as f:
        for record in csv.DictReader(f):
            key = _norm_addr(record.get("property_address", ""))
            if key:
                pipeline_by_addr[key] = record

    for r in resimpli_rows:
        match = None
        if "Foreclosure" in (r.get("Lead Source", "") or ""):
            key = _norm_addr(r.get("Property Street Address", ""))
            if key and key in pipeline_by_addr:
                match = pipeline_by_addr[key]
        r["pipeline_match"] = match
    return resimpli_rows


# ---------------------------------------------------------------------------
# Stats helpers (for dashboard summary)
# ---------------------------------------------------------------------------

def summarize(rows: list[dict]) -> dict:
    """Build summary stats for the dashboard."""
    from collections import Counter
    stats = {
        "total": len(rows),
        "by_status": dict(Counter(r.get("Lead Status", "(blank)") for r in rows).most_common()),
        "by_source": dict(Counter(r.get("Lead Source", "(blank)") for r in rows).most_common()),
        "by_acq_manager": dict(Counter(
            (r.get("Acquisition Manager") or "(none)") for r in rows
        ).most_common()),
    }

    # Pipeline match stats (only relevant if cross_reference_with_pipeline ran)
    matched = sum(1 for r in rows if r.get("pipeline_match"))
    foreclosure_total = sum(
        1 for r in rows if "Foreclosure" in (r.get("Lead Source", "") or "")
    )
    stats["foreclosure_leads"] = foreclosure_total
    stats["foreclosure_matched_to_pipeline"] = matched
    return stats


# ---------------------------------------------------------------------------
# Diff against previous import
# ---------------------------------------------------------------------------

SNAPSHOT_PATH = Path(__file__).parent / "resimpli_snapshot.csv"


def load_snapshot() -> dict[str, dict]:
    """Load the previous import keyed by Property ID."""
    if not SNAPSHOT_PATH.exists():
        return {}
    with open(SNAPSHOT_PATH, encoding="utf-8") as f:
        return {r["Property ID"]: r for r in csv.DictReader(f) if r.get("Property ID")}


def save_snapshot(rows: list[dict]) -> None:
    """Persist the current import as the new snapshot."""
    if not rows:
        return
    fields = list(rows[0].keys())
    # Skip non-CSV-safe keys like nested dicts (pipeline_match)
    fields = [f for f in fields if not isinstance(rows[0].get(f), (dict, list))]
    with open(SNAPSHOT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def diff_against_snapshot(rows: list[dict]) -> dict:
    """Return new / status-changed / missing leads vs. the last import."""
    prev = load_snapshot()
    current_ids = {r.get("Property ID") for r in rows if r.get("Property ID")}
    prev_ids = set(prev.keys())

    new_leads = [r for r in rows if r.get("Property ID") and r["Property ID"] not in prev_ids]
    missing_leads = [prev[pid] for pid in prev_ids - current_ids]

    status_changes = []
    for r in rows:
        pid = r.get("Property ID")
        if pid and pid in prev:
            old_status = prev[pid].get("Lead Status", "")
            new_status = r.get("Lead Status", "")
            if old_status != new_status:
                status_changes.append({
                    "Property ID": pid,
                    "Property Street Address": r.get("Property Street Address", ""),
                    "First Name": r.get("First Name", ""),
                    "Last Name": r.get("Last Name", ""),
                    "old_status": old_status,
                    "new_status": new_status,
                })

    return {
        "new_leads": new_leads,
        "status_changes": status_changes,
        "missing_leads": missing_leads,
    }


# ---------------------------------------------------------------------------
# Google Sheets push
# ---------------------------------------------------------------------------

# We push to a separate worksheet so it never collides with the foreclosure
# pipeline data. Configurable via env var so it can point at any sheet.
RESIMPLI_TAB_NAME = "REsimpli Leads"


def push_to_sheets(
    rows: list[dict],
    sheet_id: str,
    tab_name: str = RESIMPLI_TAB_NAME,
    columns: list[str] | None = None,
) -> tuple[int, str]:
    """Replace the REsimpli tab with the current import.

    REsimpli is the source of truth for these rows, so we do a full replace
    rather than append-and-merge. Returns (row_count, sheet_url).
    """
    if not rows:
        raise ValueError("No rows to push")

    cols = columns or [c for c in CORE_COLUMNS if c in rows[0]]

    client = _get_client()
    sheet = client.open_by_key(sheet_id)

    # Get or create the worksheet
    try:
        ws = sheet.worksheet(tab_name)
        ws.clear()
    except WorksheetNotFound:
        ws = sheet.add_worksheet(title=tab_name, rows=len(rows) + 10, cols=len(cols) + 5)

    # Build the matrix: header + data rows
    matrix = [cols]
    for r in rows:
        matrix.append([str(r.get(c, "") or "") for c in cols])

    ws.update(matrix, value_input_option="USER_ENTERED")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"
    return len(rows), sheet_url
