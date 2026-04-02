"""
ringcentral_sms.py
------------------
Sends SMS messages via RingCentral to every phone number in the traced leads CSV.

Reads:  leads_traced.csv  (output of skipgenie.py)
Writes: leads_sms_sent.csv (same rows + sms_status, sms_error columns)

Texts both the subject's numbers AND relatives' numbers.

Usage:
    python ringcentral_sms.py
    python ringcentral_sms.py --input leads_traced.csv --template template.txt --dry-run
"""

import argparse
import csv
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RingCentral OAuth (JWT flow — simplest for server-side scripts)
# ---------------------------------------------------------------------------

RC_BASE = "https://platform.ringcentral.com"


def get_access_token() -> str:
    """Exchange JWT token for a short-lived access token."""
    client_id     = os.getenv("RC_CLIENT_ID", "")
    client_secret = os.getenv("RC_CLIENT_SECRET", "")
    jwt_token     = os.getenv("RC_JWT_TOKEN", "")

    if not all([client_id, client_secret, jwt_token]):
        logger.error(
            "Missing RingCentral credentials. Set RC_CLIENT_ID, "
            "RC_CLIENT_SECRET, and RC_JWT_TOKEN in your .env file."
        )
        sys.exit(1)

    resp = requests.post(
        f"{RC_BASE}/restapi/oauth/token",
        auth=(client_id, client_secret),
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt_token},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        logger.error(f"Could not get access token: {resp.text}")
        sys.exit(1)
    logger.info("RingCentral: authenticated.")
    return token


