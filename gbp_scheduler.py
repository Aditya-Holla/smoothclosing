"""
GBP campaign scheduler — drips the 100 Smooth Closing posts to Google Business
Profile twice a week (Mon & Thu, never back-to-back), one per posting day, with
categories spread so no two consecutive posts share a category.

Content comes from gmb_posts.json (parsed from "Corrected GMB Posts.docx").
Progress is tracked in gbp_campaign.json so re-runs never double-post.

Each post: type "Update" (STANDARD), the doc's headline leads the body, and a
CALL call-to-action button (uses the location's listed phone, (512) 368-9989).
Photos are intentionally omitted for now — they can be added later.

Usage:
    python gbp_scheduler.py --plan               # show the full dated schedule
    python gbp_scheduler.py --status             # what's posted / what's next
    python gbp_scheduler.py --post-next --dry-run # preview the next post only
    python gbp_scheduler.py --post-next          # publish the next post, mark it done

When imported by the dashboard, call tick() on a timer to auto-publish one post
per posting day (Mon & Thu) — see tick() at the bottom.
"""

# Lazy annotations: this module is imported by the Streamlit dashboard under
# Python 3.9, where `list[int]` / `str | None` in signatures would raise.
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import google_business_client as g

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent
# gmb_posts.json ships with the code; campaign state + pause flag live in
# DATA_DIR so they persist on Render's disk across redeploys.
DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_DIR)).resolve()
POSTS_PATH = PROJECT_DIR / "gmb_posts.json"
STATE_PATH = DATA_DIR / "gbp_campaign.json"
PAUSE_PATH = DATA_DIR / "gbp_campaign.paused"
LOCK_PATH = DATA_DIR / "gbp_campaign.lock"


@contextmanager
def _campaign_lock():
    """Cross-process exclusive lock so the daemon and the dashboard's manual
    button can never publish at the same time. Degrades to a no-op if flock
    isn't available (non-Unix)."""
    f = None
    try:
        import fcntl
        f = open(LOCK_PATH, "w")
        fcntl.flock(f, fcntl.LOCK_EX)
    except Exception:
        f = None
    try:
        yield
    finally:
        if f is not None:
            try:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                f.close()

# Posting days: twice a week, never back-to-back — Monday=0, Thursday=3.
POSTING_WEEKDAYS = {0, 3}
POST_HOUR = 9  # publish at/after 9am local (Central)

# Austin is Central time; fall back to UTC if tz data is unavailable.
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover
    LOCAL_TZ = timezone.utc


# ── Content + sequence ────────────────────────────────────────────

def load_posts() -> dict:
    """Return {number: post} from the parsed content library."""
    posts = json.loads(POSTS_PATH.read_text())
    return {p["number"]: p for p in posts}


def build_sequence(posts: dict) -> list[int]:
    """Order post numbers so each category is spread evenly across the run and
    no two consecutive posts share a category.

    Each post gets a fractional position (j + 0.5) / k where it's the j-th of k
    in its category; sorting by that interleaves every category uniformly. A
    final pass swaps apart any incidental back-to-back collisions.
    """
    piles = defaultdict(list)
    for num in sorted(posts):
        piles[posts[num]["category"]].append(num)

    ranked = []  # (position, -category_size, category, number) — deterministic
    for cat, nums in piles.items():
        k = len(nums)
        for j, num in enumerate(nums):
            ranked.append(((j + 0.5) / k, -k, cat, num))
    ranked.sort()
    seq = [num for _, _, _, num in ranked]

    # Fixup: break any remaining adjacent same-category pairs by swapping the
    # offender forward to the next slot whose neighbours differ from it.
    def cat(n):
        return posts[n]["category"]

    for i in range(1, len(seq)):
        if cat(seq[i]) != cat(seq[i - 1]):
            continue
        for m in range(i + 1, len(seq)):
            prev_ok = cat(seq[m]) != cat(seq[i - 1])
            left_ok = cat(seq[i - 1]) != cat(seq[m - 1]) if m - 1 != i else True
            right_ok = m + 1 >= len(seq) or cat(seq[i - 1]) != cat(seq[m + 1])
            if prev_ok and left_ok and right_ok:
                seq[i], seq[m] = seq[m], seq[i]
                break
    return seq


