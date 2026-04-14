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

# Column layout — positions are DERIVED from each tab's header row at
# runtime (see _build_col_map). The constants below are DEFAULTS used
# only if a header isn't found, and are kept so existing call sites
# that reference COL_* still compile. Never hardcode column letters
# like "I", "J", "O" etc — always go through _build_col_map so the
# code survives column reordering by the team.
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

# Map from our internal canonical name -> a list of header strings that
# the team might use for that column. Case/whitespace-insensitive match.
# When you add a new column to the sheet, add an entry here.
HEADER_ALIASES = {
    "date":            ["Date"],
    "llc_name":        ["LLC Name"],
    "name1":           ["Name 1"],
    "name2":           ["Name 2"],
    "name3":           ["Name 3"],
    "name4":           ["Name 4"],
    "entity":          ["Entity"],
    "county":          ["County"],
    "phones":          ["Phones", "Phone", "Phone Numbers"],
    "mail_street":     ["Mailing Street", "Mail Street"],
    "mail_city":       ["Mailing City", "Mail City"],
    "state":           ["State", "Mailing State"],
    "zip":             ["Zip", "Mailing Zip", "ZIP"],
    "property_addr":   ["Property Address"],
    "property_city":   ["Property City"],
    "email":           ["Email", "E-mail"],
    "possible_info":   ["Possible Info", "Notes"],
}


def _col_letter(n: int) -> str:
    """1-indexed column number -> Excel letter (1=A, 27=AA)."""
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _build_col_map(ws) -> dict:
    """Read the worksheet's header row and return {canonical_name: 1-based_col}.

    Called once per tab so each tab can have its own layout. If a canonical
    name isn't found in this tab's header, it's simply absent from the map
    and callers should handle that (e.g. skip writing that field).
    """
    header = ws.row_values(1)
    # Build lowercase->position lookup once
    lower_to_pos = {h.strip().lower(): i + 1 for i, h in enumerate(header) if h.strip()}
    col_map = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            pos = lower_to_pos.get(alias.strip().lower())
            if pos:
                col_map[canonical] = pos
                break
    return col_map


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


def _react_fill_sync(page, selector: str, value: str) -> bool:
    """Fill a React-controlled <input> by triggering synthetic events.

    Skip Genie is a React app — plain page.type() / .fill() changes the
    DOM value but does NOT fire React's onChange, so the form looks empty
    and the search button stays disabled. This sets the value via the
    native HTMLInputElement setter, then dispatches 'input' and 'change'
    events that React listens for.

    Sync equivalent of skipgenie.py::_react_fill (async).
    Returns True if the input was found and value set; False otherwise.
    """
    if not value:
        return True  # nothing to fill, treat as success
    js = """
        ([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeSetter.call(el, val);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
    """
    try:
        return bool(page.evaluate(js, [selector, value]))
    except Exception as e:
        logger.debug("  react_fill failed for %s: %s", selector, e)
        return False


