"""
skipgenie.py
------------
Playwright automation for Skip Genie bulk skip tracing.

For each lead in the input CSV, logs into Skip Genie, searches by name +
address, and pulls the subject's phone numbers plus close relatives' numbers.

Outputs an enriched CSV with columns:
  phones_subject   — comma-separated numbers for the property owner
  phones_relatives — comma-separated numbers for relatives found
  relatives_names  — comma-separated relative names
  skip_status      — "found" | "not_found" | "error"

Usage:
    python skipgenie.py --input leads_with_equity.csv --output leads_traced.csv
    python skipgenie.py --input leads.csv --output leads_traced.csv --headless false
"""

import argparse
import asyncio
import csv
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()
logger = logging.getLogger(__name__)

SKIPGENIE_URL = "https://app.skipgenie.com"

# ---------------------------------------------------------------------------
# Skip Genie automation
# ---------------------------------------------------------------------------

async def login(page, username: str, password: str) -> None:
    await page.goto(f"{SKIPGENIE_URL}/login", wait_until="networkidle")
    await page.fill('input[type="email"], input[name="email"], input[id*="email"]', username)
    await page.fill('input[type="password"]', password)
    await page.click('button[type="submit"], input[type="submit"]')
    await page.wait_for_url(f"{SKIPGENIE_URL}/**", timeout=20_000)
    logger.info("Logged into Skip Genie.")


async def search_person(page, name: str, address: str) -> dict:
    """
    Navigate to the search page, enter name + address, and extract results.
    Returns a dict with phones_subject, phones_relatives, relatives_names, skip_status.
    """
    result = {
        "phones_subject": "",
        "phones_relatives": "",
        "relatives_names": "",
        "skip_status": "not_found",
    }

    try:
        # Go to search / new search page
        await page.goto(f"{SKIPGENIE_URL}/search", wait_until="networkidle", timeout=30_000)

        # Fill name field
        name_field = page.locator(
            'input[placeholder*="name" i], input[name*="name" i], input[id*="name" i]'
        ).first
        await name_field.fill(name, timeout=10_000)

        # Fill address field if present
        addr_field = page.locator(
            'input[placeholder*="address" i], input[name*="address" i], input[id*="address" i]'
        ).first
        try:
            await addr_field.fill(address, timeout=5_000)
        except Exception:
            pass  # address field optional on some search forms

        # Submit search
        await page.click('button[type="submit"], button:has-text("Search"), input[type="submit"]')
        await page.wait_for_load_state("networkidle", timeout=30_000)

        # Check for no-results state
        no_results = page.locator(
            ':text("No results"), :text("no records found"), :text("0 results")'
        )
        if await no_results.count() > 0:
            result["skip_status"] = "not_found"
            logger.info(f"  No results for: {name}")
            return result

        # Click the first result to open the detail view
        first_result = page.locator(
            'table tbody tr:first-child, .result-row:first-child, .search-result:first-child'
        ).first
        await first_result.click(timeout=10_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)

        # ---- Extract subject phone numbers ----
        subject_phones = await _extract_phones(
            page,
            section_selector='[class*="phone"], [id*="phone"], :text("Phone Numbers") ~ * a[href^="tel:"]',
        )

        # ---- Extract relatives ----
        relatives, rel_phones = await _extract_relatives(page)

        result["phones_subject"] = ", ".join(subject_phones)
        result["phones_relatives"] = ", ".join(rel_phones)
        result["relatives_names"] = ", ".join(relatives)
        result["skip_status"] = "found" if subject_phones or rel_phones else "not_found"
        logger.info(
            f"  {name}: {len(subject_phones)} subject phone(s), "
            f"{len(rel_phones)} relative phone(s)"
        )

    except PWTimeout:
        logger.warning(f"  Timeout searching for: {name}")
        result["skip_status"] = "error"
    except Exception as e:
        logger.error(f"  Error searching for '{name}': {e}")
        result["skip_status"] = "error"

    return result


async def _extract_phones(page, section_selector: str) -> list[str]:
    """Pull all E.164-ish phone numbers from the page or a section."""
    import re
    phones = set()

    # Try tel: links first (most reliable)
    tel_links = await page.locator('a[href^="tel:"]').all()
    for link in tel_links:
        href = await link.get_attribute("href")
        if href:
            num = re.sub(r"[^\d+]", "", href)
            if len(num) >= 10:
                phones.add(_format_phone(num))

    # Fallback: scan visible text for phone patterns
    if not phones:
        body = await page.inner_text("body")
        found = re.findall(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}", body)
        for f in found:
            phones.add(_format_phone(f))

    return list(phones)


async def _extract_relatives(page) -> tuple[list[str], list[str]]:
    """Return (names, phones) for relatives listed on the detail page."""
    import re
    names: list[str] = []
    phones: list[str] = []

    # Look for a relatives / associates section
    section = page.locator(
        ':text("Relatives"), :text("Associates"), :text("Household Members")'
    ).first
    try:
        await section.wait_for(timeout=5_000)
        # Grab the container after the heading
        parent = page.locator(
            '[class*="relative"], [class*="associate"], [id*="relative"]'
        )
        rows = await parent.all()
        for row in rows:
            text = await row.inner_text()
            # Parse name (first line) and any phone in the block
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if lines:
                names.append(lines[0])
            row_phones = re.findall(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}", text)
            for p in row_phones:
                phones.append(_format_phone(p))
    except Exception:
        pass  # no relatives section found

    return names, phones


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

async def run(input_csv: str, output_csv: str, headless: bool = True) -> None:
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
        browser = await pw.chromium.launch(headless=headless)
        page = await browser.new_page(ignore_https_errors=True)

        await login(page, username, password)

        for i, rec in enumerate(records, 1):
            name = rec.get("owner_name", "").strip()
            address = rec.get("property_address", "").strip()
            logger.info(f"[{i}/{len(records)}] Searching: {name} — {address}")

            skip_data = await search_person(page, name, address)
            enriched.append({**rec, **skip_data})

        await browser.close()

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
    parser.add_argument("--debug",    action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    headless = args.headless.lower() != "false"
    asyncio.run(run(args.input, args.output, headless=headless))


if __name__ == "__main__":
    main()
