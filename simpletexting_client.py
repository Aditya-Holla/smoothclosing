"""
Simple Texting API client — auto-respond to inbound texts about properties.

Flow:
1. Team sends a text blast about a property via Simple Texting
2. Buyers respond with questions
3. This script reads inbound replies, checks the original outbound message
   to find which property it's about, looks it up on CAD, and auto-responds
4. Showing/appointment questions get flagged for the team to answer manually

Usage:
    python simpletexting_client.py --list-inbound [--since 24h]
    python simpletexting_client.py --reply --to "+15125551234" --message "Hey..."
    python simpletexting_client.py --auto-respond [--property "503 Pintail Ln"] [--limit 5] [--dry-run]

Requires SIMPLETEXTING_API_KEY in .env
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.simpletexting.com/v2"


def _headers():
    key = os.getenv("SIMPLETEXTING_API_KEY", "")
    if not key:
        raise RuntimeError("SIMPLETEXTING_API_KEY not set in .env")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


# ── Send ───────────────────────────────────────────────────────────

def send_message(to: str, text: str) -> dict:
    """Send an SMS via Simple Texting."""
    to = re.sub(r'[^\d+]', '', to)
    if not to.startswith('+'):
        to = '+1' + to

    resp = requests.post(
        f"{BASE_URL}/send",
        json={"to": to, "text": text},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    logger.info("Sent to %s: %s", to, text[:80])
    return result


# ── Read Inbound ───────────────────────────────────────────────────

def list_inbound(since_hours: int = 24) -> list[dict]:
    """List recent inbound messages from Simple Texting."""
    resp = requests.get(
        f"{BASE_URL}/messages",
        params={"direction": "inbound", "limit": 50},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    messages = data if isinstance(data, list) else data.get("messages", data.get("data", []))

    cutoff = datetime.now() - timedelta(hours=since_hours)
    filtered = []
    for msg in messages:
        ts = msg.get("createdAt", msg.get("timestamp", msg.get("date", "")))
        if ts:
            try:
                msg_time = datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+00:00", ""))
                if msg_time < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        filtered.append(msg)

    return filtered


def get_conversation(phone: str) -> list[dict]:
    """Get conversation history with a phone number to find the original blast."""
    try:
        resp = requests.get(
            f"{BASE_URL}/messages",
            params={"phone": phone, "limit": 10},
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("messages", data.get("data", []))
    except Exception as e:
        logger.warning("Could not fetch conversation for %s: %s", phone, e)
        return []


# ── Property Detection ─────────────────────────────────────────────

def extract_property_from_conversation(messages: list[dict]) -> str:
    """Find the property address from the original outbound text blast."""
    for msg in messages:
        direction = msg.get("direction", msg.get("type", ""))
        text = msg.get("text", msg.get("message", msg.get("body", "")))

        # Look for outbound messages (the original blast)
        if direction in ("outbound", "sent", "out") and text:
            # Try to extract a street address from the blast
            # Common patterns: "property at 503 Pintail Ln" or just an address
            addr_match = re.search(
                r'(?:at|on|about|for|property)\s+(\d+\s+[A-Za-z\s]+(?:St|Dr|Ln|Ave|Blvd|Way|Ct|Cir|Loop|Rd|Pl|Trl|Pkwy))',
                text, re.IGNORECASE,
            )
            if addr_match:
                return addr_match.group(1).strip()

            # Fallback: find any address-like pattern (number + street)
            addr_match = re.search(
                r'(\d+\s+[A-Z][a-zA-Z\s]+(?:St|Dr|Ln|Ave|Blvd|Way|Ct|Cir|Loop|Rd|Pl|Trl|Pkwy))',
                text, re.IGNORECASE,
            )
            if addr_match:
                return addr_match.group(1).strip()

    return ""


# ── CAD Lookup ─────────────────────────────────────────────────────

def get_cad_info(address: str) -> dict | None:
    """Look up property on CAD."""
    python = str(Path(__file__).parent / ".venv-agents" / "bin" / "python3")
    if not Path(python).exists():
        python = sys.executable

    try:
        result = subprocess.run(
            [python, "cad_scraper.py", "--county", "all", "--search", address,
             "--type", "address", "--output", "/tmp/cad_reply_lookup.csv"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent),
            timeout=30,
        )
        if result.returncode == 0 and Path("/tmp/cad_reply_lookup.csv").exists():
            import csv
            with open("/tmp/cad_reply_lookup.csv") as f:
                rows = list(csv.DictReader(f))
            return rows[0] if rows else None
    except Exception as e:
        logger.warning("CAD lookup failed for %s: %s", address, e)
    return None


# ── Response Logic ─────────────────────────────────────────────────

MANUAL_KEYWORDS = [
    "showing", "tour", "open house", "when can i see",
    "what time", "schedule", "appointment", "visit",
    "meet", "come by", "walk through", "view the",
]


def needs_manual_response(message_text: str) -> bool:
    """Check if the message asks something only a human can answer."""
    lower = message_text.lower()
    return any(kw in lower for kw in MANUAL_KEYWORDS)


def craft_response(inbound_text: str, property_address: str, cad_info: dict | None) -> str:
    """Craft a casual, property-specific response to a buyer's text."""
    street = property_address.split(",")[0] if property_address else "the property"
    sender = os.getenv("SENDER_NAME", "")

    value = cad_info.get("market_value", "") if cad_info else ""
    sqft = cad_info.get("sqft", "") if cad_info else ""
    year_built = cad_info.get("year_built", "") if cad_info else ""
    lot_size = cad_info.get("lot_size", "") if cad_info else ""

    lower = inbound_text.lower()

    # Opt-out — don't respond
    if any(w in lower for w in ["stop", "unsubscribe", "remove", "don't text", "opt out"]):
        return ""

    # Interested / tell me more
    if any(w in lower for w in ["interested", "tell me more", "more info", "details", "yes"]):
        msg = f"Hey! Glad you're interested in {street}. "
        details = []
        if sqft:
            details.append(f"{sqft} sqft")
        if year_built:
            details.append(f"built in {year_built}")
        if lot_size:
            details.append(f"{lot_size} lot")
        if details:
            msg += f"It's {', '.join(details)}. "
        msg += "Want me to send you more info?"
        return msg

    # Price questions
    if any(w in lower for w in ["price", "how much", "asking", "cost", "offer", "arv"]):
        msg = f"Great question! "
        if value:
            msg += f"The county has {street} valued at {value}. "
        msg += "I can send you more details if you're interested!"
        return msg

    # Property details / specs
    if any(w in lower for w in ["how big", "square feet", "sqft", "bedrooms", "beds", "lot", "year", "size"]):
        details = []
        if sqft:
            details.append(f"{sqft} sqft")
        if year_built:
            details.append(f"built {year_built}")
        if lot_size:
            details.append(f"{lot_size} lot")
        if value:
            details.append(f"valued at {value}")
        if details:
            return f"Here's what I have on {street}: {', '.join(details)}. Want me to send you more info?"
        return f"I can send you the full details on {street} — want me to?"

    # Who is this
    if any(w in lower for w in ["who is this", "who are you", "what company", "who am i"]):
        return f"Hey! This is {sender} — I'm a real estate investor. Just reaching out about {street}. No pressure at all!"

    # Location / where
    if any(w in lower for w in ["where", "location", "address", "what city"]):
        if property_address:
            return f"The property is at {property_address}. Want me to send you more details?"
        return f"I can send you the full address — interested?"

    # Default — friendly follow up
    return f"Hey thanks for getting back to me about {street}! Want me to send you more details?"


