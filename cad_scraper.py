"""
CAD Scraper — search Texas County Appraisal District websites for property data.

Supports 3 counties (no CAPTCHA):
  - Williamson (WCAD) — JSON API
  - Hays — BIS platform via Playwright
  - Bastrop — BIS platform via Playwright

Usage:
    python cad_scraper.py --county williamson --search "Silver Homes" [--type owner|address]
    python cad_scraper.py --county hays --search "123 Main St" --type address
    python cad_scraper.py --county all --search "Silver Homes" --output results.csv
    python cad_scraper.py --list-counties

Output CSV columns:
    county, property_id, owner_name, property_address, market_value, assessed_value,
    legal_description, mailing_address
"""

import argparse
import csv
import logging
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(SCRIPT_DIR, ".cad_session")

COUNTIES = {
    "williamson": {"name": "Williamson County (WCAD)", "method": "api"},
    "hays":       {"name": "Hays County (Hays CAD)", "method": "playwright"},
    "bastrop":    {"name": "Bastrop County (Bastrop CAD)", "method": "playwright"},
    "bell":       {"name": "Bell County (Bell CAD)", "method": "playwright"},
    "burnet":     {"name": "Burnet County (Burnet CAD)", "method": "playwright"},
    "travis":     {"name": "Travis County (TCAD)", "method": "playwright"},
}

OUTPUT_COLUMNS = [
    "county", "property_id", "owner_name", "property_address",
    "mailing_address", "market_value", "assessed_value",
    "year_built", "sqft", "lot_size", "bedrooms", "legal_description",
    "deed_history",
]


# ── Williamson County (JSON API) ──────────────────────────────────