def search_address(page, street: str, city: str = "", state: str = "TX", zip_code: str = "") -> dict:
    """Search Skip Genie by mailing address. Returns {phone, email, address, city, state, zip}.

    Used before search_name so a row with a known mailing address gets
    resolved via Skip Genie's more precise Address Search tab first.
    Returns empty result if no street given or no match found.
    """
    result = {"phone": "", "email": "", "address": "", "city": "", "state": "", "zip": ""}

    if not street or not street.strip():
        return result

    page.goto("https://web.skipgenie.com/user/search")
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)

    # Click "Address Search" tab. Skip Genie renders it as <li class="tabs">
    # or similar — try a few selectors so we're resilient to minor UI changes.
    clicked = False
    for selector in [
        'li.tabs:has-text("Address")',
        'li:has-text("Address Search")',
        'a:has-text("Address Search")',
        'button:has-text("Address Search")',
        'button:has-text("Address")',
    ]:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=2000):
                loc.click(timeout=2000)
                clicked = True
                logger.info("  Clicked Address Search tab via: %s", selector)
                break
        except Exception:
            continue
    if not clicked:
        logger.warning("  Could not find Address Search tab; aborting address search")
        return result

    time.sleep(1)

    # React-aware fill. Placeholders match what skipgenie.py (the working
    # async version) uses — case-insensitive partial match on 'Street Address',
    # 'City', 'State', 'Zip' / 'Postal'.
    ok = True
    if street:
        ok &= _react_fill_sync(page, 'input[placeholder*="Street Address" i]', street.strip())
    if city:
        ok &= _react_fill_sync(page, 'input[placeholder*="City" i]', city.strip())
    if state:
        # State on address search shares a placeholder with name search's State
        ok &= _react_fill_sync(page, 'input[placeholder*="State" i]', state.strip())
    if zip_code:
        # Try Zip first, fall back to Postal (some forms use either)
        if not _react_fill_sync(page, 'input[placeholder*="Zip" i]', zip_code.strip()):
            _react_fill_sync(page, 'input[placeholder*="Postal" i]', zip_code.strip())

    if not ok:
        logger.warning("  Could not fill one or more address fields; aborting")
        return result

    # Give React a moment to process onChange handlers
    time.sleep(0.5)

    time.sleep(1)

    # Click GET INFO (same pattern as search_name)
    try:
        for btn in page.locator("button:has-text('GET INFO'), button:has-text('Get Info')").all():
            if btn.is_visible():
                btn.click(force=True)
                break
    except Exception as e:
        logger.warning("  Could not click GET INFO for address search: %s", e)
        return result

    time.sleep(3)

    # Confirm search
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
                break
        except Exception:
            continue
    if not confirmed:
        try:
            for btn in page.locator("button").all():
                if "execute" in btn.inner_text().strip().lower() and btn.is_visible():
                    btn.click()
                    confirmed = True
                    break
        except Exception:
            pass

    if not confirmed:
        logger.info("  No results / no confirmation for address: %s", street)
        return result

    time.sleep(5)

    try:
        page_text = page.inner_text("body")
    except Exception:
        return result

    if "Property Details" not in page_text and "Result :" not in page_text:
        logger.info("  No results for address: %s", street)
        return result

    # Parse results (same pattern as search_name)
    phone_match = re.search(r'\((\d{3})\)\s*(\d{3})-(\d{4})', page_text)
    if phone_match:
        result["phone"] = f"({phone_match.group(1)}) {phone_match.group(2)}-{phone_match.group(3)}"

    for email_match in re.finditer(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', page_text, re.IGNORECASE):
        email = email_match.group(0)
        if "webuyanything" not in email.lower() and "skipgenie" not in email.lower():
            result["email"] = email
            break

    # Address already known (we used it for search), but capture the first
    # Address History entry in case Skip Genie canonicalizes/formats it.
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
            addr_parts = parse_address(line)
            result["address"] = addr_parts["street"]
            result["city"] = addr_parts["city"]
            result["state"] = addr_parts["state"]
            result["zip"] = addr_parts["zip"]
            break

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


def get_pending_rows(ws, include_traced: bool = False) -> list[dict]:
    """Rows to process. Default: Phones empty + at least one name.

    Args:
        include_traced: If True, also include rows that already have a
            Phones value. Used by --retrace-all to force a re-run across
            every row (will overwrite existing Phones/Mailing/Email).

    Also captures existing Mailing Street/City/State/Zip if the row has
    them pre-filled — _trace_tab uses those to do an Address Search
    BEFORE falling back to Name Search. Uses the tab's own header to
    locate columns so different tab layouts all work.
    """
    col_map = _build_col_map(ws)
    all_data = ws.get_all_values()
    rows = []
    name_cols = [col_map.get(k) for k in ("name1", "name2", "name3", "name4")]
    name_cols = [c for c in name_cols if c]
    phones_col = col_map.get("phones")
    llc_col = col_map.get("llc_name")
    mail_street_col = col_map.get("mail_street")
    mail_city_col = col_map.get("mail_city")
    state_col = col_map.get("state")
    zip_col = col_map.get("zip")
    if not phones_col or not name_cols:
        logger.warning(
            f"Tab '{ws.title}' is missing required columns (phones/names). "
            f"Header: {all_data[0] if all_data else '(empty)'}"
        )
        return []
    max_col = max(
        [phones_col, llc_col or 0, mail_street_col or 0, mail_city_col or 0,
         state_col or 0, zip_col or 0] + name_cols
    )

    def _cell(row, col):
        return row[col - 1].strip() if col and col - 1 < len(row) else ""

    for i, row in enumerate(all_data[1:], 2):  # skip header, 1-indexed
        while len(row) < max_col:
            row.append("")
        phones = _cell(row, phones_col)
        names = [row[c - 1].strip() for c in name_cols if row[c - 1].strip()]
        if not names:
            continue
        # Default: only rows with empty Phones. With include_traced=True
        # we retrace everyone who has a name.
        if phones and not include_traced:
            continue
        rows.append({
            "row_num": i,
            "names": names,
            "llc_name": _cell(row, llc_col),
            "mail_street": _cell(row, mail_street_col),
            "mail_city":   _cell(row, mail_city_col),
            "state":       _cell(row, state_col),
            "zip":         _cell(row, zip_col),
            "had_phones":  bool(phones),  # for logging
        })
    return rows


def write_result(ws, row_num: int, phones_str: str, address: dict, email: str = "", col_map: dict = None):
    """Write trace results back to the sheet using this tab's header layout.

    Pass col_map to avoid re-reading the header on every write (caller
    should build it once per tab). Writes to whichever columns exist in
    this tab — if "email" column is absent, email is silently skipped.
    """
    from gspread.utils import ValueInputOption

    if col_map is None:
        col_map = _build_col_map(ws)

    updates = []

    def _add(canonical: str, value: str):
        col = col_map.get(canonical)
        if not col:
            logger.debug(f"  Skip {canonical} (no column in tab '{ws.title}')")
            return
        letter = _col_letter(col)
        updates.append({"range": f"{letter}{row_num}", "values": [[value]]})

    _add("phones",      phones_str)
    _add("mail_street", address.get("street", ""))
    _add("mail_city",   address.get("city", ""))
    _add("state",       address.get("state", ""))
    _add("zip",         address.get("zip", ""))
    _add("email",       email)

    if not updates:
        logger.warning(f"  Nothing to write for row {row_num} in '{ws.title}' (no matching columns).")
        return
    ws.batch_update(updates, value_input_option=ValueInputOption.raw)


# ── Main ───────────────────────────────────────────────────────────

def _trace_tab(page, ws, tab_name: str, limit: int = None, retrace_all: bool = False) -> int:
    """Trace pending rows in a single worksheet. Returns number of rows traced.

    retrace_all=True forces re-processing of every row with names, even
    ones that already have Phones filled in — the Phones/Mailing/Email
    cells will be overwritten with the new search results.
    """
    pending = get_pending_rows(ws, include_traced=retrace_all)
    if retrace_all:
        already_had = sum(1 for r in pending if r.get("had_phones"))
        logger.info(
            "[%s] retrace_all=True: processing %d rows (%d already had phones, will be OVERWRITTEN)",
            tab_name, len(pending), already_had,
        )

    # Build column map once for this tab — reused on every write below.
    # Different tabs can have different layouts (Austin Metro has a
    # "Property City" column that the others don't).
    col_map = _build_col_map(ws)
    expected = {"phones", "mail_street", "mail_city", "state", "zip", "email"}
    missing = expected - set(col_map.keys())
    if missing:
        logger.warning(
            "[%s] Header is missing expected columns: %s. Those fields will be skipped.",
            tab_name, sorted(missing),
        )

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

        # Step 1: if the row has a mailing address, try Address Search first.
        # This often resolves LLC rows more precisely than name search and
        # can skip unnecessary per-name lookups when it succeeds.
        addr_result = None
        if row.get("mail_street"):
            logger.info(
                "  Address search first: %s, %s, %s %s",
                row["mail_street"], row.get("mail_city", ""),
                row.get("state", ""), row.get("zip", ""),
            )
            addr_result = search_address(
                page,
                street=row["mail_street"],
                city=row.get("mail_city", ""),
                state=row.get("state", "TX"),
                zip_code=row.get("zip", ""),
            )
            if addr_result.get("phone"):
                logger.info("    Phone (from address): %s", addr_result["phone"])
                phone_parts.append(f"[ADDR] {addr_result['phone']}")
            else:
                logger.info("    Address search: no phone")
            if addr_result.get("email"):
                logger.info("    Email (from address): %s", addr_result["email"])
                emails.append(addr_result["email"])
            if addr_result.get("address"):
                first_address = {
                    "street": addr_result["address"],
                    "city":   addr_result["city"],
                    "state":  addr_result["state"],
                    "zip":    addr_result["zip"],
                }
            time.sleep(1)

        # Step 2: always also trace each name. Name searches give us the
        # specific phone-per-person (which Address Search can't — it only
        # returns the current resident). If Address Search already found
        # phones/email, those are kept and these name results are merged.
        for name in row["names"]:
            logger.info("  Tracing: %s", name)
            result = search_name(page, name)

            if result["phone"]:
                phone_parts.append(f"{name} {result['phone']}")
                logger.info("    Phone: %s", result["phone"])
            else:
                phone_parts.append(f"{name} (no phone)")
                logger.info("    No phone found")

            if result["email"] and result["email"] not in emails:
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

        write_result(ws, row["row_num"], phones_str, first_address, email_str, col_map=col_map)
        logger.info("  Written to '%s' row %d", tab_name, row["row_num"])

    return len(pending)


def run(limit: int = None, headless: bool = False, sheet_id: str = None,
        tab: str = None, all_tabs: bool = False, retrace_all: bool = False):
    """Main entry point: read sheet, trace names, write back results.

    retrace_all=True overwrites rows that already have Phones filled in.
    Default False: only process rows with empty Phones.
    """
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
            traced = _trace_tab(page, ws, tab_name, limit=limit, retrace_all=retrace_all)
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
    parser.add_argument(
        "--retrace-all", action="store_true",
        help="Re-process EVERY row with at least one name, including rows "
             "that already have Phones. Overwrites existing Phones/Mailing/"
             "Email cells with new search results. Default: only process "
             "rows where Phones is empty.",
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
        tab=args.tab, all_tabs=args.all_tabs, retrace_all=args.retrace_all)


if __name__ == "__main__":
    main()