def send_sms(access_token: str, from_number: str, to_number: str, text: str) -> dict:
    """Send a single SMS. Returns the API response dict."""
    resp = requests.post(
        f"{RC_BASE}/restapi/v1.0/account/~/extension/~/sms",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"},
        json={
            "from": {"phoneNumber": from_number},
            "to":   [{"phoneNumber": to_number}],
            "text": text,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Message template rendering
# ---------------------------------------------------------------------------

TEMPLATES = [
    "Hey, I'm guessing you already have it handled but I wanted to reach out and see if anyone has taken the time to go over some ways to get the auction stopped on {street}?",
    "Hey, quick question\u2014who would be the right person to talk to about {street}? I see the lender has set a sale date and wanted to understand the plan. \u2013 Vince",
    "Hey, I noticed that the lender is giving you a hard time on {street}. I just wanted to reach out and see if you needed another option?",
    "Hi, I may have some ideas to stop the auction on {street}. Would it be worth a quick conversation? \u2013 Vince",
]

# Legacy default kept for --template file override
DEFAULT_TEMPLATE = (
    "Hi {owner_name}, my name is {sender_name} and I'm a real estate investor. "
    "I saw your property at {property_address} and I'm interested in making you "
    "a fair cash offer. No hassle, no fees. Would you be open to a quick chat? "
    "Reply STOP to opt out."
)


def _extract_street(address: str) -> str:
    """Strip the street number from an address, returning just the street name.

    '123 Main St, Austin, TX 78701' -> 'Main St'
    '4500 N Lamar Blvd'             -> 'N Lamar Blvd'
    """
    if not address:
        return "your property"
    # Take only the first line / before any comma (drop city/state/zip)
    street_line = address.split(",")[0].strip()
    # Remove leading digits (house number)
    street_name = re.sub(r"^\d+[-\d]*\s*", "", street_line).strip()
    return street_name if street_name else "your property"


def render_message(template: str, record: dict, sender_name: str) -> str:
    """Fill template placeholders from the lead record."""
    ctx = {k: (v or "") for k, v in record.items()}
    ctx["sender_name"] = sender_name
    ctx["street"] = _extract_street(ctx.get("property_address", ""))
    # Trim owner name to first + last only
    name_parts = ctx.get("owner_name", "").split()
    ctx["owner_first"] = name_parts[0].title() if name_parts else ""
    try:
        return template.format(**ctx)
    except KeyError as e:
        logger.warning(f"Template key {e} not in record — leaving blank.")
        return template


def pick_template() -> str:
    """Return a randomly chosen template from TEMPLATES."""
    return random.choice(TEMPLATES)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(
    input_csv: str,
    output_csv: str,
    template: str = None,
    sender_name: str = "",
    dry_run: bool = False,
    delay_seconds: float = 1.5,
) -> None:
    in_path = Path(input_csv)
    if not in_path.exists():
        logger.error(f"Input not found: {in_path}")
        sys.exit(1)

    from_number = os.getenv("RC_FROM_NUMBER", "")
    if not from_number:
        logger.error("Set RC_FROM_NUMBER in your .env (your RingCentral SMS-enabled number).")
        sys.exit(1)

    with open(in_path, newline="", encoding="utf-8") as f:
        records = list(csv.DictReader(f))

    logger.info(f"Loaded {len(records)} lead(s).")

    access_token = None if dry_run else get_access_token()

    results = []
    sent_total = 0
    failed_total = 0

    for rec in records:
        owner   = rec.get("owner_name", "Unknown")
        address = rec.get("property_address", "")
        # Use rotating templates unless a custom template file was provided
        active_template = pick_template() if template is None else template
        message = render_message(active_template, rec, sender_name)
        rec["sms_template"] = active_template[:60] + "…"

        # Collect all numbers from phone_1, phone_2, phone_3 (skip_genie_search output)
        all_numbers = [
            str(rec.get(f"phone_{i}", "")).strip()
            for i in range(1, 4)
            if str(rec.get(f"phone_{i}", "")).strip()
            and str(rec.get(f"phone_{i}", "")).strip().lower() not in ("nan", "")
        ]

        statuses = []

        if not all_numbers:
            logger.warning(f"  No phone numbers for {owner} — skipping.")
            rec["sms_status"] = "no_numbers"
            rec["sms_error"]  = ""
            results.append(rec)
            continue

        for number in all_numbers:
            label = f"{owner} @ {number}"
            if dry_run:
                logger.info(f"  [DRY RUN] Would text {label}:\n    {message[:80]}…")
                statuses.append(f"{number}:dry_run")
                continue

            try:
                send_sms(access_token, from_number, number, message)
                logger.info(f"  Sent → {label}")
                statuses.append(f"{number}:sent")
                sent_total += 1
                time.sleep(delay_seconds)   # stay within rate limits
            except requests.HTTPError as e:
                err = e.response.text if e.response else str(e)
                logger.error(f"  Failed → {label}: {err}")
                statuses.append(f"{number}:failed")
                failed_total += 1
            except Exception as e:
                logger.error(f"  Failed → {label}: {e}")
                statuses.append(f"{number}:failed")
                failed_total += 1

        rec["sms_status"] = " | ".join(statuses)
        rec["sms_error"]  = ""
        results.append(rec)

    # Write output
    out_path = Path(output_csv)
    if results:
        fieldnames = list(results[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)

    if not dry_run:
        logger.info(f"SMS complete: {sent_total} sent, {failed_total} failed.")
    print(f"\n✓ Output → {out_path.resolve()}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Send SMS via RingCentral to traced leads")
    parser.add_argument("--input",       default="leads_traced.csv")
    parser.add_argument("--output",      default="leads_sms_sent.csv")
    parser.add_argument("--template",    default=None,
                        help="Path to a .txt file with the message template. "
                             "Supports {owner_name}, {owner_first}, {property_address}, {sender_name}.")
    parser.add_argument("--sender-name", default=os.getenv("SENDER_NAME", ""),
                        help="Your name to include in the message.")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Print messages without actually sending.")
    parser.add_argument("--delay",       type=float, default=1.5,
                        help="Seconds between sends (default 1.5).")
    parser.add_argument("--debug",       action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    template_text = None
    if args.template:
        template_text = Path(args.template).read_text(encoding="utf-8").strip()

    run(
        input_csv=args.input,
        output_csv=args.output,
        template=template_text,
        sender_name=args.sender_name,
        dry_run=args.dry_run,
        delay_seconds=args.delay,
    )


if __name__ == "__main__":
    main()
