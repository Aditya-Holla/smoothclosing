"""
Skip Genie Automated Search Script

Reads leads from leads.csv, searches each person on Skip Genie's Tracer,
and saves all results to skip_genie_results.csv.

Usage:
  python3 skip_genie_search.py                 # search all leads
  python3 skip_genie_search.py --limit 5       # search first 5 leads only
  python3 skip_genie_search.py --start 10      # skip first 10, search the rest

First run: a browser opens, log in to Skip Genie, then press Enter.
After that, your session is saved and future runs are fully automatic.
"""

import csv
import re
import time
import sys
import os
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(SCRIPT_DIR, ".skipgenie_session")
INPUT_FILE = os.path.join(SCRIPT_DIR, "leads.csv")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "skip_genie_results.csv")


SUFFIXES = {"sr", "jr", "ii", "iii", "iv", "v", "esq", "phd", "md"}

DESCRIPTORS_RE = re.compile(
    r",?\s*\b(an?\s+)?(single|married|unmarried)\s+(woman|man|person)\b.*",
    re.IGNORECASE,
)

COMPANY_RE = re.compile(
    r'\b(llc|inc|corp|corporation|holdings|enterprises|investments|properties|realty|lp|ltd'
    r'|.?ort.?age\s+electronic|registration\s+systems|mers)\b',
    re.IGNORECASE,
)


def parse_owner_name(owner_name: str) -> list[dict]:
    """Parse owner_name field into individual people with first/last/middle names."""
    # Strip descriptors like ", A Single Woman" before splitting on AND
    owner_name = DESCRIPTORS_RE.sub("", owner_name).strip()
    # Remove trailing "And" / "Nd" / "&" with no second person
    owner_name = re.sub(r'\s+(?:And|Nd|&)\s*$', '', owner_name, flags=re.IGNORECASE).strip()

    # Split on " And ", " Nd ", " & " (case-insensitive)
    names = re.split(r'\s+(?:and|nd)\s+|\s*&\s*', owner_name, flags=re.IGNORECASE)
    names = [n.strip() for n in names if n and n.strip()]

    people = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        # Remove periods and commas, collapse whitespace
        name = re.sub(r'[.,]', '', name)
        name = re.sub(r'\s+', ' ', name).strip()

        parts = name.split()

        # Strip suffix (Sr, Jr, III, etc.) from the end
        suffix = ""
        if len(parts) > 1 and parts[-1].lower() in SUFFIXES:
            suffix = parts.pop()

        if len(parts) == 1:
            people.append({"first": parts[0], "last": "", "middle": "", "suffix": suffix})
        elif len(parts) == 2:
            people.append({"first": parts[0], "last": parts[1], "middle": "", "suffix": suffix})
        elif len(parts) == 3:
            people.append({"first": parts[0], "last": parts[2], "middle": parts[1], "suffix": suffix})
        else:
            people.append({
                "first": parts[0],
                "last": parts[-1],
                "middle": " ".join(parts[1:-1]),
                "suffix": suffix,
            })

    # Inherit last name for AND-splits where one person is missing it
    # e.g. "Nancy And Ryan S. Velte" -> Nancy gets last name "Velte"
    # e.g. "Brent Sanchez And Alissa" -> Alissa gets last name "Sanchez"
    if len(people) > 1:
        known_last = next((p["last"] for p in people if p["last"] and len(p["last"]) > 1), "")
        if known_last:
            for p in people:
                if not p["last"] or len(p["last"]) <= 1:
                    p["last"] = known_last

    return people