# ── Auto-Respond ───────────────────────────────────────────────────

def auto_respond(property_address: str = None, limit: int = None, dry_run: bool = True):
    """Read inbound messages and auto-respond with property-aware replies."""
    messages = list_inbound(since_hours=24)

    if not messages:
        logger.info("No inbound messages in the last 24 hours.")
        return

    if limit:
        messages = messages[:limit]

    logger.info("Found %d inbound message(s) to process", len(messages))

    # If property address provided, look it up on CAD once
    cad_info = None
    if property_address:
        logger.info("Looking up property on CAD: %s", property_address)
        cad_info = get_cad_info(property_address)
        if cad_info:
            logger.info("CAD info: %s | Value: %s | %s sqft",
                       cad_info.get("owner_name", ""),
                       cad_info.get("market_value", ""),
                       cad_info.get("sqft", ""))

    manual_queue = []
    auto_replies = []
    skipped = 0

    for msg in messages:
        phone = msg.get("from", msg.get("phoneNumber", msg.get("phone", "")))
        text = msg.get("text", msg.get("message", msg.get("body", "")))

        if not phone or not text:
            continue

        logger.info("Inbound from %s: %s", phone, text[:80])

        # If no property address given, try to find it from conversation history
        prop_addr = property_address
        prop_cad = cad_info
        if not prop_addr:
            convo = get_conversation(phone)
            prop_addr = extract_property_from_conversation(convo)
            if prop_addr:
                logger.info("  Found property from conversation: %s", prop_addr)
                prop_cad = get_cad_info(prop_addr)

        # Check if needs manual response (showing, appointment, etc.)
        if needs_manual_response(text):
            manual_queue.append({
                "phone": phone,
                "message": text,
                "property": prop_addr,
            })
            logger.info("  -> NEEDS MANUAL: showing/scheduling question")
            continue

        # Craft response
        response = craft_response(text, prop_addr or "", prop_cad)
        if not response:
            logger.info("  -> SKIP: opt-out")
            skipped += 1
            continue

        if dry_run:
            logger.info("  -> DRY RUN reply: %s", response)
            auto_replies.append({"phone": phone, "inbound": text, "reply": response})
        else:
            try:
                send_message(phone, response)
                auto_replies.append({"phone": phone, "inbound": text, "reply": response})
                logger.info("  -> SENT: %s", response[:80])
                time.sleep(2)
            except Exception as e:
                logger.error("  -> FAILED: %s", e)

    # Summary
    print("\n" + "=" * 60)
    print("  AUTO-RESPOND SUMMARY")
    print("=" * 60)
    print(f"  Processed: {len(messages)} inbound messages")
    print(f"  Auto-replied: {len(auto_replies)}")
    print(f"  Skipped (opt-out): {skipped}")

    if manual_queue:
        print(f"\n  NEEDS MANUAL RESPONSE ({len(manual_queue)}):")
        for item in manual_queue:
            print(f"    {item['phone']}: \"{item['message']}\"")
            if item['property']:
                print(f"      Property: {item['property']}")

    if auto_replies and dry_run:
        print(f"\n  PREVIEW — would send these replies:")
        for item in auto_replies:
            print(f"    To {item['phone']}:")
            print(f"      They said: \"{item['inbound'][:60]}\"")
            print(f"      Reply:     \"{item['reply'][:80]}\"")

    print()


