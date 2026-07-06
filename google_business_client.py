"""
Google Business Profile API client — manage reviews and local posts.

Handles OAuth2 authentication, review management (list/reply), and local post
creation (updates, offers, events) for a single Google Business location.

Usage:
    python google_business_client.py --account-info
    python google_business_client.py --list-reviews
    python google_business_client.py --reply-review --review-id <id> --message "Thank you!"
    python google_business_client.py --delete-reply --review-id <id>
    python google_business_client.py --list-posts
    python google_business_client.py --create-post --type update --summary "We're open Saturdays!"
    python google_business_client.py --create-post --type update --summary "..." --image-url "https://..." --cta CALL --dry-run
    python google_business_client.py --create-post --type offer --summary "Spring deal" --coupon "SPRING25"
    python google_business_client.py --create-post --type event --summary "Open house" --title "Spring Open House" --start "2026-04-15T10:00:00" --end "2026-04-15T12:00:00"
    python google_business_client.py --delete-post --post-name <name>

Posts may carry one photo (--image-url, a publicly fetchable URL) and a
call-to-action button (--cta CALL|LEARN_MORE|SIGN_UP|BOOK|ORDER|SHOP,
plus --cta-url for non-CALL types). Add --dry-run to preview the exact
request body before publishing anything.

Requires credentials.json (Google Cloud OAuth2 client) in the project root.
On first run, opens a browser for consent and saves gbp_token.json.
"""

# Lazy annotations so PEP 604 unions (`str | None`) stay unevaluated — this
# module is imported by the Streamlit dashboard under Python 3.9, where those
# would raise TypeError at import time.
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent
# On hosted deploys DATA_DIR=/data (a persistent disk), and gbp_token.json /
# credentials.json are seeded there so they survive redeploys. Locally DATA_DIR
# is unset, so these resolve next to the code — same as before.
_DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_DIR)).resolve()
TOKEN_PATH = _DATA_DIR / "gbp_token.json"
CREDS_PATH = _DATA_DIR / "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/business.manage"]

# API base URLs
ACCOUNT_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
LOCATION_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
GBP_API = "https://mybusiness.googleapis.com/v4"


# ── OAuth2 ────────────────────────────────────────────────────────

