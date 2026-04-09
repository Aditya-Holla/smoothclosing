"""
Buyer Tracer — Skip trace buyer names from Sheet3 and write results back.

Reads rows from Sheet3 where the Phones column is empty, skip traces
Name 1-4 via Skip Genie (name-only search), and writes back:
  - Phones: "Name1 (xxx) xxx-xxxx; Name2 (xxx) xxx-xxxx"
  - Mailing Street, Mailing City, State, Zip (from first result with an address)

Usage:
    python buyer_tracer.py [--limit N] [--headless false] [--debug]
"""

import argparse
import logging
import os
import re
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

logger = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(SCRIPT_DIR, ".skipgenie_session")

# Column layout (1-indexed)
COL_DATE = 1
COL_LLC_NAME = 2
COL_NAME1 = 3
COL_NAME2 = 4
COL_NAME3 = 5
COL_NAME4 = 6
COL_ENTITY = 7
COL_COUNTY = 8
COL_PHONES = 9
COL_MAIL_STREET = 10
COL_MAIL_CITY = 11
COL_STATE = 12
COL_ZIP = 13
COL_PROPERTY_ADDR = 14
COL_EMAIL = 15
COL_POSSIBLE_INFO = 16


# ── Skip Genie Browser Automation ──────────────────────────────────

def ensure_logged_in(page, email=None, password=None) -> bool:
    """Navigate to Skip Genie and handle login if needed."""
    page.goto("https://web.skipgenie.com/user/search")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    if "login" not in page.url.lower():
        return True

    if email and password:
        logger.info("Logging in automatically...")
        try:
            page.locator("input[placeholder*='Email'], input[type='email']").fill(email)
            page.locator("input[placeholder*='Password'], input[type='password']").fill(password)
            try:
                recaptcha = page.frame_locator("iframe[title*='reCAPTCHA']")
                recaptcha.locator(".recaptcha-checkbox-border").click()
                time.sleep(3)
            except Exception:
                pass
            page.click("button:has-text('Login'), button:has-text('LOGIN')")
            page.wait_for_load_state("networkidle")
            time.sleep(3)
        except Exception as e:
            logger.warning("Auto-login failed: %s", e)

        if "login" not in page.url.lower():
            logger.info("Login successful!")
            return True
        logger.warning("Auto-login did not succeed. Falling back to manual.")

    print("\n" + "=" * 55)
    print("  Please log into Skip Genie in the browser window.")
    print("  Your session will be saved for future runs.")
    print("=" * 55)
    try:
        input("\nPress Enter after you've logged in... ")
    except EOFError:
        logger.info("Waiting for login (non-interactive)...")
        for _ in range(120):
            time.sleep(2)
            if "login" not in page.url.lower():
                break
        else:
            logger.error("Timed out waiting for login.")
            return False

    page.goto("https://web.skipgenie.com/user/search")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    if "login" in page.url.lower():
        logger.error("Still not logged in.")
        return False

    logger.info("Login successful!")
    return True


def parse_name(full_name: str) -> dict:
    """Split a full name into first/middle/last."""
    parts = full_name.strip().split()
    if len(parts) == 0:
        return {"first": "", "middle": "", "last": ""}
    elif len(parts) == 1:
        return {"first": parts[0], "middle": "", "last": ""}
    elif len(parts) == 2:
        return {"first": parts[0], "middle": "", "last": parts[1]}
    else:
        return {"first": parts[0], "middle": " ".join(parts[1:-1]), "last": parts[-1]}