# ── CLI ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Simple Texting — auto-respond to inbound texts about properties"
    )
    parser.add_argument("--list-inbound", action="store_true",
                        help="List recent inbound messages")
    parser.add_argument("--since", default="24h",
                        help="Time window for inbound (e.g., 24h, 48h)")
    parser.add_argument("--reply", action="store_true",
                        help="Send a single reply")
    parser.add_argument("--to", help="Phone number to reply to")
    parser.add_argument("--message", help="Message text")
    parser.add_argument("--auto-respond", action="store_true",
                        help="Auto-respond to inbound messages")
    parser.add_argument("--property", default=None,
                        help="Property address the blast was about (e.g., '503 Pintail Ln'). "
                             "If not provided, tries to detect from conversation history.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview responses without sending")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.list_inbound:
        hours = int(args.since.replace("h", ""))
        messages = list_inbound(since_hours=hours)
        print(f"\n{len(messages)} inbound message(s) in the last {hours}h:\n")
        for msg in messages:
            phone = msg.get("from", msg.get("phoneNumber", ""))
            text = msg.get("text", msg.get("message", msg.get("body", "")))
            ts = msg.get("createdAt", msg.get("timestamp", ""))
            print(f"  {ts} | {phone} | {text[:80]}")

    elif args.reply:
        if not args.to or not args.message:
            parser.error("--reply requires --to and --message")
        send_message(args.to, args.message)

    elif args.auto_respond:
        auto_respond(property_address=args.property, limit=args.limit, dry_run=args.dry_run)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
