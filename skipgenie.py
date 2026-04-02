"""
skipgenie.py
------------
Playwright automation for Skip Genie bulk skip tracing with relationship analysis.

ADDRESS-FIRST flow:
1. Search by property address to find the current resident
2. Look for the owner among results (multi-result navigation)
3. Extract owner phones, address, relatives/associates lists
4. Click relative h6 elements to get their profiles (phones, address, deceased)
5. Rank relatives: Spouse → Parents → Children → Siblings

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
    await page.goto(f"{SKIPGENIE_URL}/user/search", wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")

    if await _is_on_search_page(page):
        logger.info("Already logged in to Skip Genie (session restored).")
        return

    logger.info(f"Not on search page (URL: {page.url}). Need to log in.")
    logger.info("Please log in manually in the browser window (enter email, password, solve CAPTCHA).")

    logger.info("Waiting up to 3 minutes for login + CAPTCHA... Please log in in the browser window.")
    for i in range(90):
        await page.wait_for_timeout(2_000)
        if await _is_on_search_page(page):
            break
        if i % 15 == 14:
            logger.info(f"  Still waiting for login... ({(i+1)*2}s elapsed)")
    else:
        await page.screenshot(path="skipgenie_login_debug.png")
        raise RuntimeError("Timed out waiting for Skip Genie login. Please log in within 3 minutes.")

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
                        rel_name, rel_age, rel_index,
                        rel_addresses=None, poi_addresses=None) -> tuple[str, int]:
    """
    Infer relationship from age, last name, list position, gender, and address overlap.
    Returns (relationship_label, priority) where lower priority = more important.
    Priority: 1=Spouse, 2=Parent, 3=Child, 4=Sibling, 5=Friend, 6=Other, 7=Associate
    """
    parts = rel_name.split()
    rel_first = parts[0] if parts else ""
    rel_last = parts[-1] if len(parts) > 1 else ""
    rel_gender = _guess_gender(rel_first)
    same_last = rel_last.upper() == poi_last_name.upper() if rel_last and poi_last_name else False

    # Check address overlap if data available
    addr_overlap = _check_address_overlap(poi_addresses, rel_addresses) if poi_addresses and rel_addresses else False

    if poi_age is None or rel_age is None:
        # Can't do age-based inference — use position + name only
        if rel_index == 0 and rel_gender != poi_gender and rel_gender != "unknown":
            return ("Spouse", 1)
        if rel_index == 0 and same_last:
            return ("Spouse", 1)
        if same_last:
            return ("Relative", 6)
        return ("Associate", 7)

    age_diff = rel_age - poi_age  # positive = relative is older

    # --- Spouse (priority 1) ---
    # Usually first on list, similar age (+/- 10), different gender
    if rel_index == 0 and abs(age_diff) <= 10:
        # Same gender = probably NOT spouse
        if rel_gender == poi_gender and rel_gender != "unknown":
            pass  # fall through to sibling/other rules
        else:
            if addr_overlap:
                # Same address = likely still married
                if rel_gender == "female":
                    return ("Wife", 1)
                if rel_gender == "male":
                    return ("Husband", 1)
                return ("Spouse", 1)
            elif same_last:
                # Same last name but different address = possibly divorced
                if rel_gender == "female":
                    return ("Wife", 1)
                if rel_gender == "male":
                    return ("Husband", 1)
                return ("Spouse", 1)
            else:
                # Different last name, first in list, similar age, different gender
                return ("Spouse", 1)

    # --- Parent (priority 2) ---
    # 20+ years older, higher up in list
    if age_diff >= 20 and rel_index < 10:
        if same_last:
            if rel_gender == "female":
                return ("Mother", 2)
            if rel_gender == "male":
                return ("Father", 2)
            return ("Parent", 2)
        else:
            # Different last name — mother's maiden name
            if rel_gender == "female":
                return ("Mother", 2)
            return ("Parent", 2)

    # --- Child (priority 3) ---
    # 20+ years younger, same last name as male parent
    if age_diff <= -20:
        if same_last:
            if rel_gender == "female":
                return ("Daughter", 3)
            if rel_gender == "male":
                return ("Son", 3)
            return ("Child", 3)
        # Different last name but very young = still possible child (mother's maiden name on child rare)
        # Skip — more likely a different relationship

    # --- Sibling (priority 4) ---
    # Similar age, not first in list (or first but same gender)
    if abs(age_diff) <= 10 and rel_index > 0:
        if poi_gender == "male":
            # Male POI
            if same_last and rel_gender == "male":
                return ("Brother", 4)
            if rel_gender == "female" and rel_index < 5:
                if same_last:
                    return ("Sister", 4)
                # Different last name + female + over 30 = married sister
                if rel_age and rel_age > 30:
                    return ("Sister (married)", 4)
        elif poi_gender == "female":
            # Female POI
            if same_last and rel_gender == "female" and rel_index < 5:
                return ("Sister", 4)
            if same_last and rel_gender == "male" and rel_index < 5:
                return ("Brother", 4)
        # Generic sibling: same last, similar age, not first
        if same_last and abs(age_diff) <= 10:
            if rel_gender == "female":
                return ("Sister", 4)
            if rel_gender == "male":
                return ("Brother", 4)
            return ("Sibling", 4)

    # Wider sibling window (up to 15 years)
    if abs(age_diff) <= 15 and rel_index > 0 and same_last:
        if rel_gender == "female":
            return ("Sister", 4)
        if rel_gender == "male":
            return ("Brother", 4)
        return ("Sibling", 4)

    # Married sister: different last name, female, similar age
    if not same_last and rel_gender == "female" and abs(age_diff) <= 15 and rel_index > 0:
        if rel_age and rel_age > 30:
            return ("Sister (married)", 4)

    # --- Friend/Roommate (priority 5) ---
    # Address overlap but no familial connection
    if addr_overlap and not same_last and abs(age_diff) <= 15:
        return ("Friend/Roommate", 5)

    # --- Fallback ---
    if same_last:
        return ("Relative", 6)

    if addr_overlap:
        return ("Friend/Roommate", 5)

    return ("Relative", 6)


def _check_address_overlap(addrs_a, addrs_b) -> bool:
    """Check if any addresses overlap between two address lists."""
    if not addrs_a or not addrs_b:
        return False
    for a in addrs_a:
        for b in addrs_b:
            if _addresses_match(a.get("address", ""), b.get("address", "")):
                return True
    return False


def _normalize_addr(addr: str) -> str:
    """Normalize address for comparison."""
    addr = addr.upper().strip()
    addr = re.sub(r'\b(APT|UNIT|STE|SUITE|#)\s*\S+', '', addr)
    addr = re.sub(r'\s+', ' ', addr).strip().rstrip(',.')
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
            body = await page.inner_text("body")
            m = re.search(r'Result\s*:\s*\d+\s+of\s+\d+\s+(.+?)\s+at\s+(\d+)', body)
            if m:
                return {"name": m.group(1).strip(), "age": int(m.group(2)), "dob": ""}
            logger.debug("  Result header not found on page")
            return {}
        logger.debug(f"  Result header: {header_text[:100]}")
        m = re.search(r'Result\s*:\s*(\d+)\s+of\s+(\d+)\s+(.+?)\s+at\s+(\d+)\s*-\s*DOB:\s*(.*)', header_text)
        if m:
            return {"current": int(m.group(1)), "total": int(m.group(2)),
                    "name": m.group(3).strip(), "age": int(m.group(4)), "dob": m.group(5).strip()}
        m = re.search(r'Result\s*:\s*(\d+)\s+of\s+(\d+)\s+(.+?)\s+at\s+(\d+)', header_text)
        if m:
            return {"current": int(m.group(1)), "total": int(m.group(2)),
                    "name": m.group(3).strip(), "age": int(m.group(4)), "dob": ""}
        m = re.search(r'Result\s*:\s*(\d+)\s+of\s+(\d+)\s+(.+)', header_text)
        if m:
            return {"current": int(m.group(1)), "total": int(m.group(2)),
                    "name": m.group(3).strip(), "age": None, "dob": ""}
    except Exception as e:
        logger.debug(f"  Result header extraction error: {e}")
    return {}


async def _extract_result_count(page) -> tuple[int, int]:
    """Parse 'Result : X of N' and return (current_index, total_count)."""
    header = await _extract_result_header(page)
    return (header.get("current", 1), header.get("total", 1))


async def _extract_phones_from_section(page) -> list[dict]:
    """Extract phones from the Possible Phone Numbers section only."""
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


async def _extract_all_addresses(page) -> list[dict]:
    """Extract ALL addresses from Address History with date ranges.
    Returns [{"address": "123 Main St...", "dates": "01/2020 - Present", "is_current": True}, ...]
    """
    try:
        entries = await page.evaluate("""
            () => {
                const h = document.evaluate(
                    "//h5[contains(text(),'Address History')]",
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (!h) return [];
                const container = h.parentElement;
                const h6s = container.querySelectorAll('h6');
                return Array.from(h6s).map((h6, i) => {
                    const text = h6.textContent.trim();
                    // Try to find date range in sibling or child elements
                    const parent = h6.parentElement;
                    const allText = parent ? parent.innerText : text;
                    return { address: text, fullText: allText, index: i };
                });
            }
        """)
        result = []
        for entry in entries:
            addr = entry.get("address", "")
            full = entry.get("fullText", "")
            # Try to extract date range
            date_match = re.search(r'(\d{1,2}/\d{4})\s*[-–]\s*(Present|\d{1,2}/\d{4})', full)
            dates = date_match.group(0) if date_match else ""
            is_current = "present" in dates.lower() if dates else (entry.get("index", 0) == 0)
            result.append({"address": addr, "dates": dates, "is_current": is_current})
        return result
    except Exception as e:
        logger.warning(f"  Error extracting all addresses: {e}")
        return []


async def _extract_relatives_list(page) -> list[dict]:
    """Extract name+age from Possible Relatives section. No clicks, no credits."""
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
    """Check if the current result person is marked as deceased.
    Scoped to the result section (Indicators area) to reduce false positives.
    """
    try:
        # First try scoped check in the Indicators section
        scoped = await page.evaluate("""
            () => {
                // Check Indicators section first
                const h = document.evaluate(
                    "//h5[contains(text(),'Indicators')]",
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (h) {
                    const section = h.parentElement.innerText;
                    if (/\\bdeceased\\b/i.test(section) || /\\bdate of death\\b/i.test(section) || /\\bDOD:/i.test(section)) {
                        return true;
                    }
                }
                // Also check the result header area
                const ps = document.querySelectorAll('p');
                for (const p of ps) {
                    if (p.textContent.includes('Result') && /\\bdeceased\\b/i.test(p.textContent)) {
                        return true;
                    }
                }
                return false;
            }
        """)
        if scoped:
            return True
        # Fallback: check body but only in result-related sections
        body_text = await page.inner_text("body")
        # Look for "Deceased" near the person's name/header, not just anywhere
        if re.search(r'Result\s*:.*?deceased', body_text, re.IGNORECASE | re.DOTALL):
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Search execution helpers
# ---------------------------------------------------------------------------

async def _wait_for_results(page) -> bool:
    """Wait for results or no-results after executing a search. Returns True if results found."""
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
        await page.wait_for_timeout(3_000)

    await page.wait_for_timeout(1_000)  # let React settle

    body_text = await page.inner_text("body")
    if any(p in body_text.lower() for p in ["no results", "no records found", "0 results", "no matches"]):
        return False
    if "Property Details" not in body_text and "Address History" not in body_text:
        logger.warning("  Search completed but no Property Details found")
        return False
    return True


async def _click_get_info_and_confirm(page) -> None:
    """Click Get Info and confirm the Execute Search modal."""
    # Use :visible to get the correct Get Info button (Name vs Address form)
    get_info_btn = page.locator('button.pu_btn_user_search:visible').first
    await get_info_btn.click(timeout=10_000)

    confirm_btn = page.locator('button:has-text("Yes, Execute Search"):visible').first
    await confirm_btn.wait_for(state="visible", timeout=15_000)
    await confirm_btn.click()


async def _execute_search(page, first_name: str, last_name: str,
                          street: str = "", city: str = "", state: str = "TX",
                          zip_code: str = "") -> bool:
    """Fill the NAME search form and execute. Returns True if results loaded."""
    await page.goto(f"{SKIPGENIE_URL}/user/search", wait_until="networkidle", timeout=30_000)
    await _dismiss_popups(page)

    # Click "Name Search" tab to ensure we're on the right form
    try:
        name_tab = page.locator('li.tabs:has-text("Name")').first
        if await name_tab.is_visible(timeout=2_000):
            await name_tab.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass  # May already be on Name Search tab

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

    await _click_get_info_and_confirm(page)
    return await _wait_for_results(page)


async def _execute_address_search(page, street: str, city: str = "",
                                  state: str = "TX", zip_code: str = "") -> bool:
    """Fill the ADDRESS search form and execute. Returns True if results loaded."""
    await page.goto(f"{SKIPGENIE_URL}/user/search", wait_until="networkidle", timeout=30_000)
    await _dismiss_popups(page)

    # Click "Address Search" tab (li.tabs element, not a button)
    clicked_tab = False
    for selector in [
        'li.tabs:has-text("Address")',
        'li:has-text("Address Search")',
        'a:has-text("Address Search")',
        'button:has-text("Address Search")',
        'button:has-text("Address")',
    ]:
        try:
            tab_el = page.locator(selector).first
            if await tab_el.is_visible(timeout=2_000):
                await tab_el.click()
                await page.wait_for_timeout(500)
                clicked_tab = True
                logger.info(f"  Clicked Address Search tab via: {selector}")
                break
        except Exception:
            continue

    if not clicked_tab:
        logger.warning("  Could not find Address Search tab — filling address fields on current form")

    # Fill address fields only
    if street:
        await _react_fill(page, 'input[placeholder*="Street Address" i]', street.strip())
    if city:
        await _react_fill(page, 'input[placeholder*="City" i]', city.strip())
    if state:
        await _react_fill(page, 'input[placeholder*="State" i]', state.strip())
    if zip_code:
        await _react_fill(page, 'input[placeholder*="Zip" i], input[placeholder*="Postal" i]', zip_code.strip())

    await _click_get_info_and_confirm(page)
    return await _wait_for_results(page)


async def _navigate_to_result(page, target_index: int) -> bool:
    """Navigate to a specific result index in multi-result view.
    Returns True if successfully navigated to target.
    """
    current, total = await _extract_result_count(page)
    if target_index == current:
        return True
    if target_index < 1 or target_index > total:
        return False

    # Try clicking next/prev buttons to reach target
    max_attempts = total + 1
    for _ in range(max_attempts):
        current, _ = await _extract_result_count(page)
        if current == target_index:
            return True

        if target_index > current:
            # Try clicking "next" or ">" button
            clicked = await page.evaluate("""
                () => {
                    // Look for next/forward navigation buttons
                    const btns = document.querySelectorAll('button, a, span[role="button"]');
                    for (const btn of btns) {
                        const text = btn.textContent.trim().toLowerCase();
                        if (text === '>' || text === '›' || text === 'next' ||
                            text === '→' || btn.className.includes('next') ||
                            btn.className.includes('forward')) {
                            btn.click();
                            return true;
                        }
                    }
                    // Look for arrow icons
                    const arrows = document.querySelectorAll('[class*="arrow-right"], [class*="chevron-right"], [class*="fa-arrow-right"], [class*="fa-chevron-right"]');
                    for (const a of arrows) {
                        const clickable = a.closest('button, a, [role="button"]') || a;
                        clickable.click();
                        return true;
                    }
                    return false;
                }
            """)
            if not clicked:
                logger.warning(f"  Could not find 'next' button to navigate from result {current} to {target_index}")
                return False
        else:
            # Try clicking "prev" or "<" button
            clicked = await page.evaluate("""
                () => {
                    const btns = document.querySelectorAll('button, a, span[role="button"]');
                    for (const btn of btns) {
                        const text = btn.textContent.trim().toLowerCase();
                        if (text === '<' || text === '‹' || text === 'prev' || text === 'previous' ||
                            text === '←' || btn.className.includes('prev') ||
                            btn.className.includes('back')) {
                            btn.click();
                            return true;
                        }
                    }
                    const arrows = document.querySelectorAll('[class*="arrow-left"], [class*="chevron-left"], [class*="fa-arrow-left"], [class*="fa-chevron-left"]');
                    for (const a of arrows) {
                        const clickable = a.closest('button, a, [role="button"]') || a;
                        clickable.click();
                        return true;
                    }
                    return false;
                }
            """)
            if not clicked:
                logger.warning(f"  Could not find 'prev' button to navigate from result {current} to {target_index}")
                return False

        await page.wait_for_timeout(1_500)  # let result load

    return False


async def _find_owner_in_results(page, owner_first: str, owner_last: str,
                                 total_results: int):
    """Iterate through multi-result pages to find the owner.
    Returns the header dict for the matching result, or None if not found.
    Also captures the current resident (result 1 = most current dates).
    """
    current_resident = None

    for idx in range(1, min(total_results + 1, 11)):  # cap at 10
        if idx > 1:
            if not await _navigate_to_result(page, idx):
                break

        header = await _extract_result_header(page)
        result_name = header.get("name", "").upper()

        # Capture current resident (first result)
        if idx == 1:
            current_resident = header.get("name", "")

        # Check if this result matches the owner
        if owner_last.upper() in result_name and owner_first.upper() in result_name:
            logger.info(f"  Found owner at result {idx}: {result_name}")
            return {"header": header, "current_resident": current_resident, "result_index": idx}

        # Also try partial match (first name + last name separately)
        name_parts = result_name.split()
        if (owner_first.upper() in name_parts or
                any(owner_first.upper() in p for p in name_parts)):
            if (owner_last.upper() in name_parts or
                    any(owner_last.upper() in p for p in name_parts)):
                logger.info(f"  Found owner (partial match) at result {idx}: {result_name}")
                return {"header": header, "current_resident": current_resident, "result_index": idx}

    logger.info(f"  Owner '{owner_first} {owner_last}' not found in {total_results} results")
    return None if not current_resident else {"header": None, "current_resident": current_resident, "result_index": None}


# ---------------------------------------------------------------------------
# Relative h6 clicking
# ---------------------------------------------------------------------------

async def _click_relative_h6(page, rel_index: int, section: str = "Possible Relatives") -> bool:
    """Click the Nth h6 element in the relatives/associates section.
    Returns True if the relative's profile loaded.
    """
    try:
        clicked = await page.evaluate("""
            ([headingText, idx]) => {
                const h = document.evaluate(
                    `//h5[contains(text(),'${headingText}')]`,
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (!h) return false;
                const container = h.parentElement;
                const h6s = container.querySelectorAll('h6.skipg_seach_link_highlight');
                if (idx >= h6s.length) return false;
                h6s[idx].click();
                return true;
            }
        """, [section, rel_index])

        if not clicked:
            logger.warning(f"  Could not click h6 at index {rel_index} in {section}")
            return False

        # Wait for the relative's profile to load
        try:
            await page.wait_for_function("""
                () => {
                    const body = document.body.innerText;
                    return body.includes('Property Details') ||
                           body.includes('Address History') ||
                           body.includes('Possible Phone') ||
                           body.includes('No results') ||
                           body.includes('no results');
                }
            """, timeout=15_000)
        except Exception:
            await page.wait_for_timeout(3_000)

        await page.wait_for_timeout(1_000)

        body_text = await page.inner_text("body")
        if any(p in body_text.lower() for p in ["no results", "no records found"]):
            logger.info(f"  No results for relative at index {rel_index}")
            return False
        return True

    except Exception as e:
        logger.warning(f"  Error clicking relative h6 at index {rel_index}: {e}")
        return False


async def _extract_relative_profile(page, poi_addresses: list[dict]) -> dict:
    """Extract full profile from a relative's loaded result page."""
    result = {"phones": [], "current_address": "", "all_addresses": [],
              "is_deceased": False, "same_address": False}
    try:
        result["phones"] = (await _extract_phones_from_section(page))[:3]
        result["current_address"] = await _extract_current_address(page)
        result["all_addresses"] = await _extract_all_addresses(page)
        result["is_deceased"] = await _check_deceased(page)
        # Check address overlap with POI
        if poi_addresses and result["all_addresses"]:
            result["same_address"] = _check_address_overlap(poi_addresses, result["all_addresses"])
        elif poi_addresses and result["current_address"]:
            result["same_address"] = any(
                _addresses_match(result["current_address"], a.get("address", ""))
                for a in poi_addresses
            )
    except Exception as e:
        logger.warning(f"  Error extracting relative profile: {e}")
    return result


async def _navigate_back_to_poi(page, street: str, city: str, state: str,
                                zip_code: str, owner_first: str, owner_last: str) -> bool:
    """Navigate back to the POI's result after viewing a relative's profile.
    Tries multiple strategies with fallback to re-searching.
    """
    # Strategy 1: Press Escape (might close a sub-modal)
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1_000)
        header = await _extract_result_header(page)
        if header.get("name") and owner_last.upper() in header["name"].upper():
            logger.debug("  Back to POI via Escape")
            return True
    except Exception:
        pass

    # Strategy 2: Browser back
    try:
        await page.go_back()
        await page.wait_for_timeout(2_000)
        header = await _extract_result_header(page)
        if header.get("name") and owner_last.upper() in header["name"].upper():
            logger.debug("  Back to POI via go_back()")
            return True
    except Exception:
        pass

    # Strategy 3: Re-execute search (reliable fallback)
    # Use name search if no address, address search otherwise
    has_addr = bool(street and street.strip())
    if has_addr:
        logger.info("  Re-executing address search to return to POI...")
        found = await _execute_address_search(page, street, city, state, zip_code)
    else:
        logger.info("  Re-executing name search to return to POI...")
        found = await _execute_search(page, owner_first, owner_last, state=state)

    if not found:
        return False

    # Find the owner again (only needed for multi-result address search)
    if has_addr:
        _, total = await _extract_result_count(page)
        if total > 1:
            match = await _find_owner_in_results(page, owner_first, owner_last, total)
            if not match or not match.get("header"):
                return False
    else:
        header = await _extract_result_header(page)
        if not header.get("name"):
            return False

    return True


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

def _empty_result() -> dict:
    """Return a blank result dict with all expected columns."""
    result = {
        "phone_1": "", "phone_2": "", "phone_3": "",
        "phones_subject": "",
        "poi_age": "", "poi_deceased": "",
        "poi_current_address": "",
        "current_resident": "",
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
    ADDRESS-FIRST search flow:
    1. Search by property address
    2. Find owner among results (or note current resident)
    3. Extract owner data + relatives list
    4. Click relative h6 elements for their profiles
    Returns a flat dict with all columns for CSV output.
    """
    result = _empty_result()

    try:
        # Parse owner name
        has_owner_name = bool(name and name.strip())
        first_name, last_name, poi_gender = "", "", "unknown"
        if has_owner_name:
            primary_name = re.split(r'\s+[Aa]nd\s+', name)[0].strip()
            # Strip punctuation and suffixes from name parts
            primary_name = re.sub(r'[.\-,]', '', primary_name).strip()
            primary_name = re.sub(r'\b(Sr|Jr|II|III|IV)\b', '', primary_name, flags=re.IGNORECASE).strip()
            parts = primary_name.split()
            first_name = parts[0] if parts else primary_name
            last_name = parts[-1] if len(parts) > 1 else ""
            # Reject single-initial last names
            if len(last_name) <= 1:
                last_name = parts[-2] if len(parts) > 2 else ""
            poi_gender = _guess_gender(first_name)
            logger.info(f"  Owner: first='{first_name}' last='{last_name}' gender={poi_gender}")

        # Parse address — handles comma-separated and space-only formats
        street, city, state, zip_code = "", "", "TX", ""
        addr = address.strip().replace('. ', ', ').replace('.', ',')  # normalize periods to commas
        # Format: "street, city, ST ZIP"
        addr_match = re.match(
            r'^(.+?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
            addr, re.IGNORECASE
        )
        if addr_match:
            street, city, state, zip_code = addr_match.groups()
        else:
            # Format: "street, city ST ZIP" (no comma before state)
            addr_match2 = re.match(
                r'^(.+?),\s*(.+?)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
                addr, re.IGNORECASE
            )
            if addr_match2:
                street, city, state, zip_code = addr_match2.groups()
            else:
                # Format: "street CITY, ST ZIP" or "street CITY ST ZIP" (no commas)
                addr_match3 = re.match(
                    r'^(\d+\s+.+?)\s+([A-Z][a-zA-Z\s]+?),?\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
                    addr, re.IGNORECASE
                )
                if addr_match3:
                    street, city, state, zip_code = addr_match3.groups()
                    city = city.strip().rstrip(',')
                else:
                    partial = re.match(r'^(.+?),\s*([^,]+)', addr)
                    if partial:
                        street, city = partial.groups()
                    else:
                        street = addr
        logger.info(f"  Address: street='{street}' city='{city}' state='{state}' zip='{zip_code}'")

        # ---- Step 1: ADDRESS SEARCH (1 credit) ----
        has_address = bool(street.strip())
        found = False
        if has_address:
            try:
                found = await _execute_address_search(page, street, city, state, zip_code)
            except (PWTimeout, Exception) as e:
                logger.warning(f"  Address search failed: {e}")
                found = False
        else:
            logger.info(f"  No address — skipping address search")

        if not found and not has_address and has_owner_name and last_name:
            # No address: go straight to name search
            logger.info(f"  Searching by name only: '{first_name} {last_name}'")
            try:
                found = await _execute_search(page, first_name, last_name, state=state)
            except (PWTimeout, Exception) as e:
                logger.warning(f"  Name search failed: {e}")
                found = False
            if not found:
                result["skip_status"] = "not_found"
                return result
            header = await _extract_result_header(page)
        elif not found:
            logger.info(f"  No results for address: {address}")
            result["skip_status"] = "not_found"
            return result

        # ---- Step 2: Handle multi-result + find owner ----
        header = await _extract_result_header(page)
        current_idx = header.get("current", 1)
        total_results = header.get("total", 1)
        logger.info(f"  Address search: Result {current_idx} of {total_results}")

        # Note the current resident (first result = most recent dates)
        current_resident_name = header.get("name", "")
        result["current_resident"] = current_resident_name

        owner_found_via_address = True
        if has_owner_name and total_results > 1:
            # Find the owner among results
            match = await _find_owner_in_results(page, first_name, last_name, total_results)
            if match and match.get("header"):
                header = match["header"]
                result["current_resident"] = match.get("current_resident", current_resident_name)
            else:
                result["current_resident"] = match.get("current_resident", current_resident_name) if match else current_resident_name
                owner_found_via_address = False
        elif has_owner_name and total_results == 1:
            result_name = header.get("name", "").upper()
            if first_name.upper() not in result_name and (not last_name or last_name.upper() not in result_name):
                logger.info(f"  Single result '{header.get('name', '')}' doesn't match owner '{name}'")
                result["current_resident"] = header.get("name", "")
                owner_found_via_address = False

        # Fallback: if owner not found at address, try NAME search
        if not owner_found_via_address and has_owner_name and last_name:
            logger.info(f"  Owner not at address — falling back to name-only search for '{first_name} {last_name}'")
            name_found = await _execute_search(page, first_name, last_name, state=state)
            if name_found:
                header = await _extract_result_header(page)
                logger.info(f"  Name search found: {header.get('name', '')}")
            else:
                logger.info(f"  Name search also returned no results for '{first_name} {last_name}'")
                result["skip_status"] = "not_found"
                return result
        elif not owner_found_via_address:
            result["skip_status"] = "manual_review"
            logger.info(f"  Owner '{name}' not found — needs manual review")
            return result

        # ---- Step 3: Extract POI data ----
        poi_age = header.get("age")
        result["poi_age"] = str(poi_age) if poi_age else ""

        poi_deceased = await _check_deceased(page)
        result["poi_deceased"] = "Yes" if poi_deceased else ""

        # Extract phones (skip if deceased)
        if poi_deceased:
            poi_phones = []
            logger.info(f"  POI is deceased — skipping phones")
        else:
            phones = await _extract_phones_from_section(page)
            poi_phones = [p["number"] for p in phones[:3]]

        result["phone_1"] = poi_phones[0] if len(poi_phones) > 0 else ""
        result["phone_2"] = poi_phones[1] if len(poi_phones) > 1 else ""
        result["phone_3"] = poi_phones[2] if len(poi_phones) > 2 else ""
        result["phones_subject"] = ", ".join(poi_phones)

        # Extract ALL addresses for POI (needed for overlap checks)
        poi_all_addresses = await _extract_all_addresses(page)
        poi_current_addr = await _extract_current_address(page)
        result["poi_current_address"] = poi_current_addr

        logger.info(f"  POI: age={poi_age}, phones={len(poi_phones)}, "
                     f"deceased={poi_deceased}, addresses={len(poi_all_addresses)}")

        # ---- Step 4: Extract relatives + associates list (0 credits) ----
        relatives = await _extract_relatives_list(page)
        associates = await _extract_associates_list(page)
        logger.info(f"  Found {len(relatives)} relatives, {len(associates)} associates")

        # ---- Step 5: Rank relatives (initial ranking without address data) ----
        ranked = []
        for rel in relatives:
            label, priority = _infer_relationship(
                poi_age, last_name, poi_gender,
                rel["name"], rel["age"], rel["index"],
                poi_addresses=poi_all_addresses
            )
            ranked.append({**rel, "relationship": label, "priority": priority})

        for assoc in associates:
            ranked.append({**assoc, "relationship": "Associate", "priority": 7})

        ranked.sort(key=lambda r: (r["priority"], r["index"]))
        to_search = ranked[:max_relatives]
        logger.info(f"  Will search {len(to_search)} relatives: "
                     + ", ".join(f"{r['name']} ({r['relationship']})" for r in to_search))

        # ---- Step 6: Click each relative's h6 to get their profile ----
        all_rel_names = []
        all_rel_phones = []

        for i, rel in enumerate(to_search):
            idx = i + 1  # 1-based output index
            rel_list_index = rel["index"]  # 0-based index in the h6 list
            section = "Possible Relatives" if rel["source"] == "relative" else "Possible Associates"
            logger.info(f"    [{idx}/{len(to_search)}] Clicking relative: {rel['name']} ({rel['relationship']})")

            # Check cache first
            rel_parts = rel["name"].split()
            rel_first = rel_parts[0] if rel_parts else rel["name"]
            rel_last = rel_parts[-1] if len(rel_parts) > 1 else ""
            cache_k = _cache_key(rel_first, rel_last)

            if cache_k in _search_cache:
                logger.info(f"    Cache hit for {rel['name']}")
                rel_data = _search_cache[cache_k].copy()
                # Recalculate same_address for this POI
                if poi_all_addresses and rel_data.get("all_addresses"):
                    rel_data["same_address"] = _check_address_overlap(poi_all_addresses, rel_data["all_addresses"])
                elif poi_current_addr and rel_data.get("current_address"):
                    rel_data["same_address"] = _addresses_match(rel_data["current_address"], poi_current_addr)
            else:
                # Click the h6 element
                profile_loaded = await _click_relative_h6(page, rel_list_index, section)

                if profile_loaded:
                    rel_data = await _extract_relative_profile(page, poi_all_addresses)
                    _search_cache[cache_k] = rel_data.copy()

                    # Navigate back to POI for the next relative
                    if i < len(to_search) - 1:  # don't need to go back after last one
                        back_ok = await _navigate_back_to_poi(
                            page, street, city, state, zip_code, first_name, last_name
                        )
                        if not back_ok:
                            logger.warning(f"  Could not navigate back to POI after relative {rel['name']}")
                            # Still record what we got, but can't click more relatives
                            _populate_relative_result(result, idx, rel, rel_data, all_rel_names, all_rel_phones)
                            break
                else:
                    rel_data = {"phones": [], "current_address": "", "all_addresses": [],
                                "is_deceased": False, "same_address": False}
                    _search_cache[cache_k] = rel_data.copy()

            # Refine relationship with address overlap data
            if rel_data.get("all_addresses"):
                refined_label, refined_priority = _infer_relationship(
                    poi_age, last_name, poi_gender,
                    rel["name"], rel["age"], rel["index"],
                    rel_addresses=rel_data["all_addresses"],
                    poi_addresses=poi_all_addresses
                )
                rel["relationship"] = refined_label
                rel["priority"] = refined_priority

            _populate_relative_result(result, idx, rel, rel_data, all_rel_names, all_rel_phones)

        # Re-sort by refined priority and re-assign slots
        # (Only matters if priorities changed after address overlap data)
        # For now, keep the order as-is since we already populated result dict

        # Backward compat fields
        result["relatives_names"] = ", ".join(all_rel_names)
        result["phones_relatives"] = ", ".join(all_rel_phones)
        # Mark as "found" if we got any useful data (phones, deceased info, relatives)
        has_phones = bool(poi_phones or all_rel_phones)
        has_data = bool(result.get("poi_age") or result.get("poi_deceased") or all_rel_names)
        result["skip_status"] = "found" if has_phones or has_data else "not_found"

        logger.info(
            f"  {name or address}: {len(poi_phones)} POI phone(s), "
            f"{len(all_rel_phones)} relative phone(s) from {len(to_search)} relatives"
        )

    except PWTimeout:
        logger.warning(f"  Timeout searching for: {name or address}")
        result["skip_status"] = "error"
    except Exception as e:
        logger.error(f"  Error searching for '{name or address}': {e}")
        result["skip_status"] = "error"

    return result


def _populate_relative_result(result: dict, idx: int, rel: dict, rel_data: dict,
                              all_rel_names: list, all_rel_phones: list) -> None:
    """Populate the result dict for a single relative."""
    result[f"rel_{idx}_name"] = rel["name"]
    result[f"rel_{idx}_relationship"] = rel["relationship"]

    rel_phones = [p["number"] for p in rel_data.get("phones", [])]
    if rel_data.get("is_deceased"):
        result[f"rel_{idx}_deceased"] = "Yes"
        rel_phones = []
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


def _format_phone(raw: str) -> str:
    """Normalize to 10-digit string."""
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

    username = os.getenv("SKIPGENIE_USERNAME", "") or os.getenv("SKIPGENIE_EMAIL", "")
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
            logger.info(f"[{i}/{len(records)}] Searching: {name or '(no name)'} — {address}")

            skip_data = await search_person(page, name, address, max_relatives=max_relatives)
            enriched.append({**rec, **skip_data})

        await page.wait_for_timeout(2_000)
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