def search_name(page, full_name: str) -> dict:
    """Search Skip Genie by name only. Returns {phone, address}."""
    result = {"phone": "", "email": "", "address": "", "city": "", "state": "", "zip": ""}

    person = parse_name(full_name)
    if not person["first"] or not person["last"]:
        logger.warning("Cannot search — need first and last name: %r", full_name)
        return result

    page.goto("https://web.skipgenie.com/user/search")
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)

    # Ensure Name Search tab is active (may already be default)
    try:
        tab = page.locator("text=Name Search").first
        if tab.is_visible(timeout=2000):
            tab.click()
            time.sleep(0.5)
    except Exception:
        pass

    def type_into(placeholder, value):
        if not value:
            return
        field = page.locator(f"input[placeholder*='{placeholder}']").first
        field.click()
        field.press("Meta+a")
        field.press("Backspace")
        field.type(value, delay=30)

    try:
        type_into("First Name", person["first"])
        type_into("Last Name", person["last"])
        if person["middle"]:
            type_into("Middle Name", person["middle"])
        type_into("State", "TX")
    except Exception as e:
        logger.warning("Error filling form for %r: %s", full_name, e)
        return result

    time.sleep(1)

    # Click GET INFO (the visible one)
    try:
        get_info_buttons = page.locator("button:has-text('GET INFO'), button:has-text('Get Info')").all()
        for btn in get_info_buttons:
            if btn.is_visible():
                btn.click(force=True)
                break
    except Exception as e:
        logger.warning("Could not click GET INFO: %s", e)
        return result

    time.sleep(3)

    # Confirm search — try multiple selectors
    confirmed = False
    for selector in [
        "button:has-text('Yes, Execute Search')",
        "button:has-text('YES, EXECUTE SEARCH')",
        "button:has-text('Execute Search')",
        "text=Yes, Execute Search",
    ]:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=3000):
                loc.click(timeout=3000)
                confirmed = True
                logger.debug("Confirmed with selector: %s", selector)
                break
        except Exception:
            continue

    if not confirmed:
        # Last resort: find any button with "execute" in text
        try:
            for btn in page.locator("button").all():
                txt = btn.inner_text().strip()
                if "execute" in txt.lower() and btn.is_visible():
                    btn.click()
                    confirmed = True
                    logger.debug("Confirmed with button: %s", txt)
                    break
        except Exception:
            pass

    if not confirmed:
        logger.warning("No confirmation dialog for %r", full_name)
        return result

    time.sleep(5)

    # Scrape results
    try:
        page_text = page.inner_text("body")
    except Exception:
        return result

    if "Property Details" not in page_text and "Result :" not in page_text:
        logger.info("No results for %s", full_name)
        return result

    # Get first phone number only
    phone_match = re.search(r'\((\d{3})\)\s*(\d{3})-(\d{4})', page_text)
    if phone_match:
        result["phone"] = f"({phone_match.group(1)}) {phone_match.group(2)}-{phone_match.group(3)}"

    # Get first email
    for email_match in re.finditer(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', page_text, re.IGNORECASE):
        email = email_match.group(0)
        if "webuyanything" not in email.lower() and "skipgenie" not in email.lower():
            result["email"] = email
            break

    # Get first address from Address History
    lines = page_text.split('\n')
    in_addr = False
    for line in lines:
        line = line.strip()
        if 'Address History' in line:
            in_addr = True
            continue
        if in_addr and line.startswith('Possible'):
            break
        if in_addr and re.match(r'\d+\s+', line):
            # Parse address line
            addr_parts = parse_address(line)
            result["address"] = addr_parts["street"]
            result["city"] = addr_parts["city"]
            result["state"] = addr_parts["state"]
            result["zip"] = addr_parts["zip"]
            break

    # Close modal
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    time.sleep(1)

    return result


def parse_address(addr_line: str) -> dict:
    """Parse Skip Genie address like '1148 RUSTY BLACKHAW TRL SAN MARCOS TX 78666 HAYS'.

    Format is typically: STREET CITY STATE ZIP [COUNTY]
    No commas — state is always a 2-letter code followed by a 5-digit zip.
    County name may trail after the zip.
    """
    addr = addr_line.strip()

    # Match pattern: ... XX 12345 [COUNTY]
    # State (2 uppercase letters) followed by zip (5 digits), optionally followed by county
    match = re.search(r'\s([A-Z]{2})\s+(\d{5}(?:-\d{4})?)(?:\s+\w+)?\s*$', addr)
    if match:
        state = match.group(1)
        zip_code = match.group(2)
        before_state = addr[:match.start()].strip()

        # Everything before state — last word(s) are the city, rest is street
        # Heuristic: common TX city names can be multi-word, so split on known
        # street suffixes (DR, ST, AVE, LN, TRL, BLVD, WAY, CT, CIR, LOOP, RD, PL)
        suffix_match = re.search(
            r'\b(DR|ST|AVE|LN|TRL|BLVD|WAY|CT|CIR|LOOP|RD|PL|PKWY|HWY|TRCE)\b',
            before_state,
        )
        if suffix_match:
            # Street is everything up to and including the suffix
            end = suffix_match.end()
            # Check if next word is a directional (N/S/E/W/NE/NW/SE/SW) — part of street
            remaining = before_state[end:].strip()
            dir_match = re.match(r'^(N|S|E|W|NE|NW|SE|SW)\b\s*', remaining)
            if dir_match:
                end += len(dir_match.group(0))
            street = before_state[:end].strip()
            city = before_state[end:].strip().rstrip(',')
        else:
            # Fallback: assume last word is city
            parts = before_state.rsplit(None, 1)
            street = parts[0] if len(parts) > 1 else before_state
            city = parts[1] if len(parts) > 1 else ""

        return {"street": street, "city": city, "state": state, "zip": zip_code}

    # Fallback: just return the whole thing as street
    return {"street": addr, "city": "", "state": "", "zip": ""}


# ── Google Sheets Integration ──────────────────────────────────────

DISPOSITIONS_SHEET_ID = "1CCcMeIP8we_HsnUBnE4Pe-RAm2JYnousjtuCRhGK6x8"


def get_worksheet(sheet_id: str = None, tab: str = None):
    """Get a worksheet by sheet ID and tab name."""
    from sheets_exporter import _get_client
    client = _get_client()
    sid = sheet_id or DISPOSITIONS_SHEET_ID
    sheet = client.open_by_key(sid)
    if tab:
        return sheet.worksheet(tab)
    # Default to first worksheet
    return sheet.sheet1


def get_pending_rows(ws) -> list[dict]:
    """Get rows from Sheet3 where Phones column is empty and at least one name exists."""
    all_data = ws.get_all_values()
    rows = []
    for i, row in enumerate(all_data[1:], 2):  # skip header, 1-indexed
        # Pad row to 16 columns
        while len(row) < 16:
            row.append("")

        phones = row[COL_PHONES - 1].strip()
        names = [row[c - 1].strip() for c in [COL_NAME1, COL_NAME2, COL_NAME3, COL_NAME4] if row[c - 1].strip()]

        if not phones and names:
            rows.append({
                "row_num": i,
                "names": names,
                "llc_name": row[COL_LLC_NAME - 1],
            })
    return rows


def write_result(ws, row_num: int, phones_str: str, address: dict, email: str = ""):
    """Write trace results back to the sheet."""
    from gspread.utils import ValueInputOption
    updates = [
        {"range": f"I{row_num}", "values": [[phones_str]]},
        {"range": f"J{row_num}", "values": [[address.get("street", "")]]},
        {"range": f"K{row_num}", "values": [[address.get("city", "")]]},
        {"range": f"L{row_num}", "values": [[address.get("state", "")]]},
        {"range": f"M{row_num}", "values": [[address.get("zip", "")]]},
        {"range": f"O{row_num}", "values": [[email]]},
    ]
    ws.batch_update(updates, value_input_option=ValueInputOption.raw)


# ── Main ───────────────────────────────────────────────────────────

def _trace_tab(page, ws, tab_name: str, limit: int = None) -> int:
    """Trace all pending rows in a single worksheet. Returns number of rows traced."""
    pending = get_pending_rows(ws)

    if not pending:
        logger.info("No pending rows in '%s' (all have phones or no names).", tab_name)
        return 0

    if limit:
        pending = pending[:limit]

    total_names = sum(len(r["names"]) for r in pending)
    logger.info(
        "[%s] %d rows with %d total names to trace (%d Skip Genie credits)",
        tab_name, len(pending), total_names, total_names,
    )

    for row_idx, row in enumerate(pending, 1):
        logger.info(
            "[%s] [%d/%d] Row %d — %s — Names: %s",
            tab_name, row_idx, len(pending), row["row_num"],
            row["llc_name"], ", ".join(row["names"]),
        )

        phone_parts = []
        emails = []
        first_address = {"street": "", "city": "", "state": "", "zip": ""}

        for name in row["names"]:
            logger.info("  Tracing: %s", name)
            result = search_name(page, name)

            if result["phone"]:
                phone_parts.append(f"{name} {result['phone']}")
                logger.info("    Phone: %s", result["phone"])
            else:
                phone_parts.append(f"{name} (no phone)")
                logger.info("    No phone found")

            if result["email"]:
                emails.append(result["email"])
                logger.info("    Email: %s", result["email"])

            if not first_address["street"] and result["address"]:
                first_address = {
                    "street": result["address"],
                    "city": result["city"],
                    "state": result["state"],
                    "zip": result["zip"],
                }

            time.sleep(1)

        phones_str = "; ".join(phone_parts)
        email_str = "; ".join(emails)
        logger.info("  Result: %s", phones_str)

        write_result(ws, row["row_num"], phones_str, first_address, email_str)
        logger.info("  Written to '%s' row %d", tab_name, row["row_num"])

    return len(pending)


def run(limit: int = None, headless: bool = False, sheet_id: str = None,
        tab: str = None, all_tabs: bool = False):
    """Main entry point: read sheet, trace names, write back results."""
    from sheets_exporter import _get_client

    email = os.getenv("SKIPGENIE_EMAIL", "")
    password = os.getenv("SKIPGENIE_PASSWORD", "")

    # Determine which tabs to process
    if all_tabs:
        client = _get_client()
        sid = sheet_id or DISPOSITIONS_SHEET_ID
        sheet = client.open_by_key(sid)
        tabs_to_run = [(ws, ws.title) for ws in sheet.worksheets()]
        logger.info("Running all %d tabs: %s", len(tabs_to_run),
                     ", ".join(t[1] for t in tabs_to_run))
    else:
        ws = get_worksheet(sheet_id, tab)
        tabs_to_run = [(ws, tab or ws.title)]

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        if not ensure_logged_in(page, email=email, password=password):
            context.close()
            return

        total_traced = 0
        for ws, tab_name in tabs_to_run:
            traced = _trace_tab(page, ws, tab_name, limit=limit)
            total_traced += traced

        context.close()

    logger.info("Done. Traced %d rows across %d tab(s).", total_traced, len(tabs_to_run))


def list_tabs(sheet_id: str = None):
    """List available tabs in the sheet."""
    from sheets_exporter import _get_client
    client = _get_client()
    sid = sheet_id or DISPOSITIONS_SHEET_ID
    sheet = client.open_by_key(sid)
    print(f"Sheet: {sheet.title}")
    print(f"Tabs:")
    for ws in sheet.worksheets():
        row_count = len([r for r in ws.get_all_values()[1:] if any(r)])
        print(f"  {ws.title} ({row_count} data rows)")


def main():
    parser = argparse.ArgumentParser(
        description="Skip trace buyer names from a Google Sheet tab and write results back"
    )
    parser.add_argument(
        "--tab", default=None,
        help="Tab/worksheet name to process (e.g., 'Austin Metro'). Default: first tab.",
    )
    parser.add_argument(
        "--sheet-id", default=None,
        help=f"Google Sheet ID (default: Dispositions sheet {DISPOSITIONS_SHEET_ID[:12]}...)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max rows to trace (default: all pending)",
    )
    parser.add_argument(
        "--headless", default="false",
        help="Run browser headlessly (true/false, default: false)",
    )
    parser.add_argument(
        "--all-tabs", action="store_true",
        help="Process ALL tabs in the sheet (one browser session, tabs run sequentially).",
    )
    parser.add_argument(
        "--list-tabs", action="store_true",
        help="List available tabs and exit.",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.list_tabs:
        list_tabs(args.sheet_id)
        return

    headless = args.headless.lower() == "true"
    run(limit=args.limit, headless=headless, sheet_id=args.sheet_id,
        tab=args.tab, all_tabs=args.all_tabs)


if __name__ == "__main__":
    main()
