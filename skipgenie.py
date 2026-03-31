"""
skipgenie.py
------------
Playwright automation for Skip Genie bulk skip tracing with relationship analysis.

For each lead: searches the owner, extracts their phones, then clicks into
up to 6 ranked relatives (Spouse → Parents → Children → Siblings) to get
their phone numbers and addresses.

Usage:
    python skipgenie.py --input leads_with_equity.csv --output leads_traced.csv
    python skipgenie.py --input leads.csv --output traced.csv --max-relatives 3
"""

import argparse
import asyncio
import csv
import logging
import os
import re
import sys
from pathlib import Path

import gender_guesser.detector as gender_detector
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

_gender_d = gender_detector.Detector()

load_dotenv()
logger = logging.getLogger(__name__)

SKIPGENIE_URL = "https://web.skipgenie.com"
SESSION_DIR = str(Path(".skipgenie_session").resolve())

# ---------------------------------------------------------------------------
# Skip Genie automation
# ---------------------------------------------------------------------------

async def _react_fill(page, selector: str, value: str) -> None:
    """
    Fill an input in a way that triggers React's synthetic event system.
    Standard page.fill() bypasses React's onChange, leaving the button disabled.
    This uses the native value setter + dispatches 'input' and 'change' events.
    """
    await page.evaluate("""
        ([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeSetter.call(el, val);
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
    """, [selector, value])


async def _dismiss_popups(page) -> None:
    """Dismiss Terms of Service, cookie banners, or other blocking popups."""
    try:
        # Look for common accept/agree/OK buttons in popups
        for selector in [
            'button:has-text("Accept")',
            'button:has-text("Agree")',
            'button:has-text("I Agree")',
            'button:has-text("OK")',
            'button:has-text("Continue")',
            'button:has-text("Got it")',
            'button:has-text("Close")',
            '.modal button.btn-primary',
            '.modal button.pu_btn',
            '[class*="terms"] button',
            '[class*="tos"] button',
            '[class*="policy"] button',
        ]:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                logger.info(f"  Dismissed popup via: {selector}")
                await page.wait_for_timeout(1_000)
                return
    except Exception:
        pass


async def _is_on_search_page(page) -> bool:
    """Check if the search form is actually loaded AND no modals are blocking it."""
    try:
        first_name = await page.query_selector('input[placeholder*="First Name" i]')
        get_info = await page.query_selector('button.pu_btn_user_search')
        if not first_name or not get_info:
            return False
        # Check no blocking modal/popup is visible (ToS, terms, etc.)
        has_blocking_modal = await page.evaluate("""
            () => {
                const modals = document.querySelectorAll(
                    '.modal.show, .modal[style*="display: block"], ' +
                    '[class*="terms"], [class*="tos"], [class*="policy"], ' +
                    '.popup:not([style*="hidden"]):not([style*="display: none"])'
                );
                for (const m of modals) {
                    if (m.offsetParent !== null && m.innerText.length > 20) return true;
                }
                return false;
            }
        """)
        return not has_blocking_modal
    except Exception:
        return False


