"""
county_downloader.py
--------------------
Downloads foreclosure notice PDFs from Texas county clerk websites
into the input_pdfs/ folder, organized by county subdirectories.

Supported counties:
  - Bastrop
  - Burnet
  - Hays
  - Bell
  - Travis
  - Williamson

Usage:
    python county_downloader.py                    # download all counties
    python county_downloader.py --county hays bell # download specific counties
    python county_downloader.py --output ./my_pdfs # custom output folder
    python county_downloader.py --list             # list available counties
"""

import argparse
import csv
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("downloader.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_filename(name: str) -> str:
    """Sanitize a string into a safe filename."""
    name = unquote(name)
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    return name if name else 'unnamed.pdf'


def download_pdf(url: str, dest: Path, state: dict = None, output_dir: Path = None) -> bool:
    """Download a single PDF. Returns True on success."""
    # Check state first (persists across machines via git)
    if state is not None and output_dir is not None:
        rel_key = str(dest.relative_to(output_dir))
        if rel_key in state.get("downloaded_pdfs", {}):
            logger.info(f"  Already tracked in state, skipping: {dest.name}")
            return True

    if dest.exists():
        logger.info(f"  Already exists, skipping: {dest.name}")
        return True
    try:
        resp = SESSION.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        size_kb = len(resp.content) / 1024
        logger.info(f"  Downloaded: {dest.name} ({size_kb:.0f} KB)")

        # Record in state
        if state is not None and output_dir is not None:
            from state import mark_downloaded
            rel_key = str(dest.relative_to(output_dir))
            mark_downloaded(state, rel_key, url, len(resp.content))

        return True
    except requests.RequestException as e:
        logger.error(f"  Failed to download {url}: {e}")
        return False


def fetch_page(url: str) -> Optional[BeautifulSoup]:
    """Fetch a page and return parsed HTML."""
    try:
        resp = SESSION.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        logger.error(f"  Failed to fetch {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# County scrapers
# ---------------------------------------------------------------------------

def download_bastrop(output_dir: Path, state: dict = None) -> int:
    """
    Bastrop County – https://www.bastropcounty.gov/page/co.county_clerk _foreclosure
    Scrapes all PDF links from the foreclosure page.
    """
    county_dir = output_dir / "bastrop"
    county_dir.mkdir(parents=True, exist_ok=True)

    base_url = "https://www.bastropcounty.gov/page/co.county_clerk%20_foreclosure"
    soup = fetch_page(base_url)
    if not soup:
        return 0

    count = 0
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.lower().endswith(".pdf") or "/upload/" in href.lower():
            if not href.lower().endswith(".pdf"):
                continue
            full_url = urljoin(base_url, href)
            filename = safe_filename(href.split("/")[-1].split("?")[0])
            if download_pdf(full_url, county_dir / filename, state, output_dir):
                count += 1
            time.sleep(0.5)
    return count


def download_burnet(output_dir: Path, state: dict = None) -> int:
    """
    Burnet County – https://www.burnetcountytexas.org/page/cclerk.foreclose
    All PDFs are under /upload/page/ paths.
    """
    county_dir = output_dir / "burnet"
    county_dir.mkdir(parents=True, exist_ok=True)

    base_url = "https://www.burnetcountytexas.org/page/cclerk.foreclose"
    soup = fetch_page(base_url)
    if not soup:
        return 0

    count = 0
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.lower().endswith(".pdf"):
            full_url = urljoin(base_url, href)
            filename = safe_filename(href.split("/")[-1].split("?")[0])
            if download_pdf(full_url, county_dir / filename, state, output_dir):
                count += 1
            time.sleep(0.5)
    return count


def download_hays(output_dir: Path, state: dict = None) -> int:
    """
    Hays County – https://www.hayscountytx.gov/county-clerk/foreclosures
    PDFs are hosted on irp.cdn-website.com.
    """
    county_dir = output_dir / "hays"
    county_dir.mkdir(parents=True, exist_ok=True)

    base_url = "https://www.hayscountytx.gov/county-clerk/foreclosures"
    soup = fetch_page(base_url)
    if not soup:
        return 0

    count = 0
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.lower().endswith(".pdf"):
            full_url = urljoin(base_url, href)
            filename = safe_filename(unquote(href.split("/")[-1].split("?")[0]))
            if download_pdf(full_url, county_dir / filename, state, output_dir):
                count += 1
            time.sleep(0.5)
    return count


def download_bell(output_dir: Path, state: dict = None) -> int:
    """
    Bell County – https://www.bellcountytx.com/county_government/county_clerk/foreclosures.php
    Site has <base href="https://www.bellcountytx.com/"> so relative PDF links
    resolve from the site root, not the page directory.
    """
    county_dir = output_dir / "bell"
    county_dir.mkdir(parents=True, exist_ok=True)

    page_url = "https://www.bellcountytx.com/county_government/county_clerk/foreclosures.php"
    soup = fetch_page(page_url)
    if not soup:
        return 0

    # Respect <base> tag if present
    base_tag = soup.find("base", href=True)
    base_url = base_tag["href"] if base_tag else page_url

    count = 0
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.lower().endswith(".pdf") or ".pdf?" in href.lower():
            full_url = urljoin(base_url, href)
            filename = safe_filename(href.split("/")[-1].split("?")[0])
            if download_pdf(full_url, county_dir / filename, state, output_dir):
                count += 1
            time.sleep(0.5)
    return count


def download_williamson(output_dir: Path, state: dict = None) -> int:
    """
    Williamson County – https://www.wilcotx.gov/308/Foreclosure-Trustee-Sales
    Uses CivicPlus DocumentCenter. Scrapes the page and also checks
    the DocumentCenter API for foreclosure documents.
    """
    county_dir = output_dir / "williamson"
    county_dir.mkdir(parents=True, exist_ok=True)

    count = 0

    # Try the main page first
    base_url = "https://www.wilcotx.gov/308/Foreclosure-Trustee-Sales"
    soup = fetch_page(base_url)
    if soup:
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if ".pdf" in href.lower() or "/DocumentCenter/" in href:
                full_url = urljoin(base_url, href)
                # Try to get a good filename
                link_text = link.get_text(strip=True)
                if link_text and "pdf" not in link_text.lower()[:10]:
                    filename = safe_filename(link_text) + ".pdf"
                else:
                    filename = safe_filename(href.split("/")[-1].split("?")[0])
                if not filename.endswith(".pdf"):
                    filename += ".pdf"
                if download_pdf(full_url, county_dir / filename, state, output_dir):
                    count += 1
                time.sleep(0.5)

    # Also try the DocumentCenter folder for foreclosures
    doc_center_url = "https://www.wilcotx.gov/DocumentCenter/Index/63"
    soup2 = fetch_page(doc_center_url)
    if soup2:
        for link in soup2.find_all("a", href=True):
            href = link["href"]
            if "/DocumentCenter/View/" in href:
                full_url = urljoin(doc_center_url, href)
                link_text = link.get_text(strip=True)
                if link_text:
                    filename = safe_filename(link_text)
                    if not filename.endswith(".pdf"):
                        filename += ".pdf"
                else:
                    filename = safe_filename(href.split("/")[-1].split("?")[0]) + ".pdf"
                if download_pdf(full_url, county_dir / filename, state, output_dir):
                    count += 1
                time.sleep(0.5)

    return count


def _get_first_tuesdays(months_ahead: int = 3) -> list:
    """Return the first Tuesday of each month for the next N months."""
    today = date.today()
    dates = []
    for offset in range(months_ahead + 1):
        # Start of target month
        year = today.year + (today.month + offset - 1) // 12
        month = (today.month + offset - 1) % 12 + 1
        first_day = date(year, month, 1)
        # First Tuesday: weekday() == 1
        days_ahead = (1 - first_day.weekday()) % 7
        first_tuesday = first_day + timedelta(days=days_ahead)
        dates.append(first_tuesday)
    return dates


def download_travis(output_dir: Path, state: dict = None) -> int:
    """
    Travis County – via travis.texas.realforeclose.com (RealAuction).
    Travis doesn't host foreclosure notice PDFs. Instead, listings are on
    the RealAuction platform. This scraper uses Playwright to extract
    property data from upcoming/recent auction dates and saves to CSV.
    Texas tax foreclosure sales happen on the first Tuesday of each month.
    """
    county_dir = output_dir / "travis"
    county_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "  Playwright is required for Travis County. "
            "Install with: pip install playwright && python -m playwright install chromium"
        )
        return 0

    auction_dates = _get_first_tuesdays(months_ahead=3)
    # Also include last month
    today = date.today()
    prev_year = today.year + (today.month - 2) // 12
    prev_month = (today.month - 2) % 12 + 1
    first_day = date(prev_year, prev_month, 1)
    days_ahead = (1 - first_day.weekday()) % 7
    prev_tuesday = first_day + timedelta(days=days_ahead)
    auction_dates.insert(0, prev_tuesday)

    all_items = []
    base_url = "https://travis.texas.realforeclose.com/index.cfm"

    JS_EXTRACT = """() => {
        const tables = document.querySelectorAll("table.ad_tab");
        let items = [];
        tables.forEach(table => {
            let item = {};
            table.querySelectorAll("tr").forEach(row => {
                const th = row.querySelector("th.AD_LBL");
                const td = row.querySelector("td.AD_DTA");
                if (th && td) {
                    item[th.textContent.trim().replace(":", "")] = td.textContent.trim();
                }
            });
            if (Object.keys(item).length > 0) items.push(item);
        });
        return items;
    }"""

    logger.info("  Launching headless browser for RealAuction scraping...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
        )

        for auction_date in auction_dates:
            date_str = auction_date.strftime("%m/%d/%Y")
            url = f"{base_url}?zaction=AUCTION&zmethod=PREVIEW&AuctionDate={date_str}"
            logger.info(f"  Checking auction date: {date_str}")
            try:
                resp = page.goto(url, timeout=30000, wait_until="networkidle")
                if resp and resp.status != 200:
                    logger.warning(f"    Got status {resp.status}, skipping.")
                    continue
                items = page.evaluate(JS_EXTRACT)
                if items:
                    for item in items:
                        item["auction_date"] = date_str
                        item["county"] = "Travis"
                    all_items.extend(items)
                    logger.info(f"    Found {len(items)} listing(s)")
                else:
                    logger.info("    No listings found for this date.")
            except Exception as e:
                logger.error(f"    Error scraping {date_str}: {e}")
            time.sleep(1)

        browser.close()

    if not all_items:
        logger.warning("  No Travis County listings found across checked dates.")
        return 0

    # Fix address formatting: add space between house number and street name
    # RealAuction returns "7908GOLDENROD CV" instead of "7908 GOLDENROD CV"
    for item in all_items:
        addr = item.get("Property Address", "")
        if addr:
            # Insert space between digits and letters (e.g. "7908GOLDENROD" → "7908 GOLDENROD")
            item["Property Address"] = re.sub(r'(\d)([A-Za-z])', r'\1 \2', addr)

    # Write to CSV
    csv_path = county_dir / "travis_foreclosures.csv"
    fieldnames = [
        "auction_date", "county", "Sale Type", "Cause Number",
        "Precinct/Sale Number", "Adjudged Value", "Est. Min. Bid",
        "Account Number", "Property Address",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_items)

    logger.info(f"  Saved {len(all_items)} listing(s) to {csv_path.name}")
    return len(all_items)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

COUNTIES = {
    "bastrop": {
        "name": "Bastrop County",
        "url": "https://www.bastropcounty.gov/page/co.county_clerk%20_foreclosure",
        "downloader": download_bastrop,
    },
    "burnet": {
        "name": "Burnet County",
        "url": "https://www.burnetcountytexas.org/page/cclerk.foreclose",
        "downloader": download_burnet,
    },
    "hays": {
        "name": "Hays County",
        "url": "https://www.hayscountytx.gov/county-clerk/foreclosures",
        "downloader": download_hays,
    },
    "bell": {
        "name": "Bell County",
        "url": "https://www.bellcountytx.com/county_government/county_clerk/foreclosures.php",
        "downloader": download_bell,
    },
    "travis": {
        "name": "Travis County",
        "url": "https://tax-office.traviscountytx.gov/properties/foreclosed",
        "downloader": download_travis,
    },
    "williamson": {
        "name": "Williamson County",
        "url": "https://www.wilcotx.gov/308/Foreclosure-Trustee-Sales",
        "downloader": download_williamson,
    },
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download foreclosure notice PDFs from Texas county websites."
    )
    parser.add_argument(
        "--county",
        nargs="*",
        choices=list(COUNTIES.keys()),
        help="Counties to download (default: all). E.g. --county hays bell",
    )
    parser.add_argument(
        "--output",
        default="./input_pdfs",
        help="Output directory for downloaded PDFs (default: ./input_pdfs)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available counties and exit.",
    )
    args = parser.parse_args()

    if args.list:
        print("Available counties:")
        for key, info in COUNTIES.items():
            print(f"  {key:12s}  {info['name']:20s}  {info['url']}")
        return

    from state import load_state, save_state

    targets = args.county if args.county else list(COUNTIES.keys())
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    state = load_state()

    logger.info("=" * 60)
    logger.info("County Foreclosure PDF Downloader — Starting")
    logger.info(f"  Output dir : {output_dir.resolve()}")
    logger.info(f"  Counties   : {', '.join(targets)}")
    logger.info("=" * 60)

    total = 0
    for county_key in targets:
        info = COUNTIES[county_key]
        logger.info(f"\n--- {info['name']} ---")
        logger.info(f"    Source: {info['url']}")
        count = info["downloader"](output_dir, state)
        save_state(state)  # persist after each county
        logger.info(f"    → {count} file(s) downloaded for {info['name']}")
        total += count
        time.sleep(1)  # polite delay between counties

    logger.info("=" * 60)
    logger.info(f"Done. {total} total file(s) downloaded to {output_dir.resolve()}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