def _get_credentials() -> Credentials:
    """Load, refresh, or create OAuth2 credentials for GBP."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_PATH.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDS_PATH}. "
                    "Download it from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return creds


def _auth_headers() -> dict:
    """Return authorization headers for API requests."""
    creds = _get_credentials()
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }


# ── Account & Location ────────────────────────────────────────────

def get_account_id() -> str | None:
    """Get the first GBP account ID, or use GBP_ACCOUNT_ID env var."""
    override = os.getenv("GBP_ACCOUNT_ID")
    if override:
        return override

    try:
        resp = requests.get(
            f"{ACCOUNT_API}/accounts",
            headers=_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        accounts = resp.json().get("accounts", [])
        if accounts:
            return accounts[0]["name"]  # e.g., "accounts/123456789"
        logger.error("No GBP accounts found")
        return None
    except Exception as e:
        logger.error("Failed to fetch GBP accounts: %s", e)
        return None


def get_location_id(account_id: str) -> str | None:
    """Get the first location for an account, or use GBP_LOCATION_ID env var."""
    override = os.getenv("GBP_LOCATION_ID")
    if override:
        return override

    try:
        resp = requests.get(
            f"{LOCATION_API}/{account_id}/locations",
            headers=_auth_headers(),
            params={"readMask": "name,title"},
            timeout=30,
        )
        resp.raise_for_status()
        locations = resp.json().get("locations", [])
        if locations:
            return locations[0]["name"]  # e.g., "locations/987654321"
        logger.error("No locations found for %s", account_id)
        return None
    except Exception as e:
        logger.error("Failed to fetch locations: %s", e)
        return None


def _resolve_ids() -> tuple[str, str] | None:
    """Resolve account and location IDs. Returns (account_id, location_id) or None."""
    account_id = get_account_id()
    if not account_id:
        return None
    location_id = get_location_id(account_id)
    if not location_id:
        return None
    return account_id, location_id


# ── Reviews ───────────────────────────────────────────────────────

def list_reviews(account_id: str, location_id: str,
                 page_size: int = 50, page_token: str = None) -> dict | None:
    """List reviews for a location."""
    try:
        params = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(
            f"{GBP_API}/{account_id}/{location_id}/reviews",
            headers=_auth_headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to list reviews: %s", e)
        return None


def reply_to_review(account_id: str, location_id: str,
                    review_id: str, comment: str) -> dict | None:
    """Reply to a specific review."""
    try:
        resp = requests.put(
            f"{GBP_API}/{account_id}/{location_id}/reviews/{review_id}/reply",
            headers=_auth_headers(),
            json={"comment": comment},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to reply to review %s: %s", review_id, e)
        return None


def delete_review_reply(account_id: str, location_id: str,
                        review_id: str) -> bool:
    """Delete a reply from a review."""
    try:
        resp = requests.delete(
            f"{GBP_API}/{account_id}/{location_id}/reviews/{review_id}/reply",
            headers=_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Failed to delete reply for review %s: %s", review_id, e)
        return False


# ── Local Posts ───────────────────────────────────────────────────

def list_local_posts(account_id: str, location_id: str,
                     page_size: int = 10) -> dict | None:
    """List local posts for a location."""
    try:
        resp = requests.get(
            f"{GBP_API}/{account_id}/{location_id}/localPosts",
            headers=_auth_headers(),
            params={"pageSize": page_size},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to list local posts: %s", e)
        return None


def create_local_post(account_id: str, location_id: str,
                      post_body: dict) -> dict | None:
    """Create a new local post."""
    try:
        resp = requests.post(
            f"{GBP_API}/{account_id}/{location_id}/localPosts",
            headers=_auth_headers(),
            json=post_body,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to create local post: %s", e)
        return None


def delete_local_post(account_id: str, location_id: str,
                      post_name: str) -> bool:
    """Delete a local post by its resource name."""
    try:
        resp = requests.delete(
            f"{GBP_API}/{account_id}/{location_id}/localPosts/{post_name}",
            headers=_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Failed to delete post %s: %s", post_name, e)
        return False


# ── Post Helpers ──────────────────────────────────────────────────

def build_update_post(summary: str) -> dict:
    """Build a STANDARD update post body."""
    return {
        "topicType": "STANDARD",
        "summary": summary,
        "languageCode": "en",
    }


def build_offer_post(summary: str, coupon_code: str = None,
                     redeem_url: str = None, terms: str = None) -> dict:
    """Build an OFFER post body."""
    post = {
        "topicType": "OFFER",
        "summary": summary,
        "languageCode": "en",
        "offer": {},
    }
    if coupon_code:
        post["offer"]["couponCode"] = coupon_code
    if redeem_url:
        post["offer"]["redeemOnlineUrl"] = redeem_url
    if terms:
        post["offer"]["termsConditions"] = terms
    return post


def build_event_post(summary: str, title: str,
                     start_datetime: str, end_datetime: str) -> dict:
    """Build an EVENT post body. Datetimes should be ISO format (YYYY-MM-DDTHH:MM:SS)."""
    def _parse_dt(dt_str: str) -> dict:
        from datetime import datetime
        dt = datetime.fromisoformat(dt_str)
        return {
            "date": {
                "year": dt.year,
                "month": dt.month,
                "day": dt.day,
            },
            "time": {
                "hours": dt.hour,
                "minutes": dt.minute,
            },
        }

    return {
        "topicType": "EVENT",
        "summary": summary,
        "languageCode": "en",
        "event": {
            "title": title,
            "schedule": {
                "startDate": _parse_dt(start_datetime),
                "endDate": _parse_dt(end_datetime),
            },
        },
    }


CTA_TYPES = ["CALL", "LEARN_MORE", "SIGN_UP", "BOOK", "ORDER", "SHOP"]


def attach_media(post: dict, image_url: str) -> dict:
    """Attach a PHOTO to a post via a publicly accessible image URL.

    Google fetches the image server-side, so image_url must resolve without
    auth (e.g. an 'anyone with the link' Drive URL or a hosted file).
    """
    post.setdefault("media", []).append({
        "mediaFormat": "PHOTO",
        "sourceUrl": image_url,
    })
    return post


def attach_cta(post: dict, action_type: str, url: str = None) -> dict:
    """Attach a call-to-action button.

    CALL uses the location's own phone number (no url needed). All other
    action types require a url.
    """
    cta = {"actionType": action_type}
    if action_type != "CALL":
        if not url:
            raise ValueError(f"CTA type {action_type} requires --cta-url")
        cta["url"] = url
    post["callToAction"] = cta
    return post


# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Google Business Profile — manage reviews and posts"
    )
    parser.add_argument("--account-info", action="store_true",
                        help="Print account and location info")
    parser.add_argument("--list-reviews", action="store_true",
                        help="List recent reviews")
    parser.add_argument("--reply-review", action="store_true",
                        help="Reply to a review")
    parser.add_argument("--delete-reply", action="store_true",
                        help="Delete a review reply")
    parser.add_argument("--review-id", help="Review ID for reply/delete operations")
    parser.add_argument("--message", help="Reply message text")
    parser.add_argument("--list-posts", action="store_true",
                        help="List existing local posts")
    parser.add_argument("--create-post", action="store_true",
                        help="Create a new local post")
    parser.add_argument("--delete-post", action="store_true",
                        help="Delete a local post")
    parser.add_argument("--post-name", help="Post resource name for delete")
    parser.add_argument("--type", choices=["update", "offer", "event"],
                        default="update", help="Post type (default: update)")
    parser.add_argument("--summary", help="Post summary text")
    parser.add_argument("--title", help="Event title (for event posts)")
    parser.add_argument("--start", help="Event start datetime ISO (for event posts)")
    parser.add_argument("--end", help="Event end datetime ISO (for event posts)")
    parser.add_argument("--coupon", help="Coupon code (for offer posts)")
    parser.add_argument("--redeem-url", help="Redeem URL (for offer posts)")
    parser.add_argument("--terms", help="Terms and conditions (for offer posts)")
    parser.add_argument("--image-url",
                        help="Public image URL to attach as a photo to the post")
    parser.add_argument("--cta", choices=CTA_TYPES,
                        help="Call-to-action button type (e.g. CALL, LEARN_MORE)")
    parser.add_argument("--cta-url",
                        help="URL for the CTA button (not needed for CALL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the exact post body without publishing")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Account Info ──
    if args.account_info:
        ids = _resolve_ids()
        if ids:
            account_id, location_id = ids
            print(f"\nAccount:  {account_id}")
            print(f"Location: {location_id}")
        else:
            print("Failed to resolve account/location IDs")
        return

    # All other commands need resolved IDs
    ids = _resolve_ids()
    if not ids:
        print("ERROR: Could not resolve account/location IDs. Run --account-info first.")
        return
    account_id, location_id = ids

    # ── Reviews ──
    if args.list_reviews:
        data = list_reviews(account_id, location_id)
        if not data or "reviews" not in data:
            print("No reviews found.")
            return
        reviews = data["reviews"]
        print(f"\n{len(reviews)} review(s):\n")
        for r in reviews:
            reviewer = r.get("reviewer", {}).get("displayName", "Anonymous")
            rating = r.get("starRating", "?")
            comment = r.get("comment", "(no comment)")
            review_id = r.get("reviewId", "")
            reply = r.get("reviewReply", {}).get("comment", "")
            print(f"  [{rating}] {reviewer} (ID: {review_id})")
            print(f"       {comment[:120]}")
            if reply:
                print(f"       Reply: {reply[:120]}")
            print()

    elif args.reply_review:
        if not args.review_id or not args.message:
            parser.error("--reply-review requires --review-id and --message")
        result = reply_to_review(account_id, location_id, args.review_id, args.message)
        if result:
            print(f"Reply posted to review {args.review_id}")
        else:
            print("Failed to post reply")

    elif args.delete_reply:
        if not args.review_id:
            parser.error("--delete-reply requires --review-id")
        if delete_review_reply(account_id, location_id, args.review_id):
            print(f"Reply deleted from review {args.review_id}")
        else:
            print("Failed to delete reply")

    # ── Posts ──
    elif args.list_posts:
        data = list_local_posts(account_id, location_id)
        if not data or "localPosts" not in data:
            print("No local posts found.")
            return
        posts = data["localPosts"]
        print(f"\n{len(posts)} post(s):\n")
        for p in posts:
            topic = p.get("topicType", "?")
            summary = p.get("summary", "(no summary)")
            name = p.get("name", "").split("/")[-1]
            state = p.get("state", "?")
            print(f"  [{topic}] {summary[:100]}")
            print(f"       Name: {name} | State: {state}")
            print()

    elif args.create_post:
        if not args.summary:
            parser.error("--create-post requires --summary")

        if args.type == "offer":
            post_body = build_offer_post(
                args.summary,
                coupon_code=args.coupon,
                redeem_url=args.redeem_url,
                terms=args.terms,
            )
        elif args.type == "event":
            if not args.title or not args.start or not args.end:
                parser.error("Event posts require --title, --start, and --end")
            post_body = build_event_post(
                args.summary, args.title, args.start, args.end,
            )
        else:
            post_body = build_update_post(args.summary)

        if args.image_url:
            attach_media(post_body, args.image_url)
        if args.cta:
            attach_cta(post_body, args.cta, args.cta_url)

        if args.dry_run:
            print("\n── DRY RUN — nothing was posted ──")
            print(f"POST {GBP_API}/{account_id}/{location_id}/localPosts\n")
            print(json.dumps(post_body, indent=2, ensure_ascii=False))
            return

        result = create_local_post(account_id, location_id, post_body)
        if result:
            print(f"Post created: {result.get('name', 'OK')}")
            print(f"  State: {result.get('state', '?')} | "
                  f"URL: {result.get('searchUrl', '(pending)')}")
        else:
            print("Failed to create post")

    elif args.delete_post:
        if not args.post_name:
            parser.error("--delete-post requires --post-name")
        if delete_local_post(account_id, location_id, args.post_name):
            print(f"Post {args.post_name} deleted")
        else:
            print("Failed to delete post")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