async def login(page, username: str, password: str) -> None:
    # Navigate to a protected page — server will redirect to login if not authenticated
    await page.goto(f"{SKIPGENIE_URL}/user/search", wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")

    # Verify we're actually on the search page (not just URL check)
    if await _is_on_search_page(page):
        logger.info("Already logged in to Skip Genie (session restored).")
        return

    logger.info(f"Not on search page (URL: {page.url}). Need to log in.")
    logger.info("Please log in manually in the browser window (enter email, password, solve CAPTCHA).")

    # Wait up to 3 minutes for user to complete login/CAPTCHA manually
    # Check by verifying the search form actually appears
    logger.info("Waiting up to 3 minutes for login + CAPTCHA... Please log in in the browser window.")
    for i in range(90):
        await page.wait_for_timeout(2_000)
        # Don't auto-dismiss popups here — let the user handle login + ToS manually
        if await _is_on_search_page(page):
            break
        if i % 15 == 14:
            logger.info(f"  Still waiting for login... ({(i+1)*2}s elapsed)")
    else:
        await page.screenshot(path="skipgenie_login_debug.png")
        raise RuntimeError("Timed out waiting for Skip Genie login. Please log in within 3 minutes.")

    # Give user a few seconds to finish any remaining popups/ToS before we start
    logger.info("Search page detected — waiting 5 seconds before starting searches...")
    await page.wait_for_timeout(5_000)
    await _dismiss_popups(page)
    await page.wait_for_timeout(2_000)
    logger.info("Ready — starting searches.")


# ---------------------------------------------------------------------------
# Gender / relationship helpers
# ---------------------------------------------------------------------------

def _guess_gender(first_name: str) -> str:
    """Return 'male', 'female', or 'unknown'."""
    result = _gender_d.get_gender(first_name.capitalize())
    if result in ("male", "mostly_male"):
        return "male"
    if result in ("female", "mostly_female"):
        return "female"
    return "unknown"


def _infer_relationship(poi_age, poi_last_name, poi_gender,
                        rel_name, rel_age, rel_index) -> tuple[str, int]:
    """
    Infer relationship from age, last name, list position, and gender.
    Returns (relationship_label, priority) where lower priority = more important.
    Priority: 1=Spouse, 2=Parent, 3=Child, 4=Sibling, 5=Other
    """
    parts = rel_name.split()
    rel_first = parts[0] if parts else ""
    rel_last = parts[-1] if len(parts) > 1 else ""
    rel_gender = _guess_gender(rel_first)
    same_last = rel_last.upper() == poi_last_name.upper() if rel_last and poi_last_name else False

    if poi_age is None or rel_age is None:
        # Can't do age-based inference — use position + name only
        if rel_index == 0:
            return ("Spouse", 1)
        if same_last:
            return ("Relative", 5)
        return ("Associate", 6)

    age_diff = rel_age - poi_age  # positive = relative is older

    # Spouse: first in list, same last name, similar age
    if rel_index == 0 and same_last and abs(age_diff) <= 10:
        if rel_gender == "female":
            return ("Wife", 1)
        if rel_gender == "male":
            return ("Husband", 1)
        return ("Spouse", 1)

    # Parent: same last name, 20+ years older
    if same_last and age_diff >= 20:
        if rel_gender == "female":
            return ("Mother", 2)
        if rel_gender == "male":
            return ("Father", 2)
        return ("Parent", 2)

    # Parent with different last name (e.g. mother's maiden name)
    if not same_last and age_diff >= 20 and rel_index < 10:
        if rel_gender == "female":
            return ("Mother", 2)
        return ("Parent", 2)

    # Child: same last name (of male parent), 20+ years younger
    if same_last and age_diff <= -20:
        if rel_gender == "female":
            return ("Daughter", 3)
        if rel_gender == "male":
            return ("Son", 3)
        return ("Child", 3)

    # Sibling: same last name, similar age, not first in list
    if same_last and abs(age_diff) <= 15 and rel_index > 0:
        if rel_gender == "female":
            return ("Sister", 4)
        if rel_gender == "male":
            return ("Brother", 4)
        return ("Sibling", 4)

    # Possible married sister: different last name, female, similar age
    if not same_last and rel_gender == "female" and abs(age_diff) <= 15:
        return ("Sister (married)", 4)

    # Similar age, first in list, different last name — possible spouse
    if rel_index == 0 and abs(age_diff) <= 10:
        return ("Spouse", 1)

    return ("Relative", 5)


def _normalize_addr(addr: str) -> str:
    """Normalize address for comparison."""
    addr = addr.upper().strip()
    addr = re.sub(r'\b(APT|UNIT|STE|SUITE|#)\s*\S+', '', addr)
    addr = re.sub(r'\s+', ' ', addr).strip().rstrip(',.')
    # Remove county name at end (e.g. "HAYS", "TRAVIS")
    addr = re.sub(r'\s+(HAYS|TRAVIS|WILLIAMSON|BELL|BURNET|BASTROP)$', '', addr)
    return addr


def _addresses_match(addr1: str, addr2: str) -> bool:
    if not addr1 or not addr2:
        return False
    return _normalize_addr(addr1) == _normalize_addr(addr2)


# Credit dedup cache: avoid re-searching the same person across leads
_search_cache: dict[str, dict] = {}


def _cache_key(first_name: str, last_name: str) -> str:
    return f"{first_name.strip().upper()}|{last_name.strip().upper()}"


# ---------------------------------------------------------------------------
# Extraction helpers (correct DOM selectors from live page inspection)
# ---------------------------------------------------------------------------

async def _extract_result_header(page) -> dict:
    """Parse the result header: 'Result : 1 of 1 NAME at AGE - DOB: ...'"""
    try:
        header_text = await page.evaluate("""
            () => {
                // Search all <p> elements for one containing "Result"
                const ps = document.querySelectorAll('p');
                for (const p of ps) {
                    if (p.textContent.includes('Result') && p.textContent.includes('of')) {
                        return p.textContent;
                    }
                }
                return "";
            }
        """)
        if not header_text:
            # Fallback: try scanning the full body text
            body = await page.inner_text("body")
            m = re.search(r'Result\s*:\s*\d+\s+of\s+\d+\s+(.+?)\s+at\s+(\d+)', body)
            if m:
                return {"name": m.group(1).strip(), "age": int(m.group(2)), "dob": ""}
            logger.debug("  Result header not found on page")
            return {}
        logger.debug(f"  Result header: {header_text[:100]}")
        # Pattern: "Result : 1 of 1 SANDRA LEE RAMIREZ at 54 - DOB: Unavailable"
        m = re.search(r'Result\s*:\s*\d+\s+of\s+\d+\s+(.+?)\s+at\s+(\d+)\s*-\s*DOB:\s*(.*)', header_text)
        if m:
            return {"name": m.group(1).strip(), "age": int(m.group(2)), "dob": m.group(3).strip()}
        # Try without DOB
        m = re.search(r'Result\s*:\s*\d+\s+of\s+\d+\s+(.+?)\s+at\s+(\d+)', header_text)
        if m:
            return {"name": m.group(1).strip(), "age": int(m.group(2)), "dob": ""}
        # Try name only
        m = re.search(r'Result\s*:\s*\d+\s+of\s+\d+\s+(.+)', header_text)
        if m:
            return {"name": m.group(1).strip(), "age": None, "dob": ""}
    except Exception as e:
        logger.debug(f"  Result header extraction error: {e}")
    return {}


async def _extract_phones_from_section(page) -> list[dict]:
    """Extract phones from the Possible Phone Numbers section only.
    Returns [{"number": "5125672507", "phone_type": "Wireless"}, ...]
    """
    phones = []
    try:
        section_text = await page.evaluate("""
            () => {
                const h = document.evaluate(
                    "//h5[contains(text(),'Possible Phone Numbers')]",
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                return h ? h.parentElement.innerText : "";
            }
        """)
        for m in re.finditer(r'\((\d{3})\)\s*(\d{3})-(\d{4})\s*\(([^)]+)\)', section_text):
            phones.append({
                "number": f"{m.group(1)}{m.group(2)}{m.group(3)}",
                "phone_type": m.group(4),
            })
    except Exception as e:
        logger.warning(f"  Phone extraction error: {e}")
    return phones


async def _extract_current_address(page) -> str:
    """Get the first (most recent) address from Address History."""
    try:
        addr = await page.evaluate("""
            () => {
                const h = document.evaluate(
                    "//h5[contains(text(),'Address History')]",
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (!h) return "";
                const firstAddr = h.parentElement.querySelector('h6');
                return firstAddr ? firstAddr.textContent.trim() : "";
            }
        """)
        return addr or ""
    except Exception:
        return ""


async def _extract_relatives_list(page) -> list[dict]:
    """Extract name+age from Possible Relatives section. No clicks, no credits.
    Returns [{"name": "MARIO ALEMAN", "age": 54, "index": 0, "source": "relative"}, ...]
    """
    return await _extract_people_section(page, "Possible Relatives", "relative")


async def _extract_associates_list(page) -> list[dict]:
    """Extract name+age from Possible Associates section."""
    return await _extract_people_section(page, "Possible Associates", "associate")


async def _extract_people_section(page, heading_text: str, source: str) -> list[dict]:
    """Generic extraction for Relatives or Associates section."""
    people = []
    try:
        entries_data = await page.evaluate("""
            (headingText) => {
                const h = document.evaluate(
                    `//h5[contains(text(),'${headingText}')]`,
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (!h) return [];
                const container = h.parentElement;
                const h6s = container.querySelectorAll('h6.skipg_seach_link_highlight');
                return Array.from(h6s).map((h6, i) => {
                    const span = h6.querySelector('span');
                    const age = span ? span.textContent.trim() : '';
                    const name = h6.textContent.replace(age, '').trim();
                    return { name, age, index: i };
                });
            }
        """, heading_text)
        for entry in entries_data:
            age_str = str(entry.get("age", "")).strip()
            people.append({
                "name": entry["name"],
                "age": int(age_str) if age_str.isdigit() else None,
                "index": entry["index"],
                "source": source,
            })
    except Exception as e:
        logger.warning(f"  Error extracting {heading_text}: {e}")
    return people


async def _check_deceased(page) -> bool:
    """Check if the current result person is marked as deceased."""
    try:
        body_text = await page.inner_text("body")
        return bool(re.search(r'\bdeceased\b|\bdate of death\b|\bDOD:\s*\d', body_text, re.IGNORECASE))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Search execution helpers
# ---------------------------------------------------------------------------

async def _execute_search(page, first_name: str, last_name: str,
                          street: str = "", city: str = "", state: str = "TX",
                          zip_code: str = "") -> bool:
    """Fill the search form and execute. Returns True if results loaded, False if no results."""
    await page.goto(f"{SKIPGENIE_URL}/user/search", wait_until="networkidle", timeout=30_000)
    await _dismiss_popups(page)

    await _react_fill(page, 'input[placeholder*="First Name" i]', first_name)
    if last_name:
        await _react_fill(page, 'input[placeholder*="Last Name" i]', last_name)
    if street:
        await _react_fill(page, 'input[placeholder*="Street Address" i]', street.strip())
    if city:
        await _react_fill(page, 'input[placeholder*="City" i]', city.strip())
    if state:
        await _react_fill(page, 'input[placeholder*="State" i]', state.strip())
    if zip_code:
        await _react_fill(page, 'input[placeholder*="Zip" i], input[placeholder*="Postal" i]', zip_code.strip())

    # Click Get Info
    get_info_btn = page.locator('button.pu_btn_user_search').first
    await get_info_btn.click(timeout=10_000)

    # Confirm execution
    confirm_btn = page.locator('button:has-text("Yes, Execute Search"):visible').first
    await confirm_btn.wait_for(state="visible", timeout=15_000)
    await confirm_btn.click()

    # Wait for either results modal or "no results" message
    try:
        await page.wait_for_function("""
            () => {
                const body = document.body.innerText;
                return body.includes('Property Details') ||
                       body.includes('No results') ||
                       body.includes('no results') ||
                       body.includes('Address History') ||
                       body.includes('Possible Phone');
            }
        """, timeout=15_000)
    except Exception:
        # Fallback: just wait a bit
        await page.wait_for_timeout(3_000)

    await page.wait_for_timeout(1_000)  # let React settle

    # Check for no-results
    body_text = await page.inner_text("body")
    if any(p in body_text.lower() for p in ["no results", "no records found", "0 results", "no matches"]):
        return False
    if "Property Details" not in body_text and "Address History" not in body_text:
        logger.warning("  Search completed but no Property Details found")
        return False
    return True


async def _search_relative(page, rel_name: str, poi_address: str) -> dict:
    """Search a relative by name only (costs 1 credit). Returns extracted data."""
    parts = rel_name.split()
    first_name = parts[0] if parts else rel_name
    last_name = parts[-1] if len(parts) > 1 else ""

    # Check cache first
    key = _cache_key(first_name, last_name)
    if key in _search_cache:
        logger.info(f"    Cache hit for {rel_name}")
        cached = _search_cache[key].copy()
        cached["same_address"] = _addresses_match(cached.get("current_address", ""), poi_address)
        return cached

    result = {"phones": [], "current_address": "", "is_deceased": False, "same_address": False}

    try:
        found = await _execute_search(page, first_name, last_name, state="TX")
        if not found:
            logger.info(f"    No results for relative: {rel_name}")
            _search_cache[key] = result
            return result

        # Debug: screenshot after relative search
        await page.screenshot(path="skipgenie_rel_debug.png")
        body_snippet = await page.inner_text("body")
        logger.info(f"    Page text (200 chars): {body_snippet[:200]}")

        # Extract data from relative's profile
        phones = await _extract_phones_from_section(page)
        logger.info(f"    Extracted {len(phones)} phone(s) for {rel_name}")
        result["phones"] = phones[:3]
        result["current_address"] = await _extract_current_address(page)
        result["is_deceased"] = await _check_deceased(page)
        result["same_address"] = _addresses_match(result["current_address"], poi_address)
        if result["current_address"]:
            logger.info(f"    Address: {result['current_address'][:60]}")

        # Close the modal
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

    except Exception as e:
        logger.warning(f"    Error searching relative {rel_name}: {e}")

    _search_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

def _empty_result() -> dict:
    """Return a blank result dict with all expected columns."""
    result = {
        "phone_1": "", "phone_2": "", "phone_3": "",
        "phones_subject": "",
        "poi_age": "", "poi_deceased": "",
        "skip_status": "not_found",
        # Backward compat
        "phones_relatives": "", "relatives_names": "",
    }
    for i in range(1, 7):
        result[f"rel_{i}_name"] = ""
        result[f"rel_{i}_relationship"] = ""
        result[f"rel_{i}_phone_1"] = ""
        result[f"rel_{i}_phone_2"] = ""
        result[f"rel_{i}_phone_3"] = ""
        result[f"rel_{i}_address"] = ""
        result[f"rel_{i}_same_address"] = ""
        result[f"rel_{i}_deceased"] = ""
    return result


async def search_person(page, name: str, address: str, max_relatives: int = 6) -> dict:
    """
    Search for a person on Skip Genie, extract their data and up to 6 relatives.
    Returns a flat dict with all columns for CSV output.
    """
    result = _empty_result()

    try:
        # Split name — use only the primary person (before "And")
        primary_name = re.split(r'\s+[Aa]nd\s+', name)[0].strip()
        parts = primary_name.split()
        first_name = parts[0] if parts else primary_name
        last_name = parts[-1] if len(parts) > 1 else ""
        poi_gender = _guess_gender(first_name)
        logger.info(f"  Searching POI: first='{first_name}' last='{last_name}'")

        # Parse address
        street, city, state, zip_code = "", "", "TX", ""
        addr_match = re.match(
            r'^(.+?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
            address.strip(), re.IGNORECASE
        )
        if addr_match:
            street, city, state, zip_code = addr_match.groups()
        else:
            partial = re.match(r'^(.+?),\s*([^,]+)', address.strip())
            if partial:
                street, city = partial.groups()
            else:
                street = address

        # Step 1: Execute POI search (1 credit)
        found = await _execute_search(page, first_name, last_name, street, city, state, zip_code)
        if not found:
            result["skip_status"] = "not_found"
            logger.info(f"  No results for: {name}")
            return result

        # Step 2: Extract POI data
        header = await _extract_result_header(page)
        poi_age = header.get("age")
        result["poi_age"] = str(poi_age) if poi_age else ""

        poi_deceased = await _check_deceased(page)
        result["poi_deceased"] = "Yes" if poi_deceased else ""

        phones = await _extract_phones_from_section(page)
        poi_phones = [p["number"] for p in phones[:3]]
        result["phone_1"] = poi_phones[0] if len(poi_phones) > 0 else ""
        result["phone_2"] = poi_phones[1] if len(poi_phones) > 1 else ""
        result["phone_3"] = poi_phones[2] if len(poi_phones) > 2 else ""
        result["phones_subject"] = ", ".join(poi_phones)

        poi_current_addr = await _extract_current_address(page)

        logger.info(f"  POI: age={poi_age}, phones={len(poi_phones)}, deceased={poi_deceased}")

        # Step 3: Extract relatives + associates list (0 credits)
        relatives = await _extract_relatives_list(page)
        associates = await _extract_associates_list(page)
        logger.info(f"  Found {len(relatives)} relatives, {len(associates)} associates")

        # Close the POI modal before searching relatives
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

        # Step 4: Rank relatives by relationship
        ranked = []
        for rel in relatives:
            label, priority = _infer_relationship(
                poi_age, last_name, poi_gender,
                rel["name"], rel["age"], rel["index"]
            )
            ranked.append({**rel, "relationship": label, "priority": priority})

        # Add associates as low priority
        for assoc in associates:
            ranked.append({**assoc, "relationship": "Associate", "priority": 6})

        # Sort by priority then by list index
        ranked.sort(key=lambda r: (r["priority"], r["index"]))

        # Select top N
        to_search = ranked[:max_relatives]
        logger.info(f"  Will search {len(to_search)} relatives: "
                     + ", ".join(f"{r['name']} ({r['relationship']})" for r in to_search))

        # Step 5: Search each relative (1 credit each)
        all_rel_names = []
        all_rel_phones = []
        for i, rel in enumerate(to_search):
            idx = i + 1  # 1-based
            logger.info(f"    [{idx}/{len(to_search)}] Searching relative: {rel['name']} ({rel['relationship']})")

            rel_data = await _search_relative(page, rel["name"], address)

            result[f"rel_{idx}_name"] = rel["name"]
            result[f"rel_{idx}_relationship"] = rel["relationship"]

            rel_phones = [p["number"] for p in rel_data.get("phones", [])]
            if rel_data.get("is_deceased"):
                result[f"rel_{idx}_deceased"] = "Yes"
                rel_phones = []  # Don't provide phones for deceased
                logger.info(f"    {rel['name']} is deceased — skipping phones")
            else:
                result[f"rel_{idx}_deceased"] = ""

            result[f"rel_{idx}_phone_1"] = rel_phones[0] if len(rel_phones) > 0 else ""
            result[f"rel_{idx}_phone_2"] = rel_phones[1] if len(rel_phones) > 1 else ""
            result[f"rel_{idx}_phone_3"] = rel_phones[2] if len(rel_phones) > 2 else ""
            result[f"rel_{idx}_address"] = rel_data.get("current_address", "")
            result[f"rel_{idx}_same_address"] = "Yes" if rel_data.get("same_address") else ""

            all_rel_names.append(rel["name"])
            all_rel_phones.extend(rel_phones)

        # Backward compat fields
        result["relatives_names"] = ", ".join(all_rel_names)
        result["phones_relatives"] = ", ".join(all_rel_phones)
        result["skip_status"] = "found" if poi_phones or all_rel_phones else "not_found"

        logger.info(
            f"  {name}: {len(poi_phones)} POI phone(s), "
            f"{len(all_rel_phones)} relative phone(s) from {len(to_search)} relatives"
        )

    except PWTimeout:
        logger.warning(f"  Timeout searching for: {name}")
        result["skip_status"] = "error"
    except Exception as e:
        logger.error(f"  Error searching for '{name}': {e}")
        result["skip_status"] = "error"

    return result


def _format_phone(raw: str) -> str:
    """Normalize to 10-digit string."""
    import re
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run(input_csv: str, output_csv: str, headless: bool = True,
              max_relatives: int = 6) -> None:
    in_path = Path(input_csv)
    if not in_path.exists():
        logger.error(f"Input file not found: {in_path}")
        sys.exit(1)

    username = os.getenv("SKIPGENIE_USERNAME", "")
    password = os.getenv("SKIPGENIE_PASSWORD", "")
    if not username or not password:
        logger.error(
            "Set SKIPGENIE_USERNAME and SKIPGENIE_PASSWORD in your .env file."
        )
        sys.exit(1)

    with open(in_path, newline="", encoding="utf-8") as f:
        records = list(csv.DictReader(f))

    logger.info(f"Loaded {len(records)} lead(s) from '{in_path}'")

    enriched = []

    async with async_playwright() as pw:
        # Use persistent context so the session/cookies survive between runs.
        # First run: browser opens, user solves reCAPTCHA and logs in manually.
        # Subsequent runs: session is reloaded automatically, no login needed.
        context = await pw.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=False,  # must be visible so user can solve CAPTCHA on first run
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await login(page, username, password)

        for i, rec in enumerate(records, 1):
            name = rec.get("owner_name", "").strip()
            address = rec.get("property_address", "").strip()
            logger.info(f"[{i}/{len(records)}] Searching: {name} — {address}")

            skip_data = await search_person(page, name, address, max_relatives=max_relatives)
            enriched.append({**rec, **skip_data})

        # Don't close — let the persistent context save session cookies to disk
        # context.close() would kill the browser before cookies are flushed
        await page.wait_for_timeout(2_000)  # give cookies time to flush
        await context.close()

    # Write output
    out_path = Path(output_csv)
    if enriched:
        fieldnames = list(enriched[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(enriched)

    found = sum(1 for r in enriched if r.get("skip_status") == "found")
    logger.info(f"Done. {found}/{len(enriched)} leads traced. Output → {out_path.resolve()}")
    print(f"\n✓ Skip trace complete → {out_path.resolve()}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Skip Genie bulk skip tracer")
    parser.add_argument("--input",    default="leads_with_equity.csv")
    parser.add_argument("--output",   default="leads_traced.csv")
    parser.add_argument("--headless", default="true",
                        help="Run browser headlessly (true/false)")
    parser.add_argument("--max-relatives", type=int, default=6,
                        help="Max relatives to search per lead (0-6, each costs 1 credit)")
    parser.add_argument("--debug",    action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    headless = args.headless.lower() != "false"
    asyncio.run(run(args.input, args.output, headless=headless,
                    max_relatives=args.max_relatives))


if __name__ == "__main__":
    main()
