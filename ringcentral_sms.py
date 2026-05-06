"""
ringcentral_sms.py
------------------
Sends SMS messages via RingCentral to every phone number in the traced leads CSV.

Reads:  leads_traced.csv  (output of skipgenie.py)
Writes: leads_sms_sent.csv  (same rows + sms_status, sms_error columns)
Also:   sms_history.csv    (append-only; phone numbers already texted)

Texts both the subject's numbers AND relatives' numbers.

Dedup: phone numbers are normalized to last-10-digits and compared
against sms_history.csv. Numbers already in history are skipped and
the row is marked `already_texted` if every number on it is a dupe.
--dry-run still reads history but never writes to it.

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RingCentral OAuth (JWT flow — simplest for server-side scripts)
# ---------------------------------------------------------------------------

RC_BASE = "https://platform.ringcentral.com"

# ---------------------------------------------------------------------------
# SMS history (dedup across runs)
# ---------------------------------------------------------------------------

SMS_HISTORY_CSV = "sms_history.csv"
HISTORY_FIELDS = ["phone_number", "sent_at", "owner_name", "property_address"]


# Re-exported for backward compatibility — the canonical implementation
# lives in utils.normalize_phone (also used by sync_call_status.py and
# sheets_exporter._clean_phone). Don't change behavior here; change utils.
from utils import normalize_phone as _normalize_phone


def load_sms_history(path: str = SMS_HISTORY_CSV) -> set:
    """Return a set of normalized phone numbers already texted."""
    p = Path(path)
    if not p.exists():
        return set()
    sent = set()
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            num = _normalize_phone(row.get("phone_number", ""))
            if num:
                sent.add(num)
    return sent


def append_sms_history(phone: str, owner: str, address: str, path: str = SMS_HISTORY_CSV) -> None:
    """Append a single sent record to the persistent history file."""
    p = Path(path)
    is_new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "phone_number": _normalize_phone(phone),
            "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "owner_name": owner,
            "property_address": address,
        })


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
    "Hey, I'm guessing you already have it handled, but I wanted to reach out about the property on {street}. Has anyone gone over some options for stopping the auction? \u2013 Zaman",
    "Hey, quick question\u2014who would be the right person to talk to about the property on {street}? I see the lender has set a sale date and wanted to understand the plan. \u2013 Zaman",
    "Hey, I noticed the lender is giving you a hard time with the property on {street}. Just wanted to reach out and see if you needed another option. \u2013 Zaman",
    "Hi, I may have some ideas on how to stop the auction for the property on {street}. Would it be worth a quick conversation? \u2013 Zaman",
]

NO_PROPERTY_TEMPLATE = (
    "Hey {owner_first}, I'm guessing you already have it handled but I wanted "
    "to reach out and see if anyone has taken the time to go over some ways to "
    "get the auction on your property stopped. \u2013 Zaman"
)

# Templates used when texting a relative (not the owner). These explain
# who's being looked for (owner's last name / address) and that a
# foreclosure is involved, so the message doesn't read like spam.
RELATIVE_TEMPLATES = [
    "Hey, sorry for the random message. I'm trying to reach someone in the {owner_last} family regarding the property on {street}. It's scheduled for foreclosure, and I'd like to speak with whoever is handling the property to see what the plan is? \u2013 Zaman",
    "Hey, I'm trying to get in touch with whoever is responsible for the property on {street}? With the auction coming up, I wanted to see if yall needed another option? \u2013 Zaman",
    "Hey, I'm trying to connect with someone in the {owner_last} family about the property on {street} before the foreclosure date. Who should I speak with? \u2013 Zaman",
    "Hey quick question, who would be the best person to talk to about the property on {street}? A foreclosure has been posted, and I'm trying to understand the plan for the property. \u2013 Zaman",
]

RELATIVE_NO_PROPERTY_TEMPLATE = (
    "Hi, I'm trying to reach {owner_first} {owner_last} about a property matter. "
    "The lender has set a sale date and I wanted to see if I could help. "
    "Would you be able to pass my number along? \u2013 Zaman"
)

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
    # Trim owner name to first + last. Owners are often "First Middle Last" or
    # "First Last And Spouse First Last" -- grab the first token as first name
    # and, for owner_last, the last token before "And" (if present).
    raw_owner = ctx.get("owner_name", "")
    name_parts = raw_owner.split()
    ctx["owner_first"] = name_parts[0].title() if name_parts else ""
    # Find last name: last token before "And" (case-insensitive), or last token
    owner_last = ""
    if name_parts:
        # Clip at "And" if owner is a couple ("John Doe And Jane Doe" -> "Doe")
        upto = name_parts
        for i, tok in enumerate(name_parts):
            if tok.lower() == "and":
                upto = name_parts[:i]
                break
        if upto:
            owner_last = upto[-1].title()
    ctx["owner_last"] = owner_last
    try:
        return template.format(**ctx)
    except KeyError as e:
        logger.warning(f"Template key {e} not in record — leaving blank.")
        return template


def pick_template() -> str:
    """Return a randomly chosen OWNER template."""
    return random.choice(TEMPLATES)


def pick_relative_template() -> str:
    """Return a randomly chosen RELATIVE template."""
    return random.choice(RELATIVE_TEMPLATES)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _process_lead(
    rec: dict,
    template: str,
    sender_name: str,
    dry_run: bool,
    access_token: str,
    from_number: str,
    delay_seconds: float,
    history: set,
    history_lock: threading.Lock,
    counters: dict,
) -> dict:
    """Send texts for one family (owner + relatives) and return the updated rec.

    Called concurrently from a thread pool — one family per worker. The
    intra-family 5-min delay before each relative text stays (carriers flag
    rapid-fire SMS to related numbers as spam), but different families run
    in parallel so the overall wall time is dominated by the family with
    the most relatives rather than the sum across all families.
    """
    owner   = rec.get("owner_name", "Unknown")
    address = rec.get("property_address", "")
    notes = rec.get("notes", "")
    if "name unreadable" in notes.lower() or not owner.strip():
        rec["sms_status"] = "skipped"
        rec["sms_error"] = "name unreadable — needs manual review"
        logger.warning(f"  Skipping {owner or '(no name)'} — name unreadable, can't personalize text")
        return rec

    has_address = bool(address.strip())
    if template is not None:
        owner_template = template
    elif not has_address:
        owner_template = NO_PROPERTY_TEMPLATE
    else:
        owner_template = pick_template()
    rec["sms_template"] = owner_template[:60] + "…"

    owner_number = None
    for i in range(1, 4):
        num = str(rec.get(f"phone_{i}", "")).strip()
        if num and num.lower() not in ("nan", ""):
            owner_number = num
            break

    seen_numbers = set()
    if owner_number:
        seen_numbers.add(owner_number)

    relative_numbers = []
    for ri in range(1, 7):
        for pi in range(1, 4):
            num = str(rec.get(f"rel_{ri}_phone_{pi}", "")).strip()
            if num and num.lower() not in ("nan", "") and num not in seen_numbers:
                relative_numbers.append(num)
                seen_numbers.add(num)
                break

    all_numbers = ([owner_number] if owner_number else []) + relative_numbers

    statuses = []

    if not all_numbers:
        logger.warning(f"  No phone numbers for {owner} — skipping.")
        rec["sms_status"] = "no_numbers"
        rec["sms_error"]  = ""
        return rec

    # Dedup against history under lock so parallel workers don't race
    # each other when the same number appears in two families' rows.
    with history_lock:
        new_numbers = []
        dupe_numbers = []
        for n in all_numbers:
            key = _normalize_phone(n)
            if key in history:
                dupe_numbers.append(n)
            else:
                new_numbers.append(n)
                history.add(key)  # reserve so sibling worker can't also claim it
    for dupe in dupe_numbers:
        statuses.append(f"{dupe}:already_texted")

    if not new_numbers:
        logger.info(f"  All numbers for {owner} already texted — skipping.")
        rec["sms_status"] = "already_texted"
        rec["sms_error"]  = ""
        return rec

    sent_any = False
    for number in new_numbers:
        is_relative = number in relative_numbers

        if is_relative:
            if template is not None:
                number_template = template
            elif not has_address:
                number_template = RELATIVE_NO_PROPERTY_TEMPLATE
            else:
                number_template = pick_relative_template()
        else:
            number_template = owner_template
        message = render_message(number_template, rec, sender_name)

        # 5-min intra-family spacing: only apply if we actually sent a prior
        # text in this family (sent_any). A failed send shouldn't force the
        # next relative to wait 5 minutes.
        if is_relative and sent_any:
            wait = 300
            if not dry_run:
                logger.info(f"  Waiting {wait // 60}m before texting next relative for {owner}…")
                time.sleep(wait)

        label = f"{owner} @ {number}"
        if dry_run:
            is_rel = "(relative)" if is_relative else "(owner)"
            logger.info(f"  [DRY RUN] Would text {label} {is_rel}:\n    {message[:80]}…")
            statuses.append(f"{number}:dry_run")
            sent_any = True
            continue

        try:
            send_sms(access_token, from_number, number, message)
            is_rel = "(relative)" if is_relative else "(owner)"
            logger.info(f"  Sent → {label} {is_rel}")
            statuses.append(f"{number}:sent")
            with counters["lock"]:
                counters["sent"] += 1
            with history_lock:
                append_sms_history(number, owner, address)
            sent_any = True
            time.sleep(delay_seconds)
        except requests.HTTPError as e:
            err = e.response.text if e.response else str(e)
            logger.error(f"  Failed → {label}: {err}")
            statuses.append(f"{number}:failed")
            with counters["lock"]:
                counters["failed"] += 1
            # Roll back the history reservation since the send failed — lets
            # a later run retry this number instead of skipping it as dupe.
            with history_lock:
                history.discard(_normalize_phone(number))
        except Exception as e:
            logger.error(f"  Failed → {label}: {e}")
            statuses.append(f"{number}:failed")
            with counters["lock"]:
                counters["failed"] += 1
            with history_lock:
                history.discard(_normalize_phone(number))

    rec["sms_status"] = " | ".join(statuses)
    rec["sms_error"]  = ""
    return rec


def run(
    input_csv: str,
    output_csv: str,
    template: str = None,
    sender_name: str = "",
    dry_run: bool = False,
    delay_seconds: float = 1.5,
    workers: int = 8,
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

    history = load_sms_history()
    logger.info(f"Loaded {len(history)} previously-texted number(s) from {SMS_HISTORY_CSV}.")

    access_token = None if dry_run else get_access_token()

    # Parallel dispatch: one family per worker thread. Workers share the
    # dedup history set + sms_history.csv writes under history_lock, and
    # the sent/failed counters under counters["lock"].
    history_lock = threading.Lock()
    counters = {"sent": 0, "failed": 0, "lock": threading.Lock()}

    worker_count = max(1, min(workers, len(records) or 1))
    logger.info(f"Sending with {worker_count} parallel worker(s) across {len(records)} famil(ies).")

    # Preserve input order in the output CSV even though threads finish
    # out of order — each rec keeps its index, we sort by it at the end.
    indexed_results = [None] * len(records)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_to_idx = {
            pool.submit(
                _process_lead, rec, template, sender_name, dry_run,
                access_token, from_number, delay_seconds,
                history, history_lock, counters,
            ): idx
            for idx, rec in enumerate(records)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                indexed_results[idx] = future.result()
            except Exception as e:
                logger.error(f"Worker crashed on record {idx}: {e}")
                rec = records[idx]
                rec["sms_status"] = "failed"
                rec["sms_error"] = str(e)
                indexed_results[idx] = rec

    results = [r for r in indexed_results if r is not None]
    sent_total = counters["sent"]
    failed_total = counters["failed"]

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
                        help="Seconds between sends within a worker (default 1.5).")
    parser.add_argument("--workers",     type=int, default=8,
                        help="Concurrent families to process at once (default 8). "
                             "The 5-min intra-family delay between relatives still "
                             "applies, but families run in parallel so total wall "
                             "time is bounded by the family with the most relatives.")
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
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