def compose_summary(post: dict) -> str:
    """Update posts have no title field, so lead the body with the headline."""
    return f"{post['title']}\n\n{post['body']}"


# ── Campaign state ────────────────────────────────────────────────

def load_state(posts: dict) -> dict:
    """Load campaign state, creating a fresh sequence on first run."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    state = {
        "sequence": build_sequence(posts),
        "cursor": 0,      # index into sequence of the next post to publish
        "log": [],        # one entry per published post
    }
    save_state(state)
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── Schedule dates ────────────────────────────────────────────────

def posting_dates(start: date, count: int) -> list[date]:
    """The next `count` Mon/Wed/Fri dates on/after `start`."""
    out, d = [], start
    while len(out) < count:
        if d.weekday() in POSTING_WEEKDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


# ── Publishing ────────────────────────────────────────────────────

def publish_next(state: dict, posts: dict, dry_run: bool) -> bool:
    """Publish the post at the cursor. Returns True if something was posted."""
    seq = state["sequence"]
    if state["cursor"] >= len(seq):
        print("Campaign complete — all posts have been published. 🎉")
        return False

    num = seq[state["cursor"]]
    post = posts[num]
    summary = compose_summary(post)

    body = g.build_update_post(summary)

    ids = g._resolve_ids()
    if not ids:
        print("ERROR: could not resolve GBP account/location IDs.")
        return False
    account_id, location_id = ids

    header = (f"#{num} · {post['category']} · "
              f"(sequence {state['cursor'] + 1}/{len(seq)})")
    if dry_run:
        import gbp_photos
        entry = gbp_photos._load_map().get(str(num), {})
        print(f"\n── DRY RUN — next up: {header} ──")
        print(f"photo (would download+geotag+stage): {entry.get('name', '(none)')}")
        g.attach_cta(body, "CALL")
        print(f"POST {g.GBP_API}/{account_id}/{location_id}/localPosts\n")
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return False

    # Attach the post's geotagged photo if one can be sourced/staged. A photo
    # problem must never block the post — it just goes out text-only that day.
    try:
        import gbp_photos
        image_url = gbp_photos.photo_url_for_post(post)
        if image_url:
            g.attach_media(body, image_url)
            print(f"  photo: {image_url}")
        else:
            print("  photo: none available — posting text-only")
    except Exception:
        logger.exception("Photo sourcing failed for post %s", num)

    g.attach_cta(body, "CALL")

    print(f"Publishing {header} …")
    result = g.create_local_post(account_id, location_id, body)
    if not result:
        print("Failed to publish — cursor not advanced; safe to retry.")
        return False

    state["log"].append({
        "number": num,
        "category": post["category"],
        "title": post["title"],
        "sequence_index": state["cursor"],
        "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "name": result.get("name", ""),
        "state": result.get("state", "?"),
        "searchUrl": result.get("searchUrl", ""),
    })
    state["cursor"] += 1
    save_state(state)
    print(f"  ✅ Posted. State: {result.get('state', '?')} | "
          f"{state['cursor']}/{len(seq)} done.")
    return True


# ── Auto-scheduler (called by the dashboard on a timer) ───────────

def is_paused() -> bool:
    return PAUSE_PATH.exists()


def set_paused(paused: bool) -> None:
    if paused:
        PAUSE_PATH.write_text("paused\n")
    elif PAUSE_PATH.exists():
        PAUSE_PATH.unlink()


def _already_posted_today(state: dict) -> bool:
    """True if the most recent published post went out today (local time)."""
    if not state["log"]:
        return False
    last = state["log"][-1]["posted_at"]
    last_local = datetime.fromisoformat(last).astimezone(LOCAL_TZ).date()
    return last_local == datetime.now(LOCAL_TZ).date()


def tick(force: bool = False) -> dict:
    """Publish today's post if one is due. Safe to call as often as you like —
    it publishes at most once per Mon/Wed/Fri and never double-posts.

    Returns a small status dict describing what happened.
    """
    posts = load_posts()
    with _campaign_lock():
        state = load_state(posts)
        now = datetime.now(LOCAL_TZ)

        if state["cursor"] >= len(state["sequence"]):
            return {"action": "complete", "posted": state["cursor"],
                    "total": len(state["sequence"])}
        if is_paused() and not force:
            return {"action": "paused"}
        if not force:
            if now.weekday() not in POSTING_WEEKDAYS or now.hour < POST_HOUR:
                return {"action": "idle", "reason": "not a posting time"}
            if _already_posted_today(state):
                return {"action": "idle", "reason": "already posted today"}

        posted = publish_next(state, posts, dry_run=False)
        return {"action": "posted" if posted else "error",
                "posted": state["cursor"], "total": len(state["sequence"])}


def run_daemon(interval: int = 1800) -> None:
    """Run forever, auto-posting on the Mon/Thu schedule. Started by the Docker
    CMD on Render so posting runs 24/7 independent of anyone opening the app.

    Refuses to run without DATA_DIR set, so `--daemon` on a laptop can never
    fire live posts by accident.
    """
    if not os.environ.get("DATA_DIR"):
        print("Refusing to start daemon without DATA_DIR set (safety guard "
              "against accidental live posting from a local machine).")
        return
    logging.info("GBP daemon up — posting weekdays %s at/after %02d:00 (%s), "
                 "checking every %ds", sorted(POSTING_WEEKDAYS), POST_HOUR,
                 LOCAL_TZ, interval)
    while True:
        try:
            logging.info("gbp tick: %s", tick())
        except Exception:
            logging.exception("GBP daemon tick failed")
        time.sleep(interval)


# ── CLI ───────────────────────────────────────────────────────────

def cmd_plan(state: dict, posts: dict) -> None:
    seq = state["sequence"]
    dates = posting_dates(date.today(), len(seq))
    print(f"\nSmooth Closing GBP campaign — {len(seq)} posts, Mon & Thu\n")
    for i, (num, d) in enumerate(zip(seq, dates)):
        done = "✓" if i < state["cursor"] else " "
        print(f"  [{done}] {d:%a %Y-%m-%d}  #{num:>3}  {posts[num]['category']}")
        print(f"          {posts[num]['title']}")
    last = dates[-1]
    print(f"\n  Runs through {last:%Y-%m-%d} (~{len(seq)//2} weeks).")


def cmd_status(state: dict, posts: dict) -> None:
    seq, cur = state["sequence"], state["cursor"]
    print(f"\nPosted: {cur}/{len(seq)}")
    if state["log"]:
        print("\nMost recent:")
        for e in state["log"][-5:]:
            print(f"  {e['posted_at'][:10]}  #{e['number']:>3}  "
                  f"[{e['state']}]  {e['title'][:60]}")
    if cur < len(seq):
        nxt = seq[cur]
        print(f"\nNext up: #{nxt} — {posts[nxt]['category']}")
        print(f"         {posts[nxt]['title']}")
    else:
        print("\nCampaign complete. 🎉")


def main():
    parser = argparse.ArgumentParser(description="GBP campaign scheduler")
    parser.add_argument("--plan", action="store_true", help="Show dated schedule")
    parser.add_argument("--status", action="store_true", help="Show progress")
    parser.add_argument("--post-next", action="store_true",
                        help="Publish the next post in the sequence")
    parser.add_argument("--daemon", action="store_true",
                        help="Run forever, auto-posting on schedule (hosted use)")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --post-next, preview without publishing")
    parser.add_argument("--force", action="store_true",
                        help="Publish even if today isn't a Mon/Wed/Fri")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.daemon:
        run_daemon()
        return

    posts = load_posts()
    state = load_state(posts)

    if args.plan:
        cmd_plan(state, posts)
    elif args.status:
        cmd_status(state, posts)
    elif args.post_next:
        if not args.dry_run and not args.force and date.today().weekday() not in POSTING_WEEKDAYS:
            print(f"Today ({date.today():%A}) isn't a posting day (Mon/Thu). "
                  f"Use --force to post anyway.")
            return
        publish_next(state, posts, args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