def search_wcad(query: str, search_type: str = "owner") -> list[dict]:
    """Search Williamson CAD via their JSON API."""
    url = "https://search.wcad.org/ProxyT/Search/Properties/quick/"
    params = {
        "f": query,
        "pn": 1,
        "st": 4,
        "so": "desc",
        "pt": "RP;PP;MH;NR",
        "ty": 2025,
    }

    try:
        resp = requests.get(url, params=params, timeout=30,
                           headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("WCAD search failed: %s", e)
        return []

    results = []
    for item in data.get("ResultList", []):
        owner = item.get("OwnerName", "")
        situs = item.get("SitusAddress", "")
        prop_id = item.get("PropertyQuickRefID", "")

        # Filter by search type
        if search_type == "owner" and query.lower() not in owner.lower():
            continue
        if search_type == "address" and query.lower() not in situs.lower():
            continue

        results.append({
            "county": "Williamson",
            "property_id": prop_id,
            "owner_name": owner,
            "property_address": situs,
            "mailing_address": "",
            "market_value": "",
            "assessed_value": "",
            "year_built": "",
            "sqft": "",
            "lot_size": "",
            "bedrooms": "",
            "legal_description": "",
            "deed_history": "",
        })

    # Get detail for each result (market value, mailing address)
    for r in results[:10]:  # limit detail lookups
        detail = get_wcad_detail(r["property_id"])
        r.update(detail)
        time.sleep(0.5)

    logger.info("WCAD: found %d result(s) for %r", len(results), query)
    return results


def get_wcad_detail(prop_id: str) -> dict:
    """Fetch property detail from WCAD."""
    url = f"https://search.wcad.org/Property-Detail?PropertyQuickRefID={prop_id}&TaxYear=2025"
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning("WCAD detail fetch failed for %s: %s", prop_id, e)
        return {}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text()

    detail = {}

    # Market value
    mv_match = re.search(r'Total Market Value\s*\$?([\d,]+)', text)
    if mv_match:
        detail["market_value"] = "$" + mv_match.group(1)

    # Assessed value
    av_match = re.search(r'Total Assessed Value\s*\$?([\d,]+)', text)
    if av_match:
        detail["assessed_value"] = "$" + av_match.group(1)

    # Mailing address
    mail_match = re.search(r'Mailing Address\s+(.*?)(?:\n|Owner|Property)', text, re.DOTALL)
    if mail_match:
        detail["mailing_address"] = " ".join(mail_match.group(1).split()).strip()

    # Legal description
    legal_match = re.search(r'Legal Description\s+(.*?)(?:\n\n|Neighborhood|Value)', text, re.DOTALL)
    if legal_match:
        detail["legal_description"] = " ".join(legal_match.group(1).split()).strip()[:200]

    # Property specs — year built, sqft, bedrooms
    yb_match = re.search(r'YEAR BUILT\s+SQ\.\s*FT.*?\n\s*.*?\n\s*(\d{4})\s+([\d,]+)', text, re.DOTALL)
    if yb_match:
        detail["year_built"] = yb_match.group(1)
        detail["sqft"] = yb_match.group(2)

    bed_match = re.search(r'Bedrooms\s+(\d+)', text)
    if bed_match:
        detail["bedrooms"] = bed_match.group(1)

    # Lot size from legal description (ACRES X.XX)
    lot_match = re.search(r'ACRES?\s+([\d.]+)', detail.get("legal_description", ""), re.IGNORECASE)
    if lot_match:
        detail["lot_size"] = lot_match.group(1) + " acres"

    # Deed / transfer history — structured as consecutive lines:
    # date, seller, buyer, instrument#, volume/page (each on its own line)
    lines = text.split('\n')
    deed_entries = []
    in_deed = False
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if 'DEED DATE' in line:
            in_deed = True
            i += 1
            continue
        if in_deed and not line:
            i += 1
            continue
        if in_deed and re.match(r'\d{1,2}/\d{1,2}/\d{4}', line):
            date = line
            seller = lines[i + 1].strip() if i + 1 < len(lines) else ""
            buyer = lines[i + 2].strip() if i + 2 < len(lines) else ""
            if seller and buyer and not re.match(r'\d{1,2}/\d{1,2}/\d{4}', seller):
                deed_entries.append(f"{date}: {seller} -> {buyer}")
            i += 4  # skip past instrument#/volume
            continue
        if in_deed and any(k in line.upper() for k in ['SALES HISTORY', 'IMPROVEMENT', 'BUILDING']):
            break
        i += 1

    if deed_entries:
        detail["deed_history"] = " | ".join(deed_entries[:10])

    return detail


# ── BIS Platform (Hays, Bastrop, Bell, Burnet) via Playwright ──────

def search_bis(county: str, query: str, search_type: str = "owner") -> list[dict]:
    """Search a BIS-platform CAD via Playwright browser automation."""
    from playwright.sync_api import sync_playwright

    base_urls = {
        "hays": "https://esearch.hayscad.com",
        "bastrop": "https://esearch.bastropcad.org",
        "bell": "https://esearch.bellcad.org",
        "burnet": "https://esearch.burnet-cad.org",
    }

    base_url = base_urls.get(county)
    if not base_url:
        logger.error("Unknown BIS county: %s", county)
        return []

    county_name = COUNTIES[county]["name"].split("(")[0].strip()

    results = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=True,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            # Navigate to search page
            page.goto(f"{base_url}/Search/Result?keywords={query.replace(' ', '+')}&TaxYear=2025",
                      timeout=30000, wait_until="networkidle")
            time.sleep(3)

            # Wait for Blazor to render results
            page.wait_for_selector("#results-page", timeout=10000)
            time.sleep(2)

            # Try to find result rows — BIS uses a grid with rows
            rows = page.locator("tr.search-result-row, .result-item, [data-propertyid]").all()

            if not rows:
                # Try alternative: look for any links to property detail
                rows = page.locator("a[href*='Property/Detail'], a[href*='property']").all()

            if not rows:
                # Last resort: scrape visible text for property-like data
                body_text = page.inner_text("body")
                logger.info("BIS %s: no structured results found. Page text sample: %s",
                           county, body_text[:300])
                context.close()
                return []

            for row in rows[:20]:  # limit to 20 results
                try:
                    text = row.inner_text(timeout=2000)
                    # Parse the row text — BIS typically shows: PropertyID | OwnerName | Address | Value
                    parts = [p.strip() for p in text.split('\n') if p.strip()]

                    result = {
                        "county": county_name,
                        "property_id": "",
                        "owner_name": "",
                        "property_address": "",
                        "mailing_address": "",
                        "market_value": "",
                        "assessed_value": "",
                        "year_built": "",
                        "sqft": "",
                        "lot_size": "",
                        "bedrooms": "",
                        "legal_description": "",
                        "deed_history": "",
                    }

                    # Try to extract structured data
                    for part in parts:
                        if re.match(r'^[RP]\d+', part):
                            result["property_id"] = part
                        elif '$' in part:
                            result["market_value"] = part
                        elif re.match(r'^\d+\s+[A-Z]', part):
                            result["property_address"] = part
                        elif part and not result["owner_name"]:
                            result["owner_name"] = part

                    if result["owner_name"] or result["property_address"]:
                        results.append(result)
                except Exception:
                    continue

            # Try clicking into detail pages for richer data
            for r in results[:5]:
                if r["property_id"]:
                    try:
                        detail = _get_bis_detail(page, base_url, r["property_id"])
                        r.update(detail)
                        time.sleep(1)
                    except Exception:
                        pass

        except Exception as e:
            logger.error("BIS %s search failed: %s", county, e)
        finally:
            context.close()

    logger.info("BIS %s: found %d result(s) for %r", county, len(results), query)
    return results


def _get_bis_detail(page, base_url: str, property_id: str) -> dict:
    """Navigate to a BIS property detail page and scrape data."""
    detail = {}
    try:
        page.goto(f"{base_url}/Property/Detail/{property_id}?TaxYear=2025",
                  timeout=15000, wait_until="networkidle")
        time.sleep(2)

        text = page.inner_text("body")

        mv = re.search(r'(?:Market|Appraised)\s+Value[:\s]*\$?([\d,]+)', text, re.IGNORECASE)
        if mv:
            detail["market_value"] = "$" + mv.group(1)

        av = re.search(r'Assessed\s+Value[:\s]*\$?([\d,]+)', text, re.IGNORECASE)
        if av:
            detail["assessed_value"] = "$" + av.group(1)

        mail = re.search(r'Mailing\s+Address[:\s]*(.*?)(?:\n|Owner|Exemptions)', text, re.DOTALL | re.IGNORECASE)
        if mail:
            detail["mailing_address"] = " ".join(mail.group(1).split()).strip()

        legal = re.search(r'Legal\s+Description[:\s]*(.*?)(?:\n\n|Value|Exemptions)', text, re.DOTALL | re.IGNORECASE)
        if legal:
            detail["legal_description"] = " ".join(legal.group(1).split()).strip()[:200]

    except Exception as e:
        logger.debug("BIS detail scrape failed for %s: %s", property_id, e)

    return detail


# ── Travis CAD (Prodigy platform) via Playwright ──────────────────

def search_travis(query: str, search_type: str = "owner") -> list[dict]:
    """Search Travis CAD via Playwright (Prodigy CAD platform)."""
    from playwright.sync_api import sync_playwright

    results = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=True,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto("https://stage.travis.prodigycad.com/property-search",
                      timeout=30000, wait_until="networkidle")
            time.sleep(3)

            # Find and fill the search input
            search_input = page.locator(
                "input[placeholder*='property'], input[placeholder*='search'], "
                "input[placeholder*='Enter'], input[type='search'], input[type='text']"
            ).first

            search_input.click()
            search_input.fill(query)
            time.sleep(1)

            # Press Enter or click search button
            try:
                search_btn = page.locator(
                    "button:has-text('Search'), button[type='submit'], "
                    "button:has-text('search'), [aria-label='Search']"
                ).first
                if search_btn.is_visible(timeout=2000):
                    search_btn.click()
                else:
                    search_input.press("Enter")
            except Exception:
                search_input.press("Enter")

            time.sleep(5)

            # Scrape results
            body_text = page.inner_text("body")

            # Look for property rows in results
            # Prodigy typically shows: Property ID | Owner | Address | Value
            lines = [l.strip() for l in body_text.split('\n') if l.strip()]

            i = 0
            while i < len(lines):
                line = lines[i]
                # Look for property ID patterns (Travis uses R + digits)
                if re.match(r'^R?\d{5,}', line):
                    result = {
                        "county": "Travis",
                        "property_id": line,
                        "owner_name": "",
                        "property_address": "",
                        "mailing_address": "",
                        "market_value": "",
                        "assessed_value": "",
                        "year_built": "",
                        "sqft": "",
                        "lot_size": "",
                        "bedrooms": "",
                        "legal_description": "",
                        "deed_history": "",
                    }

                    # Next lines usually have owner and address
                    for j in range(1, 5):
                        if i + j >= len(lines):
                            break
                        next_line = lines[i + j]
                        if '$' in next_line:
                            result["market_value"] = next_line
                        elif re.match(r'^\d+\s+[A-Z]', next_line) and not result["property_address"]:
                            result["property_address"] = next_line
                        elif next_line and not result["owner_name"] and not next_line.startswith('R'):
                            result["owner_name"] = next_line

                    if result["owner_name"] or result["property_address"]:
                        # Filter by search type
                        if search_type == "owner" and query.lower() in result["owner_name"].lower():
                            results.append(result)
                        elif search_type == "address" and query.lower() in result["property_address"].lower():
                            results.append(result)
                        elif search_type not in ("owner", "address"):
                            results.append(result)
                    i += 5
                else:
                    i += 1

            # If no structured results, try clicking on first result link
            if not results:
                links = page.locator("a[href*='property'], a[href*='detail'], tr[class*='result']").all()
                for link in links[:5]:
                    try:
                        text = link.inner_text(timeout=1000)
                        if query.lower() in text.lower():
                            result = {
                                "county": "Travis",
                                "property_id": "",
                                "owner_name": "",
                                "property_address": "",
                                "mailing_address": "",
                                "market_value": "",
                                "assessed_value": "",
                                "year_built": "",
                                "sqft": "",
                                "lot_size": "",
                                "bedrooms": "",
                                "legal_description": "",
                                "deed_history": "",
                            }
                            parts = [p.strip() for p in text.split('\n') if p.strip()]
                            for part in parts:
                                if re.match(r'^\d+\s+[A-Z]', part) and not result["property_address"]:
                                    result["property_address"] = part
                                elif '$' in part:
                                    result["market_value"] = part
                                elif part and not result["owner_name"]:
                                    result["owner_name"] = part
                            if result["owner_name"] or result["property_address"]:
                                results.append(result)
                    except Exception:
                        continue

        except Exception as e:
            logger.error("Travis CAD search failed: %s", e)
        finally:
            context.close()

    logger.info("Travis CAD: found %d result(s) for %r", len(results), query)
    return results


# ── Unified Search ─────────────────────────────────────────────────

def search_cad(county: str, query: str, search_type: str = "owner") -> list[dict]:
    """Search a specific county CAD."""
    if county not in COUNTIES:
        logger.error("Unknown county: %s. Available: %s", county, ", ".join(COUNTIES.keys()))
        return []

    method = COUNTIES[county]["method"]
    if method == "api":
        return search_wcad(query, search_type)
    elif county == "travis":
        return search_travis(query, search_type)
    elif method == "playwright":
        return search_bis(county, query, search_type)
    return []


def search_all(query: str, search_type: str = "owner") -> list[dict]:
    """Search all supported counties."""
    all_results = []
    for county in COUNTIES:
        results = search_cad(county, query, search_type)
        all_results.extend(results)
        time.sleep(1)
    return all_results


# ── CSV Output ─────────────────────────────────────────────────────

def write_results(results: list[dict], output_path: str):
    """Write results to CSV."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    logger.info("Wrote %d results to %s", len(results), output_path)


# ── CLI ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Search Texas CAD websites for property data"
    )
    parser.add_argument(
        "--county", default="all",
        help=f"County to search: {', '.join(COUNTIES.keys())}, or 'all' (default: all)",
    )
    parser.add_argument(
        "--search", required=False,
        help="Search query (owner name or property address)",
    )
    parser.add_argument(
        "--type", default="owner", choices=["owner", "address"],
        help="Search by owner name or address (default: owner)",
    )
    parser.add_argument(
        "--output", default="cad_results.csv",
        help="Output CSV path (default: cad_results.csv)",
    )
    parser.add_argument(
        "--list-counties", action="store_true",
        help="List supported counties and exit",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.list_counties:
        print("Supported counties:")
        for key, info in COUNTIES.items():
            print(f"  {key:12s}  {info['name']:40s}  ({info['method']})")
        return

    if not args.search:
        parser.error("--search is required (unless using --list-counties)")

    if args.county == "all":
        results = search_all(args.search, args.type)
    else:
        results = search_cad(args.county, args.search, args.type)

    if results:
        write_results(results, args.output)
        print(f"\n{len(results)} result(s) written to {args.output}")
        for r in results:
            print(f"  {r['county']:12s} | {r['owner_name']:30s} | {r['property_address']}")
    else:
        print("No results found.")


if __name__ == "__main__":
    main()