def parse_address(property_address: str) -> dict:
    """Parse property_address into street, city, state, zip."""
    if not property_address:
        return {"street": "", "city": "", "state": "", "zip": ""}

    addr = property_address.strip().rstrip('.')

    zip_match = re.search(r'(\d{5}(?:-\d{4})?)\s*$', addr)
    zip_code = zip_match.group(1) if zip_match else ""
    if zip_match:
        addr = addr[:zip_match.start()].strip().rstrip(',')

    state_match = re.search(r',?\s*([A-Z]{2})\s*$', addr)
    state = state_match.group(1) if state_match else ""
    if state_match:
        addr = addr[:state_match.start()].strip().rstrip(',')

    parts = [p.strip() for p in addr.split(',') if p.strip()]
    if len(parts) >= 2:
        street = parts[0]
        city = parts[1]
    elif len(parts) == 1:
        street = parts[0]
        city = ""
    else:
        street = ""
        city = ""

    return {"street": street, "city": city, "state": state, "zip": zip_code}


def ensure_logged_in(page, email=None, password=None) -> bool:
    """Navigate to Skip Genie and handle login if needed. Returns True if logged in."""
    page.goto("https://web.skipgenie.com/user/search")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    if "login" not in page.url.lower():
        return True

    # Try auto-login if credentials provided
    if email and password:
        print("Logging in automatically...")
        try:
            page.locator("input[placeholder*='Email'], input[type='email']").fill(email)
            page.locator("input[placeholder*='Password'], input[type='password']").fill(password)
            # Handle reCAPTCHA checkbox
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
            print(f"Auto-login failed: {e}")

        if "login" not in page.url.lower():
            print("Login successful!")
            return True
        print("Auto-login did not succeed (may need CAPTCHA). Falling back to manual.")

    # Manual login fallback
    print("\n" + "=" * 55)
    print("  Please log into Skip Genie in the browser window.")
    print("  Your session will be saved for future runs.")
    print("=" * 55)
    try:
        input("\nPress Enter after you've logged in... ")
    except EOFError:
        # Non-interactive: poll until logged in
        print("\nWaiting for login...")
        for _ in range(120):
            time.sleep(2)
            if "login" not in page.url.lower():
                break
        else:
            print("Timed out waiting for login.")
            return False

    # Verify login succeeded
    page.goto("https://web.skipgenie.com/user/search")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    if "login" in page.url.lower():
        print("ERROR: Still not logged in. Please try again.")
        return False

    print("Login successful!")
    return True


def search_person(page, person: dict, address: dict) -> dict:
    """Fill the search form and execute search, return scraped results."""
    empty_results = {
        "phones": [], "emails": [], "addresses": [],
        "result_name": "", "age": "", "dob": "",
        "bankruptcies": "", "liens": "", "judgements": "",
    }

    # Navigate to search page
    page.goto("https://web.skipgenie.com/user/search")
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)

    # Click Name Search tab
    try:
        page.click("button:has-text('Name Search')", timeout=5000)
        time.sleep(0.5)
    except Exception:
        pass

    # Fill form fields by clicking and typing (simulates real user input for React)
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
        type_into("Middle Name", person["middle"])
        type_into("Street Address", address["street"])
        type_into("City", address["city"])
        type_into("State", address["state"])
        type_into("Zip", address["zip"])
    except Exception as e:
        print(f"  Error filling form: {e}")
        return empty_results

    time.sleep(1)

    # Click GET INFO
    try:
        page.locator("button:has-text('Get Info')").first.click(force=True, timeout=5000)
    except Exception as e:
        print(f"  Could not click GET INFO: {e}")
        return empty_results

    time.sleep(2)

    # Click YES, EXECUTE SEARCH on confirmation modal
    confirmed = False
    for attempt_selector in [
        "text=YES, EXECUTE SEARCH",
        "button:has-text('EXECUTE SEARCH')",
        "button:has-text('Execute Search')",
        "button:has-text('execute search')",
    ]:
        try:
            loc = page.locator(attempt_selector).first
            if loc.is_visible(timeout=2000):
                loc.click(timeout=3000)
                confirmed = True
                print(f"  Confirmed search")
                break
        except Exception:
            continue

    if not confirmed:
        # Last resort: find all visible buttons and check text
        try:
            buttons = page.locator("button:visible").all()
            for btn in buttons:
                txt = btn.inner_text()
                if "EXECUTE" in txt.upper():
                    btn.click()
                    confirmed = True
                    print(f"  Confirmed search (found button: {txt})")
                    break
        except Exception:
            pass

    if not confirmed:
        print("  No confirmation dialog found")
        return empty_results

    # Wait for results to load
    time.sleep(5)

    # Scrape results
    return scrape_results(page)


def scrape_results(page) -> dict:
    """Scrape all data from the results modal."""
    results = {
        "phones": [], "emails": [], "addresses": [],
        "result_name": "", "age": "", "dob": "",
        "bankruptcies": "", "liens": "", "judgements": "",
    }

    try:
        page_text = page.inner_text("body")
    except Exception:
        print("  Could not read page text")
        return results

    if "Property Details" not in page_text and "Result :" not in page_text:
        print("  No results found for this person")
        return results

    # Extract result header (name, age, DOB)
    result_match = re.search(
        r'Result\s*:\s*\d+\s*of\s*\d+\s+(.*?)at\s+(\d+)\s*-\s*DOB:\s*(.*?)(?:\n|$)',
        page_text
    )
    if result_match:
        results["result_name"] = result_match.group(1).strip()
        results["age"] = result_match.group(2)
        results["dob"] = result_match.group(3).strip()

    # Extract phone numbers
    for match in re.finditer(r'\((\d{3})\)\s*(\d{3})-(\d{4})\s*\((.*?)\)', page_text):
        phone = f"({match.group(1)}) {match.group(2)}-{match.group(3)}"
        phone_type = match.group(4)
        results["phones"].append(f"{phone} ({phone_type})")

    # Extract emails (skip the logged-in user's email)
    for match in re.finditer(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', page_text, re.IGNORECASE):
        email = match.group(0)
        if email not in results["emails"] and "webuyanything" not in email.lower():
            results["emails"].append(email)

    # Extract addresses from Address History section
    lines = page_text.split('\n')
    in_address_section = False
    for line in lines:
        line = line.strip()
        if 'Address History' in line:
            in_address_section = True
            continue
        if in_address_section and line.startswith('Possible'):
            in_address_section = False
            continue
        if in_address_section and re.match(r'\d+\s+', line):
            results["addresses"].append(line)

    # Extract indicators
    for indicator in ['Bankruptcies', 'Liens', 'Judgements']:
        ind_match = re.search(rf'{indicator}:\s*(.*?)(?:\n|$)', page_text)
        if ind_match:
            results[indicator.lower()] = ind_match.group(1).strip()

    # Close the modal
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    time.sleep(1)
    return results


def main():
    # Parse args
    limit = None
    start = 0
    email = os.environ.get("SKIPGENIE_EMAIL", "")
    password = os.environ.get("SKIPGENIE_PASSWORD", "")
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i].startswith("--limit="):
            limit = int(args[i].split("=")[1])
            i += 1
        elif args[i] == "--start" and i + 1 < len(args):
            start = int(args[i + 1])
            i += 2
        elif args[i].startswith("--start="):
            start = int(args[i].split("=")[1])
            i += 1
        elif args[i] == "--email" and i + 1 < len(args):
            email = args[i + 1]
            i += 2
        elif args[i] == "--password" and i + 1 < len(args):
            password = args[i + 1]
            i += 2
        else:
            i += 1

    # Read leads
    leads = []
    with open(INPUT_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append(row)

    total = len(leads)
    leads = leads[start:]
    if limit:
        leads = leads[:limit]

    print(f"Total leads in CSV: {total}")
    print(f"Searching: {len(leads)} leads (starting from #{start + 1})")
    print(f"Each search uses 1 skip credit.\n")

    with sync_playwright() as p:
        # Launch browser with persistent context (saves login cookies)
        context = p.chromium.launch_persistent_context(
            SESSION_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        # Ensure logged in
        if not ensure_logged_in(page, email=email, password=password):
            context.close()
            return

        # Check credits
        try:
            body_text = page.inner_text("body")
            credit_match = re.search(r'Skip Credit\s*:\s*(\d+)', body_text)
            if credit_match:
                credits = int(credit_match.group(1))
                print(f"Skip credits available: {credits}")
                if credits < len(leads):
                    print(f"WARNING: Only {credits} credits for {len(leads)} searches!")
                    try:
                        resp = input("Continue? (y/n): ").strip().lower()
                        if resp != 'y':
                            context.close()
                            return
                    except EOFError:
                        print("Proceeding anyway (non-interactive mode)...")
        except Exception:
            pass

        print(f"\nStarting searches...\n")

        # Output CSV
        fieldnames = [
            "owner_name", "property_address", "searched_first", "searched_middle", "searched_last",
            "result_name", "age", "dob",
            "phone_1", "phone_2", "phone_3",
            "email_1", "email_2", "email_3",
            "addresses", "bankruptcies", "liens", "judgements",
            "case_number", "loan_amount", "lender"
        ]

        with open(OUTPUT_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for i, lead in enumerate(leads):
                owner_name = lead.get("owner_name", "")
                property_address = lead.get("property_address", "")

                print(f"[{start + i + 1}/{total}] {owner_name}")

                # Skip company names — not searchable as people
                if COMPANY_RE.search(owner_name):
                    print("  SKIP - company/entity name\n")
                    continue

                # Use mailing_address as fallback when property_address is missing
                if not property_address or property_address.strip().lower() in ("nan", ""):
                    mailing = lead.get("mailing_address", "")
                    if mailing and mailing.strip().lower() not in ("nan", ""):
                        property_address = mailing
                        print(f"  (using mailing address as fallback)")

                address = parse_address(property_address)
                people = parse_owner_name(owner_name)

                if not people:
                    print("  SKIP - could not parse name\n")
                    continue

                person = people[0]

                # Skip if first or last name is missing/too short
                if not person["first"]:
                    print("  SKIP - no first name\n")
                    continue
                if not person["last"] or len(person["last"]) <= 1:
                    print(f"  SKIP - no usable last name (got '{person['last']}')\n")
                    continue

                sfx = f" {person['suffix']}" if person.get("suffix") else ""
                print(f"  -> First={person['first']}, Middle={person.get('middle','')}, Last={person['last']}{sfx}"
                      f" | {address['street']}, {address['city']}, {address['state']}")

                try:
                    results = search_person(page, person, address)
                except Exception as e:
                    print(f"  ERROR: {e}")
                    results = {
                        "phones": [], "emails": [], "addresses": [],
                        "result_name": "", "age": "", "dob": "",
                        "bankruptcies": "", "liens": "", "judgements": "",
                    }

                row = {
                    "owner_name": owner_name,
                    "property_address": property_address,
                    "searched_first": person["first"],
                    "searched_middle": person.get("middle", ""),
                    "searched_last": person["last"],
                    "result_name": results.get("result_name", ""),
                    "age": results.get("age", ""),
                    "dob": results.get("dob", ""),
                    "phone_1": results["phones"][0] if len(results["phones"]) > 0 else "",
                    "phone_2": results["phones"][1] if len(results["phones"]) > 1 else "",
                    "phone_3": results["phones"][2] if len(results["phones"]) > 2 else "",
                    "email_1": results["emails"][0] if len(results["emails"]) > 0 else "",
                    "email_2": results["emails"][1] if len(results["emails"]) > 1 else "",
                    "email_3": results["emails"][2] if len(results["emails"]) > 2 else "",
                    "addresses": " | ".join(results.get("addresses", [])),
                    "bankruptcies": results.get("bankruptcies", ""),
                    "liens": results.get("liens", ""),
                    "judgements": results.get("judgements", ""),
                    "case_number": lead.get("case_number", ""),
                    "loan_amount": lead.get("loan_amount", ""),
                    "lender": lead.get("lender", ""),
                }
                writer.writerow(row)
                f.flush()

                p_count = len(results['phones'])
                e_count = len(results['emails'])
                print(f"  => {p_count} phones, {e_count} emails\n")

                # Rate limit
                time.sleep(2)

        print(f"Done! Results saved to {OUTPUT_FILE}")
        context.close()


if __name__ == "__main__":
    main()
