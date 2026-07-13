"""
dashboard.py — SmoothClosing Team Dashboard

Simple interface for the acquisitions and dispositions teams.
Credentials are loaded from .env automatically — no sidebar config needed.

Run:
    streamlit run dashboard.py
"""

# Keep annotations lazy so PEP 604 unions (e.g. `dict | None`) don't get
# evaluated at runtime — this dashboard is launched under Python 3.9, where
# `dict | None` in a signature raises TypeError and crashes the whole app.
from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="SmoothClosing",
    page_icon="🏠",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# BASE_DIR = where this code lives (dashboard.py + all sibling .py scripts)
# DATA_DIR = where mutable state lives (leads.csv, *.json, input_pdfs/, etc.)
#
# For local development DATA_DIR defaults to BASE_DIR — same as before.
# In hosted deploys, set DATA_DIR=/data (or wherever the persistent volume is
# mounted) so user data survives container redeploys.

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR)).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Use the agents venv python if available
PYTHON = str(BASE_DIR / ".venv-agents" / "bin" / "python3")
if not Path(PYTHON).exists():
    PYTHON = sys.executable


def _script(name: str) -> str:
    """Absolute path to a sibling .py script (so it works regardless of cwd)."""
    return str(BASE_DIR / name)


def _data(name: str) -> Path:
    """Path inside DATA_DIR for a data file."""
    return DATA_DIR / name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_script(cmd: list[str], status_container, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a pipeline script and stream output to the UI in real-time.

    Uses Popen + a line-by-line read loop so the user sees progress (e.g.
    "Processing: April 5.pdf") IMMEDIATELY rather than waiting until the
    subprocess exits. This is critical for OCR-heavy steps that can run
    30+ minutes — without streaming, the dashboard looks frozen.

    timeout defaults to 10 min. Long-running steps (SMS, skip-trace,
    OCR-heavy PDF batches) should pass a larger value. On timeout the
    subprocess is killed, the partial output is shown, and a clean error
    appears in the UI (no stack trace).
    """
    import time

    status_container.info(f"Running: `{' '.join(cmd)}`")
    # PYTHONUNBUFFERED=1 forces Python subprocess to flush stdout per-line.
    # Without this, Python switches to block buffering when stdout is a pipe
    # (not a TTY), so lines sit in the buffer for minutes and the dashboard
    # appears frozen halfway through a step.
    env = {
        **os.environ,
        "PYTHONPATH": str(BASE_DIR),
        "PYTHONUNBUFFERED": "1",
    }

    # Run python with -u (also unbuffered) so any nested subprocess.run that
    # python kicks off still flushes promptly. Belt + suspenders.
    if cmd and cmd[0] == PYTHON and (len(cmd) < 2 or cmd[1] != "-u"):
        cmd = [cmd[0], "-u"] + cmd[1:]

    proc = subprocess.Popen(
        cmd,
        cwd=str(DATA_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge so we get a single chronological stream
        text=True,
        bufsize=1,
    )

    # Keep a sliding window of the most recent output lines so the UI stays
    # responsive even when scripts emit thousands of lines (e.g. per-page
    # OCR logs).
    MAX_LINES = 60
    lines: list[str] = []
    start = time.time()
    last_ui_update = 0.0
    timed_out = False

    def _flush_ui():
        # Slice off the sliding window for display.
        window = lines[-MAX_LINES:]
        try:
            status_container.code("\n".join(window), language=None)
        except Exception:
            # Streamlit can throw if the websocket is mid-reconnect — swallow
            # so the subprocess keeps running.
            pass

    try:
        for line in iter(proc.stdout.readline, ""):
            lines.append(line.rstrip("\n"))
            now = time.time()
            # Throttle UI updates to ~2/sec. Updating on every single line
            # over a 30-min OCR run flooded Streamlit's websocket and the
            # UI froze halfway through.
            if now - last_ui_update >= 0.5:
                _flush_ui()
                last_ui_update = now

            if now - start > timeout:
                proc.kill()
                timed_out = True
                break
        proc.stdout.close()
        # Final UI flush to capture the last lines emitted between throttle ticks
        _flush_ui()
        returncode = proc.wait(timeout=10)
    except Exception as e:
        # Don't let stream-reader exceptions kill the dashboard
        try:
            proc.kill()
        except Exception:
            pass
        returncode = -1
        lines.append(f"[run_script] internal error: {e}")
        status_container.code("\n".join(lines[-MAX_LINES:]), language=None)

    full_output = "\n".join(lines)

    if timed_out:
        status_container.error(
            f"Step timed out after {timeout}s and was killed. "
            f"Partial output shown above. For OCR-heavy steps, consider "
            f"running smaller batches by processing one county subfolder "
            f"at a time."
        )
        return subprocess.CompletedProcess(
            args=cmd, returncode=124, stdout=full_output, stderr="",
        )

    if returncode != 0:
        # Surface ERROR-level lines clearly so the user can see what broke
        errors = [
            l for l in lines
            if any(tag in l for tag in ("ERROR", "Error", "Traceback", "FAILED"))
        ]
        if errors:
            status_container.error("\n".join(errors[-10:]))

    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=full_output, stderr="",
    )


def count_csv_rows(path: str) -> int:
    p = _data(path) if not Path(path).is_absolute() else Path(path)
    if not p.exists():
        return 0
    with open(p) as f:
        return sum(1 for _ in csv.reader(f)) - 1  # minus header


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("🏠 SmoothClosing")

tab_tv, tab_acq, tab_dispo, tab_aoh, tab_resimpli, tab_gbp = st.tabs(
    ["📺 TV Dashboard", "Acquisitions", "Dispositions",
     "Heirship Affidavit", "REsimpli Sync", "📣 GBP Posts"]
)


# ===========================================================================
# GBP CAMPAIGN — status + manual controls. Automatic posting is handled by a
# dedicated 24/7 daemon (gbp_scheduler.py --daemon, started by the Docker CMD),
# not from here, so it runs even when nobody has the dashboard open.
# ===========================================================================

with tab_gbp:
    st.subheader("📣 Google Business Profile — auto-posting campaign")
    st.caption("Posting runs automatically on **GitHub Actions** (Mon & Thu, ~9am CT) — "
               "a geotagged property photo + Call button per post. This tab is a "
               "read-only status view.")
    try:
        import gbp_scheduler

        gposts = gbp_scheduler.load_posts()
        gstate = gbp_scheduler.load_state(gposts)
        gseq, gcur = gstate["sequence"], gstate["cursor"]

        c1, c2, c3 = st.columns(3)
        c1.metric("Posted", f"{gcur}/{len(gseq)}")
        c2.metric("Cadence", "Mon·Thu ~9am CT")
        c3.metric("Status", "✅ Complete" if gcur >= len(gseq) else "▶ Running")
        st.progress(gcur / len(gseq) if gseq else 0.0)

        if gcur < len(gseq):
            nxt = gposts[gseq[gcur]]
            st.markdown(f"**Next up:** #{gseq[gcur]} · {nxt['category']}  \n{nxt['title']}")
        else:
            st.success("All posts published. 🎉")

        if gstate["log"]:
            st.markdown("**Recently posted**")
            rows = [{"when": e["posted_at"][:10], "#": e["number"],
                     "category": e["category"], "title": e["title"][:55],
                     "state": e["state"]} for e in gstate["log"][-10:][::-1]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.caption("To pause: disable the **GBP Campaign** workflow in the repo's "
                   "GitHub → Actions tab. Status here updates on each post.")
    except Exception as e:
        st.error(f"GBP status unavailable: {e}")

# ===========================================================================
# TV DASHBOARD — Big, glanceable display for an always-on TV
# ===========================================================================

with tab_tv:
    # This is a wall display, so it auto-refreshes on a timer. Auto-refresh was
    # removed once because it orphaned long-running pipelines — that no longer
    # applies (the pipeline runs as a detached process, tracked in
    # pipeline.state). Two safeguards keep it well-behaved:
    #   1. It's a PER-SESSION toggle, so an operator can turn it off in their
    #      own browser without affecting the TV (Streamlit sessions are
    #      per-browser).
    #   2. It PAUSES while a pipeline is actively running, so it can never
    #      disturb a live run or its monitoring.
    _c_auto, _c_now = st.columns([1, 4])
    with _c_auto:
        _tv_auto = st.toggle("🔄 Auto", value=True, key="tv_autorefresh_on",
                             help="Auto-refresh this display every 30 seconds")
    with _c_now:
        if st.button("Refresh now", key="tv_manual_refresh",
                     help="Re-read the latest REsimpli CSVs from disk"):
            st.rerun()

    _pstate_f = _data("pipeline.state")
    _pipe_running = (_pstate_f.exists()
                     and _pstate_f.read_text().strip().startswith("running"))
    if _tv_auto and not _pipe_running:
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=30_000, key="tv_autorefresh_tick")
        except Exception:
            pass
    elif _tv_auto and _pipe_running:
        st.caption("⏸ Auto-refresh paused while the pipeline is running "
                   "(resumes automatically when it finishes).")

    # Read latest snapshots from disk
    leads_path = _data("resimpli_latest_leads.csv")
    inv_path = _data("resimpli_latest_inventory.csv")

    if not leads_path.exists() and not inv_path.exists():
        st.markdown(
            "<div style='text-align:center; padding:80px; color:#888; "
            "font-size:24px;'>📺 No data yet. Upload CSVs in the "
            "<b>REsimpli Sync</b> tab to populate this dashboard.</div>",
            unsafe_allow_html=True,
        )
    else:
        from resimpli_importer import parse_resimpli_csv
        from inventory_importer import parse_inventory_csv, parse_dollar
        from collections import defaultdict
        from datetime import datetime

        leads_rows = (
            parse_resimpli_csv(leads_path) if leads_path.exists() else []
        )
        inv_rows = (
            parse_inventory_csv(inv_path) if inv_path.exists() else []
        )

        # View toggle — segmented control at top
        view = st.radio(
            "View",
            ["📊 Overview", "📝 Under Contract", "📦 Inventory"],
            horizontal=True,
            label_visibility="collapsed",
            key="tv_view_selector",
        )

        # ---- Aggregate ---------------------------------------------------
        pipeline_profit = sum(
            parse_dollar(r.get("Expected Profit", "")) for r in leads_rows
        )
        inventory_profit = sum(
            parse_dollar(r.get("Expected Profit", "")) for r in inv_rows
        )
        total_profit = pipeline_profit + inventory_profit

        # Per-person aggregation. Match inventory to AM via Property ID first;
        # fall back to "(team)" for unmatched inventory rows.
        am_by_pid = {
            r.get("Property ID"): r.get("Acquisition Manager", "").strip() or "(team)"
            for r in leads_rows
        }
        per_person = defaultdict(lambda: {
            "pipeline_profit": 0.0,
            "pipeline_deals": 0,
            "inventory_profit": 0.0,
            "inventory_props": 0,
            "total_profit": 0.0,
        })
        for r in leads_rows:
            am = r.get("Acquisition Manager", "").strip() or "(team)"
            p = parse_dollar(r.get("Expected Profit", ""))
            per_person[am]["pipeline_deals"] += 1
            per_person[am]["pipeline_profit"] += p
            per_person[am]["total_profit"] += p
        for r in inv_rows:
            pid = r.get("Property ID")
            am = am_by_pid.get(pid, "(team)")
            p = parse_dollar(r.get("Expected Profit", ""))
            per_person[am]["inventory_props"] += 1
            per_person[am]["inventory_profit"] += p
            per_person[am]["total_profit"] += p

        # Sort and rank — but exclude "(team)" bucket (inventory items
        # we couldn't attribute to a specific Acquisition Manager) from
        # the leaderboard. They still count in the company-wide totals
        # at the top.
        ranked = sorted(
            [(n, d) for n, d in per_person.items() if n != "(team)"],
            key=lambda x: -x[1]["total_profit"],
        )
        max_total = ranked[0][1]["total_profit"] if ranked else 1
        unattributed = per_person.get("(team)", None)

        last_updated = datetime.now().strftime("%I:%M %p")

        # Which sections render for which view
        show_overview_hero = view == "📊 Overview"
        show_uc_hero = view == "📝 Under Contract"
        show_inv_hero = view == "📦 Inventory"
        show_leaderboard = view in ("📊 Overview", "📝 Under Contract")
        show_inventory_status = view in ("📊 Overview", "📦 Inventory")
        show_inventory_detail = view == "📦 Inventory"
        show_uc_detail = view == "📝 Under Contract"
        show_top_deals = view in ("📊 Overview",)

        # ---- HERO HEADER ---------------------------------------------
        if show_overview_hero:
            st.markdown(
            f"""
            <div style='
                background: linear-gradient(135deg, #0f1729 0%, #1e3a5f 50%, #2d5f8f 100%);
                padding: 36px 48px;
                border-radius: 18px;
                box-shadow: 0 8px 24px rgba(0,0,0,0.35);
                margin-bottom: 28px;
                border: 1px solid #2d5f8f;
            '>
                <div style='display:flex; justify-content:space-between;
                            align-items:flex-start; margin-bottom: 16px;'>
                    <div style='font-size: 22px; color: #b8d4ea; letter-spacing: 2px;
                                text-transform: uppercase; font-weight: 600;'>
                        🏆 SmoothClosing — Live Pipeline
                    </div>
                    <div style='font-size: 16px; color: #7ba6cc;'>
                        Updated {last_updated}
                    </div>
                </div>
                <div style='font-size: 18px; color: #b8d4ea;
                            text-transform: uppercase; letter-spacing: 1px;
                            margin-bottom: 8px; font-weight: 500;'>
                    Total Expected Profit
                </div>
                <div style='font-size: 96px; font-weight: 900; color: #ffffff;
                            line-height: 1; letter-spacing: -2px;'>
                    ${total_profit:,.0f}
                </div>
                <div style='display: flex; gap: 64px; margin-top: 24px;'>
                    <div>
                        <div style='font-size: 14px; color: #7ba6cc; text-transform: uppercase;
                                    letter-spacing: 1px;'>Pipeline (Acquisitions)</div>
                        <div style='font-size: 38px; font-weight: 700; color: #4CC9F0;'>
                            ${pipeline_profit:,.0f}
                        </div>
                        <div style='font-size: 14px; color: #b8d4ea;'>
                            {len(leads_rows)} active leads
                        </div>
                    </div>
                    <div>
                        <div style='font-size: 14px; color: #7ba6cc; text-transform: uppercase;
                                    letter-spacing: 1px;'>Inventory (Portfolio)</div>
                        <div style='font-size: 38px; font-weight: 700; color: #F8961E;'>
                            ${inventory_profit:,.0f}
                        </div>
                        <div style='font-size: 14px; color: #b8d4ea;'>
                            {len(inv_rows)} properties owned
                        </div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ---- UNDER CONTRACT HERO -----------------------------------------
        if show_uc_hero:
            uc_count = len(leads_rows)
            uc_total_contract = sum(
                parse_dollar(r.get("Under Contract Price", ""))
                for r in leads_rows
            )
            avg_uc_profit = (
                pipeline_profit / uc_count if uc_count else 0
            )
            st.markdown(
                f"""
                <div style='
                    background: linear-gradient(135deg, #0f1729 0%, #1e3a5f 50%, #4CC9F0 200%);
                    padding: 36px 48px;
                    border-radius: 18px;
                    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
                    margin-bottom: 28px;
                    border-left: 8px solid #4CC9F0;
                '>
                    <div style='display:flex; justify-content:space-between;
                                align-items:flex-start; margin-bottom: 16px;'>
                        <div style='font-size: 22px; color: #b8d4ea;
                                    letter-spacing: 2px; text-transform: uppercase;
                                    font-weight: 600;'>
                            📝 Under Contract — Pipeline
                        </div>
                        <div style='font-size: 16px; color: #7ba6cc;'>
                            Updated {last_updated}
                        </div>
                    </div>
                    <div style='font-size: 18px; color: #b8d4ea;
                                text-transform: uppercase; letter-spacing: 1px;
                                margin-bottom: 8px;'>
                        Expected Profit on Active Deals
                    </div>
                    <div style='font-size: 96px; font-weight: 900; color: #ffffff;
                                line-height: 1; letter-spacing: -2px;'>
                        ${pipeline_profit:,.0f}
                    </div>
                    <div style='display: flex; gap: 64px; margin-top: 24px;'>
                        <div>
                            <div style='font-size: 14px; color: #7ba6cc;
                                        text-transform: uppercase; letter-spacing: 1px;'>
                                Active Deals
                            </div>
                            <div style='font-size: 38px; font-weight: 700;
                                        color: #4CC9F0;'>{uc_count}</div>
                        </div>
                        <div>
                            <div style='font-size: 14px; color: #7ba6cc;
                                        text-transform: uppercase; letter-spacing: 1px;'>
                                Total Under Contract
                            </div>
                            <div style='font-size: 38px; font-weight: 700;
                                        color: #4CC9F0;'>${uc_total_contract:,.0f}</div>
                        </div>
                        <div>
                            <div style='font-size: 14px; color: #7ba6cc;
                                        text-transform: uppercase; letter-spacing: 1px;'>
                                Avg Profit / Deal
                            </div>
                            <div style='font-size: 38px; font-weight: 700;
                                        color: #4CC9F0;'>${avg_uc_profit:,.0f}</div>
                        </div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # ---- INVENTORY HERO ----------------------------------------------
        if show_inv_hero:
            inv_count = len(inv_rows)
            inv_top_deal_profit = max(
                (parse_dollar(r.get("Expected Profit", "")) for r in inv_rows),
                default=0,
            )
            avg_inv_profit = (
                inventory_profit / inv_count if inv_count else 0
            )
            st.markdown(
                f"""
                <div style='
                    background: linear-gradient(135deg, #0f1729 0%, #5f3a1e 50%, #F8961E 200%);
                    padding: 36px 48px;
                    border-radius: 18px;
                    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
                    margin-bottom: 28px;
                    border-left: 8px solid #F8961E;
                '>
                    <div style='display:flex; justify-content:space-between;
                                align-items:flex-start; margin-bottom: 16px;'>
                        <div style='font-size: 22px; color: #f0d5b8;
                                    letter-spacing: 2px; text-transform: uppercase;
                                    font-weight: 600;'>
                            📦 Inventory — Active Portfolio
                        </div>
                        <div style='font-size: 16px; color: #cc9b7b;'>
                            Updated {last_updated}
                        </div>
                    </div>
                    <div style='font-size: 18px; color: #f0d5b8;
                                text-transform: uppercase; letter-spacing: 1px;
                                margin-bottom: 8px;'>
                        Expected Profit on Inventory
                    </div>
                    <div style='font-size: 96px; font-weight: 900; color: #ffffff;
                                line-height: 1; letter-spacing: -2px;'>
                        ${inventory_profit:,.0f}
                    </div>
                    <div style='display: flex; gap: 64px; margin-top: 24px;'>
                        <div>
                            <div style='font-size: 14px; color: #cc9b7b;
                                        text-transform: uppercase; letter-spacing: 1px;'>
                                Properties
                            </div>
                            <div style='font-size: 38px; font-weight: 700;
                                        color: #F8961E;'>{inv_count}</div>
                        </div>
                        <div>
                            <div style='font-size: 14px; color: #cc9b7b;
                                        text-transform: uppercase; letter-spacing: 1px;'>
                                Avg Profit / Property
                            </div>
                            <div style='font-size: 38px; font-weight: 700;
                                        color: #F8961E;'>${avg_inv_profit:,.0f}</div>
                        </div>
                        <div>
                            <div style='font-size: 14px; color: #cc9b7b;
                                        text-transform: uppercase; letter-spacing: 1px;'>
                                Top Deal
                            </div>
                            <div style='font-size: 38px; font-weight: 700;
                                        color: #F8961E;'>${inv_top_deal_profit:,.0f}</div>
                        </div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # ---- LEADERBOARD -------------------------------------------------
        if show_leaderboard:
            st.markdown(
                "<div style='font-size:34px; font-weight:800; margin: 28px 0 16px 0;'>"
                "🏅 Team Leaderboard</div>",
                unsafe_allow_html=True,
            )

        medals = ["🥇", "🥈", "🥉"]
        person_colors = ["#FFD700", "#C0C0C0", "#CD7F32",
                         "#4CC9F0", "#90BE6D", "#9B5DE5", "#F94144"]

        for i, (name, data) in enumerate(ranked if show_leaderboard else []):
            medal = medals[i] if i < 3 else f"  {i+1}."
            color = person_colors[i % len(person_colors)]
            pct = (
                (data["total_profit"] / max_total * 100) if max_total else 0
            )
            initials = "".join(
                w[0].upper() for w in name.split() if w
            )[:2] or "?"

            st.markdown(
                f"""
                <div style='
                    background: linear-gradient(90deg, rgba(30,58,95,0.5) 0%,
                                                       rgba(30,58,95,0.1) 100%);
                    padding: 22px 28px;
                    border-radius: 14px;
                    margin-bottom: 14px;
                    border-left: 6px solid {color};
                    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
                '>
                    <div style='display: flex; align-items: center;
                                justify-content: space-between;'>
                        <div style='display: flex; align-items: center; gap: 24px;'>
                            <div style='font-size: 42px; font-weight: 700; min-width: 60px;'>
                                {medal}
                            </div>
                            <div style='
                                width: 64px; height: 64px; border-radius: 50%;
                                background: {color}; color: #0f1729;
                                display: flex; align-items: center; justify-content: center;
                                font-size: 26px; font-weight: 800;
                            '>{initials}</div>
                            <div>
                                <div style='font-size: 30px; font-weight: 700;
                                            color: #ffffff; line-height: 1.1;'>
                                    {name}
                                </div>
                                <div style='font-size: 14px; color: #b8d4ea;
                                            margin-top: 4px;'>
                                    {data["pipeline_deals"]} pipeline deal(s)
                                    • {data["inventory_props"]} inventory prop(s)
                                </div>
                            </div>
                        </div>
                        <div style='text-align: right;'>
                            <div style='font-size: 44px; font-weight: 900;
                                        color: {color}; line-height: 1;'>
                                ${data["total_profit"]:,.0f}
                            </div>
                            <div style='font-size: 13px; color: #7ba6cc;
                                        margin-top: 4px;'>
                                pipeline ${data["pipeline_profit"]:,.0f}
                                • inventory ${data["inventory_profit"]:,.0f}
                            </div>
                        </div>
                    </div>
                    <div style='
                        margin-top: 14px;
                        background: rgba(255,255,255,0.08);
                        border-radius: 8px;
                        height: 12px;
                        overflow: hidden;
                    '>
                        <div style='
                            width: {pct:.1f}%;
                            height: 100%;
                            background: linear-gradient(90deg, {color} 0%,
                                                              {color}99 100%);
                            border-radius: 8px;
                        '></div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # Note about unattributed inventory (not tied to any AM via Property ID)
        if show_leaderboard and unattributed and unattributed["inventory_props"]:
            st.markdown(
                f"""
                <div style='
                    background: rgba(87,117,144,0.18);
                    padding: 12px 20px;
                    border-radius: 10px;
                    border-left: 4px solid #577590;
                    font-size: 14px;
                    color: #b8d4ea;
                    margin-top: 8px;
                '>
                    <b>{unattributed["inventory_props"]} inventory propert(ies)
                    totaling ${unattributed["inventory_profit"]:,.0f}</b>
                    aren't tied to a specific Acquisition Manager (no matching
                    Property ID in the leads export). They count in the company
                    total above but not in the leaderboard.
                </div>
                """,
                unsafe_allow_html=True,
            )

        # ---- INVENTORY STATUS BREAKDOWN ----------------------------------
        if inv_rows and show_inventory_status:
            from inventory_importer import summarize as inv_summarize
            inv_stats = inv_summarize(inv_rows)
            st.markdown(
                "<div style='font-size:34px; font-weight:800; margin: 36px 0 16px 0;'>"
                "📦 Inventory Status</div>",
                unsafe_allow_html=True,
            )
            status_colors = {
                "Listed For Sale": "#4CC9F0",
                "Under Rehab": "#F8961E",
                "Under Contract": "#9B5DE5",
                "New Inventory": "#90BE6D",
            }
            cols = st.columns(min(len(inv_stats["by_status"]), 4) or 1)
            for i, (status, data) in enumerate(inv_stats["by_status"].items()):
                color = status_colors.get(status, "#577590")
                with cols[i % len(cols)]:
                    st.markdown(
                        f"""
                        <div style='
                            background: rgba(30,58,95,0.4);
                            padding: 24px;
                            border-radius: 12px;
                            border-top: 4px solid {color};
                            text-align: center;
                            min-height: 160px;
                        '>
                            <div style='font-size: 14px; color: #b8d4ea;
                                        text-transform: uppercase; letter-spacing: 1px;'>
                                {status}
                            </div>
                            <div style='font-size: 56px; font-weight: 900; color: {color};
                                        line-height: 1.1; margin: 8px 0;'>
                                {data["count"]}
                            </div>
                            <div style='font-size: 22px; font-weight: 700; color: #ffffff;'>
                                ${data["profit"]:,.0f}
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

        # ---- TOP DEALS ---------------------------------------------------
        all_deals = [] if show_top_deals else None
        if not show_top_deals:
            top_deals = []
        for r in (leads_rows if show_top_deals else []):
            all_deals.append({
                "address": r.get("Property Street Address", ""),
                "city": r.get("Property City", "").strip(),
                "profit": parse_dollar(r.get("Expected Profit", "")),
                "status": r.get("Lead Status", ""),
                "kind": "Pipeline",
            })
        for r in (inv_rows if show_top_deals else []):
            all_deals.append({
                "address": r.get("Property Street Address", ""),
                "city": r.get("Property City", "").strip(),
                "profit": parse_dollar(r.get("Expected Profit", "")),
                "status": r.get("Property Status", ""),
                "kind": "Inventory",
            })
        if show_top_deals:
            top_deals = sorted(all_deals, key=lambda d: -d["profit"])[:8]

        if top_deals:
            st.markdown(
                "<div style='font-size:34px; font-weight:800; margin: 36px 0 16px 0;'>"
                "🏆 Top Deals</div>",
                unsafe_allow_html=True,
            )
            rows_html = ""
            for i, d in enumerate(top_deals):
                kind_color = "#4CC9F0" if d["kind"] == "Pipeline" else "#F8961E"
                rows_html += f"""
                <tr style='border-bottom: 1px solid rgba(255,255,255,0.08);'>
                    <td style='padding: 14px 8px; font-size: 22px; font-weight: 700;
                               color: #b8d4ea; width: 50px;'>{i+1}</td>
                    <td style='padding: 14px 8px; font-size: 22px; font-weight: 600;
                               color: #ffffff;'>
                        {d["address"]}{', ' + d["city"] if d["city"] else ''}
                    </td>
                    <td style='padding: 14px 8px; font-size: 14px;'>
                        <span style='background: {kind_color}; color: #0f1729;
                                     padding: 4px 10px; border-radius: 6px;
                                     font-weight: 700;'>{d["kind"]}</span>
                    </td>
                    <td style='padding: 14px 8px; font-size: 16px; color: #b8d4ea;'>
                        {d["status"]}
                    </td>
                    <td style='padding: 14px 8px; font-size: 26px; font-weight: 900;
                               color: #4CC9F0; text-align: right;'>
                        ${d["profit"]:,.0f}
                    </td>
                </tr>
                """
            st.markdown(
                f"""
                <table style='width: 100%; border-collapse: collapse;
                              background: rgba(30,58,95,0.3);
                              border-radius: 12px; overflow: hidden;'>
                    {rows_html}
                </table>
                """,
                unsafe_allow_html=True,
            )

        # ---- UNDER CONTRACT DEAL DETAIL TABLE ----------------------------
        if show_uc_detail and leads_rows:
            st.markdown(
                "<div style='font-size:34px; font-weight:800; "
                "margin: 36px 0 16px 0;'>📝 All Under-Contract Deals</div>",
                unsafe_allow_html=True,
            )
            uc_sorted = sorted(
                leads_rows,
                key=lambda r: -parse_dollar(r.get("Expected Profit", "")),
            )
            rows_html = ""
            for r in uc_sorted:
                profit = parse_dollar(r.get("Expected Profit", ""))
                contract = parse_dollar(r.get("Under Contract Price", ""))
                am = r.get("Acquisition Manager", "").strip() or "(team)"
                addr = r.get("Property Street Address", "")
                city = r.get("Property City", "").strip()
                lead_name = (
                    f"{r.get('First Name','')} {r.get('Last Name','')}"
                ).strip()
                close_date = r.get("Schedule Closing Date", "")
                rows_html += f"""
                <tr style='border-bottom: 1px solid rgba(255,255,255,0.08);'>
                    <td style='padding: 14px 12px; font-size: 22px; font-weight: 600;
                               color: #ffffff;'>
                        {addr}{', ' + city if city else ''}
                    </td>
                    <td style='padding: 14px 12px; font-size: 16px; color: #b8d4ea;'>
                        {lead_name}
                    </td>
                    <td style='padding: 14px 12px; font-size: 16px; color: #4CC9F0;
                               font-weight: 600;'>
                        {am}
                    </td>
                    <td style='padding: 14px 12px; font-size: 16px; color: #b8d4ea;'>
                        {close_date}
                    </td>
                    <td style='padding: 14px 12px; font-size: 18px; color: #b8d4ea;
                               text-align: right;'>
                        ${contract:,.0f}
                    </td>
                    <td style='padding: 14px 12px; font-size: 26px; font-weight: 900;
                               color: #4CC9F0; text-align: right;'>
                        ${profit:,.0f}
                    </td>
                </tr>
                """
            st.markdown(
                f"""
                <table style='width: 100%; border-collapse: collapse;
                              background: rgba(30,58,95,0.3);
                              border-radius: 12px; overflow: hidden;'>
                    <thead>
                        <tr style='background: rgba(76,201,240,0.15);'>
                            <th style='padding: 14px 12px; text-align: left;
                                       font-size: 13px; color: #7ba6cc;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Property</th>
                            <th style='padding: 14px 12px; text-align: left;
                                       font-size: 13px; color: #7ba6cc;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Lead</th>
                            <th style='padding: 14px 12px; text-align: left;
                                       font-size: 13px; color: #7ba6cc;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Acq Manager</th>
                            <th style='padding: 14px 12px; text-align: left;
                                       font-size: 13px; color: #7ba6cc;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Close Date</th>
                            <th style='padding: 14px 12px; text-align: right;
                                       font-size: 13px; color: #7ba6cc;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Contract</th>
                            <th style='padding: 14px 12px; text-align: right;
                                       font-size: 13px; color: #7ba6cc;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Expected Profit</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
                """,
                unsafe_allow_html=True,
            )

        # ---- INVENTORY DEAL DETAIL TABLE ---------------------------------
        if show_inventory_detail and inv_rows:
            st.markdown(
                "<div style='font-size:34px; font-weight:800; "
                "margin: 36px 0 16px 0;'>📦 All Inventory Properties</div>",
                unsafe_allow_html=True,
            )
            inv_sorted = sorted(
                inv_rows,
                key=lambda r: -parse_dollar(r.get("Expected Profit", "")),
            )
            status_colors = {
                "Listed For Sale": "#4CC9F0",
                "Under Rehab": "#F8961E",
                "Under Contract": "#9B5DE5",
                "New Inventory": "#90BE6D",
            }
            rows_html = ""
            for r in inv_sorted:
                profit = parse_dollar(r.get("Expected Profit", ""))
                addr = r.get("Property Street Address", "")
                city = r.get("Property City", "").strip()
                status = r.get("Property Status", "")
                ptype = r.get("Project Type", "")
                purchase_date = r.get("Purchase Date", "")
                color = status_colors.get(status, "#577590")
                rows_html += f"""
                <tr style='border-bottom: 1px solid rgba(255,255,255,0.08);'>
                    <td style='padding: 14px 12px; font-size: 22px; font-weight: 600;
                               color: #ffffff;'>
                        {addr}{', ' + city if city else ''}
                    </td>
                    <td style='padding: 14px 12px; font-size: 14px;'>
                        <span style='background: {color}; color: #0f1729;
                                     padding: 4px 10px; border-radius: 6px;
                                     font-weight: 700;'>{status}</span>
                    </td>
                    <td style='padding: 14px 12px; font-size: 16px; color: #b8d4ea;'>
                        {ptype}
                    </td>
                    <td style='padding: 14px 12px; font-size: 16px; color: #b8d4ea;'>
                        {purchase_date}
                    </td>
                    <td style='padding: 14px 12px; font-size: 26px; font-weight: 900;
                               color: #F8961E; text-align: right;'>
                        ${profit:,.0f}
                    </td>
                </tr>
                """
            st.markdown(
                f"""
                <table style='width: 100%; border-collapse: collapse;
                              background: rgba(95,58,30,0.2);
                              border-radius: 12px; overflow: hidden;'>
                    <thead>
                        <tr style='background: rgba(248,150,30,0.15);'>
                            <th style='padding: 14px 12px; text-align: left;
                                       font-size: 13px; color: #cc9b7b;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Property</th>
                            <th style='padding: 14px 12px; text-align: left;
                                       font-size: 13px; color: #cc9b7b;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Status</th>
                            <th style='padding: 14px 12px; text-align: left;
                                       font-size: 13px; color: #cc9b7b;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Project Type</th>
                            <th style='padding: 14px 12px; text-align: left;
                                       font-size: 13px; color: #cc9b7b;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Purchased</th>
                            <th style='padding: 14px 12px; text-align: right;
                                       font-size: 13px; color: #cc9b7b;
                                       text-transform: uppercase; letter-spacing: 1px;'>
                                Expected Profit</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
                """,
                unsafe_allow_html=True,
            )

        st.markdown(
            "<div style='text-align:center; padding:30px; color:#577590; "
            "font-size:13px;'>Auto-refreshes every 60 seconds — "
            "data sourced from the latest CSV uploads in the REsimpli Sync tab.</div>",
            unsafe_allow_html=True,
        )


# ===========================================================================
# CHAT TAB — Talk to the orchestrator
# ===========================================================================

# ===========================================================================
# ACQUISITIONS TAB
# ===========================================================================

with tab_acq:
    st.header("Foreclosure Lead Pipeline")

    col1, col2, col3, col4 = st.columns(4)

    # --- Step 1: Download ---
    with col1:
        st.subheader("1. Download")
        st.caption("Pull new PDFs from county websites")
        if st.button("Download PDFs", type="primary", key="dl"):
            with st.spinner("Downloading..."):
                out = run_script([PYTHON, _script("county_downloader.py")], st.empty())
                if out.returncode == 0:
                    st.success("Done")
                else:
                    st.error("Failed — check logs")

    # --- Step 2: Process ---
    with col2:
        st.subheader("2. Process")
        st.caption("Parse PDFs + estimate equity")
        include_equity = st.checkbox("Include equity (uses RentCast credits)", value=False)
        if st.button("Process Leads", type="primary", key="proc"):
            st.session_state["pipeline_running"] = True
            log_area = st.empty()
            with st.spinner("Parsing PDFs (OCR can take ~1 min per PDF)..."):
                # Bump to 1 hour — OCR over 20+ scanned PDFs can take 30+ min,
                # and TimeoutExpired silently kills the dashboard otherwise.
                try:
                    out = run_script(
                        [PYTHON, _script("main.py"),
                         "--input", "./input_pdfs", "--output", "leads.csv"],
                        log_area, timeout=3600,
                    )
                finally:
                    st.session_state["pipeline_running"] = False
            if out.returncode == 0:
                count = count_csv_rows("leads.csv")
                st.success(f"{count} leads extracted")
                if include_equity:
                    with st.spinner("Running equity estimator..."):
                        out2 = run_script([PYTHON, _script("equity_estimator.py"), "--input", "leads.csv", "--output", "leads_with_equity.csv"], log_area)
                    if out2.returncode == 0:
                        st.success("Equity estimates added")
            else:
                st.error("Processing failed")

    # --- Step 3: Skip Trace ---
    with col3:
        st.subheader("3. Skip Trace")
        st.caption("Find phone numbers for leads from the LAST pipeline run only")
        trace_limit = st.number_input("Max leads", min_value=0, value=0, step=5, key="trace_lim",
                                       help="0 = all leads from the latest run")
        if st.button("Skip Trace", type="primary", key="trace"):
            # Prefer leads_new_equity.csv (has equity data) over leads_new.csv.
            # Intentionally NOT falling back to leads.csv - that would re-trace
            # accumulated old leads from prior runs.
            if _data("leads_new_equity.csv").exists():
                input_file = "leads_new_equity.csv"
            elif _data("leads_new.csv").exists():
                input_file = "leads_new.csv"
            else:
                st.error(
                    "No leads_new.csv found. Run Process Leads (or Run Full Pipeline) "
                    "first - Skip Trace only operates on the latest run's new leads."
                )
                st.stop()

            row_count = count_csv_rows(input_file)
            if row_count == 0:
                st.warning(
                    f"{input_file} is empty - the last pipeline run found 0 new leads. "
                    "Nothing to skip trace."
                )
                st.stop()

            st.caption(f"Tracing {row_count} new lead(s) from `{input_file}`")
            cmd = [PYTHON, _script("skipgenie.py"), "--input", input_file, "--output", "leads_new_traced.csv"]
            if trace_limit > 0:
                cmd.extend(["--max-relatives", "0"])  # faster
            # Skip trace is ~1 min per lead with relatives. Give 1 hour.
            with st.spinner("Skip tracing... this takes a while"):
                out = run_script(cmd, st.empty(), timeout=60 * 60)
            if out.returncode == 0:
                st.success("Skip trace complete -> leads_new_traced.csv")

    # --- Step 4: Text ---
    with col4:
        st.subheader("4. Text")
        st.caption("Text ONLY the leads from the latest pipeline run")
        dry_run = st.checkbox("Dry run first", value=True, key="sms_dry")
        if st.button("Send Texts", type="primary", key="sms"):
            if not _data("leads_new_traced.csv").exists():
                st.error(
                    "No leads_new_traced.csv found. Run Skip Trace first - "
                    "Send Texts only operates on freshly-traced new leads."
                )
                st.stop()

            row_count = count_csv_rows("leads_new_traced.csv")
            if row_count == 0:
                st.warning("leads_new_traced.csv is empty - nothing to text.")
                st.stop()

            st.caption(
                f"{row_count} new lead(s) in leads_new_traced.csv. "
                "Previously-texted numbers (from sms_history.csv) will be skipped automatically."
            )
            cmd = [PYTHON, _script("ringcentral_sms.py"),
                   "--input", "leads_new_traced.csv",
                   "--output", "leads_new_sms_sent.csv"]
            if dry_run:
                cmd.append("--dry-run")
            # SMS parallelizes across families (default 8 workers) but keeps
            # the 5-min intra-family spacing between relatives. Wall time is
            # roughly (families / workers) × 15 min for an average family.
            # 4-hour timeout comfortably covers a 100+ lead batch.
            sms_timeout = 4 * 60 * 60  # 14400 sec = 4 hours
            with st.spinner("Sending..." if not dry_run else "Dry run..."):
                out = run_script(cmd, st.empty(), timeout=sms_timeout)
            if out.returncode == 0:
                if dry_run:
                    st.info("Dry run complete - review output above. Uncheck 'Dry run' to send for real.")
                else:
                    st.success("Texts sent (and sms_history.csv updated)")

        # Sync to Sheet button: marks Call Status column for every row
        # whose phone number is in sms_history.csv. Safe to click any time
        # after Send Texts; never overwrites manual Call Status entries.
        st.divider()
        if st.button("Sync to Sheet", key="sync_sheet",
                     help="Mark Call Status = 'Texted YYYY-MM-DD' in the sheet "
                          "for every row whose phone is in sms_history.csv. "
                          "Skips rows that already have a Call Status."):
            with st.spinner("Syncing..."):
                out = run_script(
                    [PYTHON, _script("sync_call_status.py")],
                    st.empty(),
                    timeout=120,
                )
            if out.returncode == 0:
                st.success("Sheet synced. Check the Call Status column.")

    # --- Run All ---
    st.divider()
    st.caption(
        "Full pipeline = download -> parse new PDFs -> skip trace NEW leads -> push new leads to sheet. "
        "Texting is intentionally NOT wired in - use the Send Texts button above after reviewing."
    )
    # ─── Detached pipeline runner ──────────────────────────────────────
    # The pipeline takes 30+ minutes to OCR a batch of PDFs. Streamlit
    # can't host a job that long — any rerun (browser refresh, network
    # blip, autorefresh tick, tab switch in some cases) makes st.button
    # return False on the next pass, abandoning the orchestrating code.
    #
    # So we spawn pipeline_runner.py as a DETACHED background process
    # (start_new_session=True). It survives Streamlit reruns and even
    # the user closing the browser. The dashboard polls three files
    # written by the runner:
    #   pipeline.log    — combined stdout/stderr
    #   pipeline.pid    — written on start, removed on clean exit
    #   pipeline.state  — single line: "running:STEP" / "done" / "failed:STEP"
    pid_file = _data("pipeline.pid")
    log_file = _data("pipeline.log")
    state_file = _data("pipeline.state")

    def _pipeline_alive() -> bool:
        if not pid_file.exists():
            return False
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return False
        try:
            os.kill(pid, 0)  # signal 0 = "is alive?"
            return True
        except OSError:
            return False

    is_running = _pipeline_alive()
    current_state = (
        state_file.read_text().strip() if state_file.exists() else ""
    )

    col_pl1, col_pl2 = st.columns([3, 1])
    with col_pl1:
        if st.button(
            "Run Full Pipeline",
            type="secondary",
            key="run_all",
            disabled=is_running,
        ):
            # Truncate log so the new run starts fresh visually
            log_file.write_text("")
            # Spawn detached
            env = {
                **os.environ,
                "PYTHONPATH": str(BASE_DIR),
                "PYTHONUNBUFFERED": "1",
                "DATA_DIR": str(DATA_DIR),
            }
            subprocess.Popen(
                [PYTHON, "-u", _script("pipeline_runner.py")],
                cwd=str(BASE_DIR),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach: survives our process death
            )
            st.success(
                "✅ Pipeline started in the background. It will keep "
                "running even if you refresh this page or close the "
                "browser. Progress shown below."
            )
            time.sleep(0.5)
            st.rerun()  # immediately reflect "running" state
    with col_pl2:
        if is_running and st.button("Stop Pipeline", type="primary",
                                     key="stop_pipeline"):
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 15)  # SIGTERM
                st.warning(f"Sent stop signal to PID {pid}")
            except (ValueError, OSError) as e:
                st.error(f"Couldn't stop: {e}")
            time.sleep(0.5)
            st.rerun()

    # Live status indicator
    if is_running:
        st.info(
            f"🟢 **Pipeline running** — state: `{current_state or 'starting...'}`"
        )
    elif current_state == "done":
        st.success("✅ Last pipeline run finished successfully")
    elif current_state.startswith("done:"):
        st.success(f"✅ Last pipeline run: {current_state}")
    elif current_state.startswith("failed:"):
        st.error(f"❌ Last pipeline run failed at: {current_state}")
    elif current_state:
        st.info(f"Last state: `{current_state}`")

    # Show live log (last 80 lines)
    if log_file.exists():
        log_text = log_file.read_text()
        last_lines = "\n".join(log_text.splitlines()[-80:])
        if last_lines.strip():
            with st.expander(
                "📋 Pipeline log (last 80 lines)",
                expanded=is_running,
            ):
                st.code(last_lines, language=None)

    # Manual refresh button — required because we no longer auto-refresh
    if is_running:
        st.caption(
            "Click 'Refresh status' to fetch the latest log lines. "
            "(We don't auto-refresh because Streamlit reruns would kill "
            "any subprocess in the foreground — but this background one is safe.)"
        )
        if st.button("🔄 Refresh status", key="refresh_pipeline_status"):
            st.rerun()

    # --- View Current Leads ---
    st.divider()
    st.subheader("Current Leads")
    preview_candidates = [
        ("leads_new_sms_sent.csv",  "Latest run - after SMS"),
        ("leads_new_traced.csv",    "Latest run - after Skip Trace (about to be texted)"),
        ("leads_new_equity.csv",    "Latest run - after Equity (ready for Skip Trace)"),
        ("leads_new.csv",           "Latest run - parsed only (ready for Skip Trace)"),
        ("leads.csv",               "All leads ever parsed (accumulated)"),
    ]
    shown = False
    for csv_name, label in preview_candidates:
        if not _data(csv_name).exists():
            continue
        # Render in Google Sheet format: 1 owner row + relative rows beneath,
        # with Call Status populated from sms_history.csv so you can see at
        # a glance who's been texted. Matches what ends up in the sheet.
        try:
            import csv as _csv, re as _re
            from sheets_exporter import records_to_sheet_rows, HEADER_ROW

            with open(_data(csv_name), encoding="utf-8") as f:
                records = list(_csv.DictReader(f))

            # Build phone -> sent_at lookup from sms_history for Call Status
            def _norm(p):
                d = _re.sub(r"\D", "", p or "")
                return d[-10:] if len(d) >= 10 else d
            sent_lookup = {}
            if _data("sms_history.csv").exists():
                with open(_data("sms_history.csv"), encoding="utf-8") as hf:
                    for hr in _csv.DictReader(hf):
                        np = _norm(hr.get("phone_number", ""))
                        if np:
                            sent_lookup[np] = hr.get("sent_at", "")[:10]

            rows = records_to_sheet_rows(records)
            phone_idx = HEADER_ROW.index("Phone Number")
            cs_idx = HEADER_ROW.index("Call Status")
            for row in rows:
                ph = row[phone_idx] if phone_idx < len(row) else ""
                if ph and not row[cs_idx]:
                    np = _norm(ph)
                    if np in sent_lookup:
                        row[cs_idx] = f"Texted {sent_lookup[np]}"

            # HEADER_ROW intentionally has two "Name" columns (owner vs.
            # relative) to mirror the Google Sheet layout. Streamlit
            # refuses duplicate column names, so disambiguate the second
            # occurrence ("Name" -> "Relative Name") for display only.
            seen = {}
            display_headers = []
            for h in HEADER_ROW[: len(rows[0]) if rows else len(HEADER_ROW)]:
                if h in seen:
                    seen[h] += 1
                    display_headers.append(
                        "Relative Name" if h == "Name" else f"{h} ({seen[h]})"
                    )
                else:
                    seen[h] = 1
                    display_headers.append(h)
            df = pd.DataFrame(rows, columns=display_headers)
            owners_count = len(records)
            st.write(f"**{csv_name}** - {label} - {owners_count} lead(s), {len(rows)} total rows (owner + relatives)")
            st.dataframe(df, width='stretch', height=400)
            # Download keeps the raw internal format in case anything downstream
            # needs the full pipeline columns (phone_1, rel_1_name, etc).
            raw_df = pd.read_csv(csv_name)
            st.download_button(
                f"Download {csv_name} (raw)",
                data=raw_df.to_csv(index=False).encode("utf-8"),
                file_name=csv_name,
                mime="text/csv",
            )
            st.download_button(
                f"Download sheet-style view",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=csv_name.replace(".csv", "_sheet_view.csv"),
                mime="text/csv",
                key=f"dl_sheet_{csv_name}",
            )
            shown = True
            break
        except Exception as e:
            # Fall back to raw CSV view if the sheet-style conversion breaks
            st.warning(f"Sheet-style render failed ({e}) — showing raw CSV.")
            df = pd.read_csv(csv_name)
            st.write(f"**{csv_name}** - {label} - {len(df)} row(s)")
            st.dataframe(df, width='stretch', height=400)
            st.download_button(
                f"Download {csv_name}",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=csv_name,
                mime="text/csv",
            )
            shown = True
            break
    if not shown:
        st.info("No leads yet. Run the pipeline to get started.")


# ===========================================================================
# DISPOSITIONS TAB
# ===========================================================================

with tab_dispo:
    st.header("Buyer Contact Lookup")
    st.caption("Skip trace buyer names from the Dispositions sheet")

    # Tab selection
    metro = st.selectbox(
        "Metro area",
        ["Austin Metro", "Houston Metro", "San Antonio Metro", "Dallas Metro"],
        key="metro_tab",
    )

    col_left, col_right = st.columns([1, 2])

    with col_left:
        trace_limit_d = st.number_input("Max rows to trace", min_value=0, value=0, step=5,
                                         key="dispo_limit", help="0 = all pending rows (per tab)")

        retrace_all = st.checkbox(
            "Retrace every row (overwrites existing data)",
            value=False,
            key="dispo_retrace_all",
            help="When OFF (default): only processes rows where Phones is empty. "
                 "When ON: re-runs on EVERY row with a name, overwriting existing "
                 "Phones / Mailing / Email cells with fresh Skip Genie results. "
                 "Useful after upgrading the search logic.",
        )

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            trace_one = st.button("Trace Selected", type="primary", key="trace_buyers")
        with btn_col2:
            trace_all = st.button("Trace All Tabs", type="secondary", key="trace_all")

        if trace_one or trace_all:
            if trace_all:
                cmd = [PYTHON, _script("buyer_tracer.py"), "--all-tabs"]
                label = "all tabs"
            else:
                cmd = [PYTHON, _script("buyer_tracer.py"), "--tab", metro]
                label = metro
            if trace_limit_d > 0:
                cmd.extend(["--limit", str(trace_limit_d)])
            if retrace_all:
                cmd.append("--retrace-all")
                st.caption(f":warning: Retrace-all is ON. Existing Phones/Mailing/Email in {label} will be OVERWRITTEN.")
            # Skip trace can take a while with lots of rows + relatives.
            # Give it 2 hours since retrace-all touches everyone.
            with st.spinner(f"Tracing {label} buyers... this takes ~30s per name"):
                log_area = st.empty()
                out = run_script(cmd, log_area, timeout=2 * 60 * 60)
            if out.returncode == 0:
                st.success(f"Done — check the Dispositions sheet")
            else:
                st.error("Trace failed — check logs above")

    with col_right:
        # Show current state of the selected tab
        try:
            from sheets_exporter import _get_client
            from buyer_tracer import DISPOSITIONS_SHEET_ID
            client = _get_client()
            sheet = client.open_by_key(DISPOSITIONS_SHEET_ID)
            ws = sheet.worksheet(metro)
            data = ws.get_all_values()
            if len(data) > 1:
                df = pd.DataFrame(data[1:], columns=data[0])
                # Filter out empty rows
                df = df[df.apply(lambda r: any(str(v).strip() for v in r), axis=1)]
                if not df.empty:
                    total = len(df)
                    has_phone = df["Phones"].astype(str).str.strip().ne("").sum()
                    st.metric("Total buyers", total)
                    st.metric("With phones", f"{has_phone}/{total}")
                    st.dataframe(df, width='stretch', height=400)
                else:
                    st.info(f"No data in {metro} yet. Add buyer names to the sheet first.")
            else:
                st.info(f"No data in {metro} yet. Add buyer names to the sheet first.")
        except Exception as e:
            st.warning(f"Could not load sheet: {e}")

    st.divider()
    # Build the sheet link from the env-driven ID so changing the sheet only
    # requires updating .env (DISPOSITIONS_SHEET_ID).
    from buyer_tracer import DISPOSITIONS_SHEET_ID as _dispo_id
    st.caption(
        f"Dispositions sheet: "
        f"[Open in Google Sheets](https://docs.google.com/spreadsheets/d/{_dispo_id})"
    )


# ===========================================================================
# HEIRSHIP AFFIDAVIT TAB
# ===========================================================================

with tab_aoh:
    st.header("Affidavit of Heirship Generator")
    st.caption("Fill in the questionnaire fields below, then generate the PDF.")

    # --- Session state for dynamic lists ---
    for key, default in [
        ("aoh_num_children", 1),
        ("aoh_num_deceased_children", 0),
        ("aoh_num_grandchildren", 0),
        ("aoh_num_marriages", 1),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # -----------------------------------------------------------------------
    # Section 1: Decedent Info
    # -----------------------------------------------------------------------
    st.subheader("Decedent Information")
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        decedent_full_name = st.text_input(
            "Decedent Full Name", key="aoh_dec_name",
            placeholder="e.g. Ethel Mae Hafernik Hummell",
        )
        decedent_aka = st.text_input(
            "Decedent AKA (blank if none)", key="aoh_dec_aka",
            placeholder="e.g. Ethel M. Hummell",
        )
        decedent_dob = st.text_input(
            "Decedent Date of Birth", key="aoh_dec_dob", placeholder="MM/DD/YYYY",
        )
        decedent_pronoun = st.selectbox(
            "Decedent Pronoun (for 'his/her home' phrasing)",
            ["she", "he"], key="aoh_dec_pronoun",
        )
    with col_d2:
        death_date = st.text_input(
            "Date of Death", key="aoh_death_date",
            placeholder="e.g. October 6, 2010",
        )
        death_city = st.text_input("City of Death", key="aoh_death_city", value="Austin")
        death_county = st.text_input(
            "County of Death", key="aoh_death_county", value="Travis",
            help="Leave blank if death occurred outside Texas.",
        )
        death_state = st.text_input(
            "State of Death", key="aoh_death_state", value="Texas",
            help="Override if decedent died outside Texas.",
        )

    st.caption("Decedent's Residential Address at Time of Death")
    col_r1, col_r2 = st.columns(2)
    with col_r1:
        res_address = st.text_input(
            "Street Address", key="aoh_res_addr",
            placeholder="e.g. 8710 Colonial Dr.",
        )
        res_city = st.text_input("City", key="aoh_res_city", value="Austin")
        res_state = st.text_input("State", key="aoh_res_state", value="Texas")
    with col_r2:
        res_zip = st.text_input("Zip", key="aoh_res_zip")
        res_county = st.text_input("County", key="aoh_res_county", value="Travis")

    st.divider()

    # -----------------------------------------------------------------------
    # Section 2: Affiant
    # -----------------------------------------------------------------------
    st.subheader("Affiant (Family Member or Person Familiar with History)")
    col_a1, col_a2 = st.columns(2)
    with col_a1:
        aff_name = st.text_input(
            "Affiant Name", key="aoh_aff_name", placeholder="e.g. Norman Hummell",
        )
        aff_aka = st.text_input(
            "Affiant AKA (blank if none)", key="aoh_aff_aka",
            placeholder="e.g. Norman S. Hummell",
        )
        st.caption(
            "Intro will read: \"I [verb] [article] [relationship] of the Decedent "
            "and knew Decedent for [duration].\""
        )
        col_av1, col_av2 = st.columns(2)
        with col_av1:
            aff_verb = st.selectbox(
                "Verb", ["was", "am"], key="aoh_aff_verb",
                help="'am' for current relatives still living (e.g. son), "
                     "'was' for spouses or non-family",
            )
        with col_av2:
            aff_article = st.selectbox(
                "Article", ["the", "a"], key="aoh_aff_article",
                help="'the' for family (the son), 'a' for friends (a friend)",
            )
        aff_relationship = st.text_input(
            "Relationship", key="aoh_aff_rel",
            placeholder="e.g. son, husband, half-brother, family friend",
        )
        aff_duration = st.text_input(
            "Duration phrase", key="aoh_aff_duration",
            placeholder="e.g. 'more than forty-eight (48) years' OR 'my entire life until her death'",
        )
    with col_a2:
        aff_address = st.text_input(
            "Street Address", key="aoh_aff_addr",
            placeholder="e.g. 8710 Colonial Dr.",
        )
        aff_city = st.text_input("City", key="aoh_aff_city", value="Austin")
        aff_county = st.text_input("County", key="aoh_aff_county", value="Travis")
        aff_state = st.text_input("State", key="aoh_aff_state", value="Texas")
        aff_zip = st.text_input("Zip", key="aoh_aff_zip")

    st.divider()

    # -----------------------------------------------------------------------
    # Section 3 & 4: Witnesses
    # -----------------------------------------------------------------------
    st.subheader("Witnesses (2 — not related to decedent, knew decedent 10+ years)")
    col_w1, col_w2 = st.columns(2)

    def _witness_inputs(prefix: str, label: str):
        st.caption(label)
        name = st.text_input("Name", key=f"aoh_{prefix}_name")
        address = st.text_input("Street Address", key=f"aoh_{prefix}_addr")
        city = st.text_input("City", key=f"aoh_{prefix}_city")
        county = st.text_input("County", key=f"aoh_{prefix}_county")
        state = st.text_input("State", key=f"aoh_{prefix}_state", value="Texas")
        zip_ = st.text_input("Zip", key=f"aoh_{prefix}_zip")
        c1, c2 = st.columns(2)
        with c1:
            verb = st.selectbox("Verb", ["was", "am"], key=f"aoh_{prefix}_verb")
        with c2:
            article = st.selectbox("Article", ["a", "the"], key=f"aoh_{prefix}_article")
        relationship = st.text_input(
            "Relationship", key=f"aoh_{prefix}_rel",
            placeholder="e.g. friend, neighbor, family friend",
        )
        duration = st.text_input(
            "Duration phrase", key=f"aoh_{prefix}_duration",
            placeholder="e.g. 'more than fifteen (15) years'",
        )
        return {
            "name": name, "address": address, "city": city, "county": county,
            "state": state, "zip": zip_, "verb": verb, "article": article,
            "relationship": relationship, "duration": duration,
        }

    with col_w1:
        w1_data = _witness_inputs("w1", "Witness 1")
    with col_w2:
        w2_data = _witness_inputs("w2", "Witness 2")

    w1_name = w1_data["name"]; w2_name = w2_data["name"]

    st.divider()

    # -----------------------------------------------------------------------
    # Section 5: Marital History
    # -----------------------------------------------------------------------
    st.subheader("Marital History")
    never_married = st.checkbox(
        "Decedent was never married", value=False, key="aoh_never_married",
    )
    marriages_data = []
    if not never_married:
        num_marriages = st.number_input(
            "Number of marriages", min_value=0, max_value=5, value=1,
            key="aoh_num_marriages_input",
        )
        for i in range(num_marriages):
            st.markdown(f"**Marriage {i+1}**")
            col_m1, col_m2, col_m3 = st.columns(3)
            with col_m1:
                m_date = st.text_input(
                    "Date Married", key=f"aoh_m{i}_date",
                    placeholder="e.g. May 20, 1962",
                )
            with col_m2:
                m_spouse = st.text_input("Spouse Name", key=f"aoh_m{i}_spouse")
            with col_m3:
                m_spouse_aka = st.text_input(
                    "Spouse AKA", key=f"aoh_m{i}_spouse_aka",
                    placeholder="blank if none",
                )
            col_e1, col_e2, col_e3 = st.columns(3)
            with col_e1:
                m_ended = st.selectbox(
                    "How marriage ended",
                    ["Decedent's death (final spouse)",
                     "Spouse's death", "Divorce"],
                    key=f"aoh_m{i}_ended",
                )
            with col_e2:
                m_end_date = st.text_input(
                    "End Date (divorce/spouse-death)", key=f"aoh_m{i}_end_date",
                    placeholder="blank if ended by Decedent's death",
                )
            with col_e3:
                m_spouse_pronoun = st.selectbox(
                    "Spouse Pronoun (for 'his/her death')",
                    ["his", "her"], key=f"aoh_m{i}_spouse_pron",
                )
            ended_map = {
                "Decedent's death (final spouse)": "death_decedent",
                "Spouse's death": "death_spouse",
                "Divorce": "divorce",
            }
            marriages_data.append({
                "date": m_date,
                "spouse_name": m_spouse,
                "spouse_aka": m_spouse_aka,
                "ended_by": ended_map[m_ended],
                "end_date": m_end_date,
                "spouse_pronoun": m_spouse_pronoun,
            })

    st.divider()

    # -----------------------------------------------------------------------
    # Section 6: Children — grouped by other parent
    # -----------------------------------------------------------------------
    st.subheader("Children")
    st.caption(
        "Group children by their OTHER parent. Each group becomes a separate "
        "numbered fact in the affidavit."
    )
    num_child_groups = st.number_input(
        "Number of child groups (one per other-parent)",
        min_value=0, max_value=10, value=1, key="aoh_num_child_groups",
    )
    child_groups_data = []
    for g in range(num_child_groups):
        st.markdown(f"**Group {g+1}**")
        col_g1, col_g2, col_g3 = st.columns(3)
        with col_g1:
            op_name = st.text_input(
                "Other Parent Name", key=f"aoh_cg{g}_op",
                placeholder="e.g. Norman Hummell",
            )
        with col_g2:
            op_aka = st.text_input(
                "Other Parent AKA", key=f"aoh_cg{g}_op_aka",
                placeholder="blank if none",
            )
        with col_g3:
            rel_type = st.selectbox(
                "Relationship",
                ["marriage to", "relationship with",
                 "relationship with and marriage to"],
                key=f"aoh_cg{g}_rel_type",
                help="Use 'relationship with and marriage to' when kids "
                     "were born both before and after the marriage.",
            )
        num_kids = st.number_input(
            "Number of children in this group", min_value=0, max_value=15,
            value=1, key=f"aoh_cg{g}_num",
        )
        kids = []
        for i in range(num_kids):
            col_k1, col_k2 = st.columns(2)
            with col_k1:
                k_name = st.text_input(f"Child {i+1} Name", key=f"aoh_cg{g}_k{i}_name")
            with col_k2:
                k_dob = st.text_input(
                    f"DOB", key=f"aoh_cg{g}_k{i}_dob",
                    placeholder="e.g. August 31, 1965",
                )
            kids.append({"name": k_name, "dob": k_dob})
        rel_type_map = {
            "marriage to": "marriage",
            "relationship with": "relationship",
            "relationship with and marriage to": "relationship_and_marriage",
        }
        child_groups_data.append({
            "other_parent": op_name,
            "other_parent_aka": op_aka,
            "relationship_type": rel_type_map.get(rel_type, "marriage"),
            "children": kids,
        })

    st.divider()

    # -----------------------------------------------------------------------
    # Section 7: Step-children
    # -----------------------------------------------------------------------
    st.subheader("Step-Children (children Decedent took in and raised)")
    num_step_groups = st.number_input(
        "Number of step-child groups (one per spouse who had prior kids)",
        min_value=0, max_value=5, value=0, key="aoh_num_step_groups",
    )
    step_groups_data = []
    for g in range(num_step_groups):
        st.markdown(f"**Step-Group {g+1}**")
        col_s1, col_s2, col_s3 = st.columns(3)
        with col_s1:
            sp_name = st.text_input(
                "Spouse (parent of step-kids)", key=f"aoh_sg{g}_sp",
                placeholder="e.g. Howard Loving",
            )
        with col_s2:
            prior_type = st.selectbox(
                "Prior Relationship",
                ["marriage to", "relationship with"],
                key=f"aoh_sg{g}_prior_type",
            )
        with col_s3:
            prior_parent = st.text_input(
                "Prior Other Parent (the step-kids' mother/father)",
                key=f"aoh_sg{g}_prior_parent",
            )
        num_skids = st.number_input(
            "Number of step-children", min_value=1, max_value=15,
            value=1, key=f"aoh_sg{g}_num",
        )
        skids = []
        for i in range(num_skids):
            col_sk1, col_sk2 = st.columns(2)
            with col_sk1:
                sk_name = st.text_input(
                    f"Step-Child {i+1} Name", key=f"aoh_sg{g}_k{i}_name",
                )
            with col_sk2:
                sk_dob = st.text_input(
                    "DOB", key=f"aoh_sg{g}_k{i}_dob",
                    placeholder="MM/DD/YYYY",
                )
            skids.append({"name": sk_name, "dob": sk_dob})
        step_groups_data.append({
            "spouse_name": sp_name,
            "prior_relationship_type": (
                "marriage" if prior_type == "marriage to" else "relationship"
            ),
            "prior_other_parent": prior_parent,
            "children": skids,
        })

    st.divider()

    # -----------------------------------------------------------------------
    # Section 8: Subsequent deaths of children
    # -----------------------------------------------------------------------
    st.subheader("Subsequent Deaths (children who died AFTER decedent)")
    st.caption(
        "Adds: 'Subsequent to the death of Decedent, NAME died on DATE. "
        "An Affidavit of Heirship for NAME is filed in the Official Public "
        "Records of COUNTY, Texas, in conjunction herewith.'"
    )
    num_subq = st.number_input(
        "Number of subsequent deaths", min_value=0, max_value=10, value=0,
        key="aoh_num_subq",
    )
    subsequent_data = []
    for i in range(num_subq):
        col_q1, col_q2, col_q3, col_q4 = st.columns(4)
        with col_q1:
            sq_name = st.text_input(f"Name", key=f"aoh_sq{i}_name")
        with col_q2:
            sq_rel = st.text_input(
                f"Relationship", key=f"aoh_sq{i}_rel",
                placeholder="daughter / son (optional)",
                help="If provided, prefix becomes \"Decedent's daughter NAME\"",
            )
        with col_q3:
            sq_date = st.text_input(
                f"Date of Death", key=f"aoh_sq{i}_date",
                placeholder="e.g. October 19, 2023",
            )
        with col_q4:
            sq_county = st.text_input(
                f"AOH Filed in County", key=f"aoh_sq{i}_county",
                value="Travis",
            )
        subsequent_data.append({
            "name": sq_name, "relationship": sq_rel,
            "death_date": sq_date, "aoh_county": sq_county,
        })

    st.divider()

    # -----------------------------------------------------------------------
    # Section 8.5: Family Facts (free-form numbered facts for survived-by /
    # parents / siblings — used when decedent has no children, or other
    # extended-family statements need to appear in the affidavit)
    # -----------------------------------------------------------------------
    st.subheader("Additional Family Facts (optional)")
    st.caption(
        "Each non-empty line becomes its own numbered fact in the affidavit. "
        "Use for things like 'Decedent was survived by her mother...' or "
        "'Decedent's father, X, died in 2010.' This is most often used when "
        "the decedent had no children."
    )
    family_facts_text = st.text_area(
        "Family facts (one per line)",
        key="aoh_family_facts",
        height=100,
        placeholder=(
            "Decedent's father, Larry Wray Stucker, died in 2010. Decedent's "
            "mother, Patricia Jenks a/k/a Patricia L. Jenks, died on February "
            "27, 2022. Decedent was survived by his siblings, Cynthia Jones, "
            "Gary Stucker, and Judith Dutelle."
        ),
    )
    family_facts_data = [
        line.strip() for line in (family_facts_text or "").splitlines()
        if line.strip()
    ]

    st.divider()

    # -----------------------------------------------------------------------
    # Section 9: Property
    # -----------------------------------------------------------------------
    st.subheader("Property (Optional)")
    use_exhibit_a = st.checkbox(
        "Use 'Exhibit A' reference instead of inline description",
        value=False, key="aoh_use_exhibit_a",
    )
    col_p1, col_p2 = st.columns([1, 3])
    with col_p1:
        property_county = st.text_input(
            "Property County", key="aoh_prop_county", value="Travis",
        )
    with col_p2:
        property_description = st.text_area(
            "Property Legal Description (leave blank to skip)",
            key="aoh_prop_desc", height=80,
            placeholder="e.g. Lot 2, Block L, QUAIL CREEK WEST, SECTION FOUR, "
                        "a subdivision in Travis County, Texas, according to "
                        "the map or plat thereof, recorded in Volume 54, "
                        "Page 14, Plat Records, Travis County, Texas.",
        )

    st.divider()

    # -----------------------------------------------------------------------
    # Section 10: Estate value + boilerplate facts
    # -----------------------------------------------------------------------
    st.subheader("Estate Information")
    col_e1, col_e2, col_e3 = st.columns(3)
    with col_e1:
        estate_value = st.text_input(
            "Estate Value (less than)", key="aoh_estate_value",
            value="$13,990,000.00",
            help="Federal estate tax exemption — current is around $13.99M",
        )
    with col_e2:
        died_intestate = st.checkbox(
            "Decedent died intestate (no will)",
            value=True, key="aoh_died_intestate",
        )
    with col_e3:
        no_unpaid_debts = st.checkbox(
            "No unpaid debts / inheritance taxes",
            value=True, key="aoh_no_unpaid_debts",
        )

    st.divider()

    # -----------------------------------------------------------------------
    # Section 11: Signing details (optional - blank for hand-fill if omitted)
    # -----------------------------------------------------------------------
    st.subheader("Signing Date (optional — leave blank for hand-fill)")
    col_s1, col_s2, col_s3 = st.columns(3)
    with col_s1:
        signing_day = st.text_input(
            "Day", key="aoh_signing_day", placeholder="e.g. 17",
            help="Will be formatted as ordinal (17th)",
        )
    with col_s2:
        signing_month = st.text_input(
            "Month", key="aoh_signing_month", placeholder="e.g. April",
        )
    with col_s3:
        signing_year = st.text_input(
            "Year", key="aoh_signing_year", placeholder="e.g. 2026",
        )

    st.divider()

    # -----------------------------------------------------------------------
    # Questionnaire-only fields (still useful for the questionnaire PDF)
    # -----------------------------------------------------------------------
    with st.expander("Questionnaire-only fields (Will / Debts / Taxes)"):
        had_will = st.text_input(
            "Did decedent have a Will? Was it probated?",
            key="aoh_will", placeholder="e.g. No",
        )
        unpaid_debts_text = st.text_input(
            "Any debts when decedent died?",
            key="aoh_debts", placeholder="e.g. No",
        )
        unpaid_taxes = st.selectbox(
            "Unpaid estate or inheritance taxes?",
            ["No", "Yes"], key="aoh_taxes",
        )

    st.divider()

    # -----------------------------------------------------------------------
    # Build data dict (shared by both buttons)
    # -----------------------------------------------------------------------
    def _build_aoh_data() -> dict | None:
        missing = []
        if not decedent_full_name: missing.append("Decedent Name")
        if not death_date: missing.append("Date of Death")
        if not aff_name: missing.append("Affiant Name")
        if not w1_name: missing.append("Witness 1 Name")
        if not w2_name: missing.append("Witness 2 Name")
        if missing:
            st.error(f"Missing required fields: {', '.join(missing)}")
            return None

        # Build legacy "children" / "deceased_children" lists for the
        # questionnaire generator (which still uses the flat structure).
        flat_children = []
        for grp in child_groups_data:
            for k in grp.get("children", []):
                flat_children.append({
                    "name": k.get("name", ""),
                    "dob": k.get("dob", ""),
                    "other_parent": grp.get("other_parent", ""),
                    "relationship": "Biological",
                })
        flat_deceased = [
            {"name": d["name"], "death_date": d["death_date"]}
            for d in subsequent_data
        ]
        # Build legacy marriages/divorces/remarriages for questionnaire.
        legacy_marriages = []
        divorce_dates_legacy = []
        legacy_remarriages = []
        for i, m in enumerate(marriages_data):
            entry = {
                "date": m["date"],
                "spouse_name": m["spouse_name"],
                "spouse_aka": m.get("spouse_aka", ""),
            }
            if i == 0:
                legacy_marriages.append(entry)
            else:
                legacy_remarriages.append(entry)
            if m.get("ended_by") == "divorce" and m.get("end_date"):
                divorce_dates_legacy.append(m["end_date"])

        return {
            # Decedent
            "decedent_full_name": decedent_full_name,
            "decedent_aka": decedent_aka,
            "decedent_dob": decedent_dob,
            "decedent_pronoun": decedent_pronoun,
            "death_date": death_date,
            "death_city": death_city,
            "death_county": death_county,
            "death_state": death_state,
            "residence_address": res_address,
            "residence_city": res_city,
            "residence_state": res_state,
            "residence_zip": res_zip,
            "residence_county": res_county,
            # Affiant
            "affiant_name": aff_name,
            "affiant_aka": aff_aka,
            "affiant_address": aff_address,
            "affiant_city": aff_city,
            "affiant_county": aff_county,
            "affiant_state": aff_state,
            "affiant_zip": aff_zip,
            "affiant_verb": aff_verb,
            "affiant_article": aff_article,
            "affiant_relationship": aff_relationship,
            "affiant_duration": aff_duration,
            # Witnesses (full + flat for back-compat)
            "w1_name": w1_data["name"],
            "w1_address": w1_data["address"],
            "w1_city": w1_data["city"],
            "w1_county": w1_data["county"],
            "w1_state": w1_data["state"],
            "w1_zip": w1_data["zip"],
            "w1_verb": w1_data["verb"],
            "w1_article": w1_data["article"],
            "w1_relationship": w1_data["relationship"],
            "w1_duration": w1_data["duration"],
            "w2_name": w2_data["name"],
            "w2_address": w2_data["address"],
            "w2_city": w2_data["city"],
            "w2_county": w2_data["county"],
            "w2_state": w2_data["state"],
            "w2_zip": w2_data["zip"],
            "w2_verb": w2_data["verb"],
            "w2_article": w2_data["article"],
            "w2_relationship": w2_data["relationship"],
            "w2_duration": w2_data["duration"],
            # Marital history (new + legacy)
            "never_married": never_married,
            "marriages": marriages_data if not never_married else [],
            "divorced": any(
                m.get("ended_by") == "divorce" for m in marriages_data
            ),
            "divorce_dates": divorce_dates_legacy,
            "remarriages": legacy_remarriages,
            # Children — new grouped + legacy flat for questionnaire
            "child_groups": child_groups_data,
            "children": flat_children,
            "step_child_groups": step_groups_data,
            "subsequent_deaths": subsequent_data,
            "family_facts": family_facts_data,
            "deceased_children": flat_deceased,
            "grandchildren": [],
            # Other facts
            "died_intestate": died_intestate,
            "no_unpaid_debts": no_unpaid_debts,
            "estate_value": estate_value,
            "property_description": property_description,
            "property_county": property_county,
            "use_exhibit_a": use_exhibit_a,
            # Signing — the affidavit is recorded where the property sits, so
            # the header "COUNTY OF ___" follows the property (then residence)
            # county, not the county of death.
            "signing_county": property_county or res_county or death_county,
            "signing_day": signing_day,
            "signing_month": signing_month,
            "signing_year": signing_year,
            # Questionnaire-only
            "had_will": had_will,
            "unpaid_debts": unpaid_debts_text,
            "unpaid_taxes": unpaid_taxes == "Yes",
        }

    # -----------------------------------------------------------------------
    # Pre-generation validation (advisory) — catches the data-entry mistakes
    # that slip past the required-field check: placeholder witnesses, a
    # decedent pronoun that doesn't match the name's usual gender, empty
    # duration phrases, and near-duplicate name spellings (likely typos).
    # -----------------------------------------------------------------------
    def _aoh_warnings(data: dict) -> list:
        warnings: list = []

        placeholder_names = {
            "john doe", "jane doe", "jon doe", "j. doe", "j doe",
            "first last", "name name", "test test",
        }
        placeholder_addr = ("1234 address", "123 main", "address ln")

        # Placeholder / incomplete affiant + witnesses
        for label, nkey, akey, zkey in (
            ("Affiant", "affiant_name", "affiant_address", "affiant_zip"),
            ("Witness 1", "w1_name", "w1_address", "w1_zip"),
            ("Witness 2", "w2_name", "w2_address", "w2_zip"),
        ):
            name = (data.get(nkey) or "").strip()
            addr = (data.get(akey) or "").strip()
            zc = (data.get(zkey) or "").strip()
            if name.lower() in placeholder_names:
                warnings.append(
                    f"{label} name looks like a placeholder (“{name}”) — "
                    f"replace it with the real person before filing."
                )
            if any(frag in addr.lower() for frag in placeholder_addr):
                warnings.append(
                    f"{label} address looks like a placeholder (“{addr}”)."
                )
            elif not addr or not zc:
                warnings.append(
                    f"{label} is missing a complete address (street and/or zip)."
                )

        # Empty duration phrase -> sentence reads "knew Decedent for ."
        for label, dkey in (
            ("Affiant", "affiant_duration"),
            ("Witness 1", "w1_duration"),
            ("Witness 2", "w2_duration"),
        ):
            if not (data.get(dkey) or "").strip():
                warnings.append(
                    f"{label} “duration” phrase is empty — the sentence "
                    f"will read “knew Decedent for .”"
                )

        # Pronoun vs. first-name gender (advisory heuristic)
        gender_by_name = {
            "salvador": "he", "jose": "he", "juan": "he", "ramero": "he",
            "ramiro": "he", "carlos": "he", "luis": "he", "miguel": "he",
            "francisco": "he", "manuel": "he", "pedro": "he", "antonio": "he",
            "roberto": "he", "robert": "he", "john": "he", "james": "he",
            "william": "he", "richard": "he", "david": "he", "norman": "he",
            "howard": "he", "charles": "he", "george": "he", "joseph": "he",
            "thomas": "he", "michael": "he",
            "maria": "she", "carmen": "she", "rosa": "she", "irene": "she",
            "aurora": "she", "teresa": "she", "mary": "she", "alice": "she",
            "elena": "she", "guadalupe": "she", "ethel": "she", "margaret": "she",
            "dorothy": "she", "helen": "she", "patricia": "she", "linda": "she",
            "barbara": "she", "grace": "she", "gloria": "she", "juanita": "she",
        }
        pronoun = (data.get("decedent_pronoun") or "").strip().lower()
        tokens = (data.get("decedent_full_name") or "").strip().split()
        first = tokens[0].lower() if tokens else ""
        guessed = gender_by_name.get(first)
        if guessed and pronoun and guessed != pronoun:
            warnings.append(
                f"Decedent pronoun is set to “{pronoun}”, but the first "
                f"name “{first.title()}” is usually "
                f"{'male' if guessed == 'he' else 'female'}. Double-check the "
                f"pronoun — it drives the his/her wording."
            )

        # Near-duplicate name spellings (likely typos), e.g. Villareal/Villarreal
        def _lev(a: str, b: str) -> int:
            prev = list(range(len(b) + 1))
            for i, ca in enumerate(a, 1):
                cur = [i]
                for j, cb in enumerate(b, 1):
                    cur.append(min(
                        prev[j] + 1, cur[j - 1] + 1,
                        prev[j - 1] + (ca != cb),
                    ))
                prev = cur
            return prev[-1]

        names = []
        for key in ("decedent_full_name", "affiant_name", "w1_name", "w2_name"):
            if data.get(key):
                names.append(data[key])
        for m in data.get("marriages", []) or []:
            if m.get("spouse_name"):
                names.append(m["spouse_name"])
        for grp in data.get("child_groups", []) or []:
            if grp.get("other_parent"):
                names.append(grp["other_parent"])
            for k in grp.get("children", []) or []:
                if k.get("name"):
                    names.append(k["name"])
        for d in data.get("subsequent_deaths", []) or []:
            if d.get("name"):
                names.append(d["name"])

        token_orig = {}
        for n in names:
            for tok in n.strip().split():
                t = tok.strip(".,").strip()
                if len(t) >= 6:
                    token_orig.setdefault(t.lower(), t)
        toks = sorted(token_orig)
        flagged = set()
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                a, b = toks[i], toks[j]
                if abs(len(a) - len(b)) <= 1 and _lev(a, b) == 1:
                    if (a, b) in flagged:
                        continue
                    flagged.add((a, b))
                    warnings.append(
                        f"Possible name typo — “{token_orig[a]}” vs "
                        f"“{token_orig[b]}” differ by one letter. "
                        f"Verify the spelling."
                    )
        return warnings

    # -----------------------------------------------------------------------
    # Generate buttons
    # -----------------------------------------------------------------------
    btn_col1, btn_col2 = st.columns(2)

    with btn_col1:
        if st.button("Generate Affidavit PDF", type="primary", key="aoh_generate"):
            data = _build_aoh_data()
            if data:
                for _w in _aoh_warnings(data):
                    st.warning(_w)
                from aoh_generator import generate_aoh_pdf
                try:
                    pdf_bytes = generate_aoh_pdf(data)
                    safe_name = decedent_full_name.replace(" ", "_").replace("/", "-")
                    st.success("Affidavit PDF generated!")
                    st.download_button(
                        "Download Affidavit PDF",
                        data=pdf_bytes,
                        file_name=f"AOH_{safe_name}.pdf",
                        mime="application/pdf",
                        type="primary",
                    )
                except Exception as e:
                    st.error(f"Error generating PDF: {e}")

    with btn_col2:
        if st.button("Generate Questionnaire PDF", type="secondary", key="aoh_questionnaire"):
            data = _build_aoh_data()
            if data:
                for _w in _aoh_warnings(data):
                    st.warning(_w)
                from aoh_generator import generate_questionnaire_pdf
                try:
                    pdf_bytes = generate_questionnaire_pdf(data)
                    safe_name = decedent_full_name.replace(" ", "_").replace("/", "-")
                    st.success("Questionnaire PDF generated!")
                    st.download_button(
                        "Download Questionnaire PDF",
                        data=pdf_bytes,
                        file_name=f"AOH_Questionnaire_{safe_name}.pdf",
                        mime="application/pdf",
                        type="secondary",
                    )
                except Exception as e:
                    st.error(f"Error generating PDF: {e}")


# ===========================================================================
# RESIMPLI SYNC TAB
# ===========================================================================

with tab_resimpli:
    st.header("REsimpli Lead Sync")
    st.caption(
        "REsimpli has no public API for pulling leads, so this is a manual "
        "upload workflow."
    )

    # ---------------- How-to instructions ----------------------------------
    with st.expander("📋 How to get the CSV from REsimpli", expanded=True):
        st.markdown(
            """
            **Step-by-step:**

            1. Log in to **REsimpli** and go to the **Leads** page
            2. At the top of the leads table, click the **"Select All"** checkbox
            3. Click **"Export"** (or "Export Files") in the toolbar
            4. Wait for the CSV to download to your computer
            5. **Drag the downloaded CSV into the upload box below** — or click
               *Browse files* and pick it
            6. Once it uploads, you'll see lead stats, the manager performance
               dashboard, and an option to push to Google Sheets

            > **Tip:** Filter REsimpli to "Under Contract" or "Active" leads
            > before exporting if you only want to sync that subset.
            """
        )

    # ---------------- Upload ------------------------------------------------
    uploaded = st.file_uploader(
        "Upload REsimpli CSV export",
        type=["csv"],
        key="resimpli_upload",
        help="Drop the CSV file from REsimpli's export feature",
    )

    if not uploaded:
        st.info(
            "No CSV uploaded yet. Once you upload, you'll see lead stats, "
            "cross-references with your foreclosure pipeline, and an option "
            "to push to Google Sheets."
        )
    else:
        from resimpli_importer import (
            parse_resimpli_csv,
            cross_reference_with_pipeline,
            summarize,
            diff_against_snapshot,
            save_snapshot,
            push_to_sheets,
            CORE_COLUMNS,
            RESIMPLI_TAB_NAME,
        )

        try:
            rows = parse_resimpli_csv(uploaded)
            rows = cross_reference_with_pipeline(rows, str(_data("leads.csv")))
            # Persist to disk so the TV Dashboard can read it
            try:
                with open(_data("resimpli_latest_leads.csv"), "wb") as fh:
                    uploaded.seek(0)
                    fh.write(uploaded.read())
            except Exception:
                pass
        except Exception as e:
            st.error(f"Failed to parse CSV: {e}")
            st.stop()

        st.success(f"Parsed {len(rows)} lead(s) from {uploaded.name}")

        # ---------------- Summary stats ------------------------------------
        stats = summarize(rows)

        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        col_s1.metric("Total Leads", stats["total"])
        col_s2.metric(
            "Foreclosure Source",
            stats["foreclosure_leads"],
            help="REsimpli leads with 'Foreclosure Auction' source — these came "
                 "from your scraping pipeline",
        )
        col_s3.metric(
            "Matched to Pipeline",
            stats["foreclosure_matched_to_pipeline"],
            help="Of the foreclosure-source leads, how many we found in leads.csv "
                 "by address match",
        )
        col_s4.metric(
            "Statuses",
            len(stats["by_status"]),
        )

        # ---------------- Status / source breakdowns -----------------------
        col_b1, col_b2, col_b3 = st.columns(3)
        with col_b1:
            st.caption("**By Status**")
            for k, v in stats["by_status"].items():
                st.write(f"- {v} {k}")
        with col_b2:
            st.caption("**By Source**")
            for k, v in stats["by_source"].items():
                marker = " 🎯" if "Foreclosure" in k else ""
                st.write(f"- {v} {k}{marker}")
        with col_b3:
            st.caption("**By Acquisition Manager**")
            for k, v in stats["by_acq_manager"].items():
                st.write(f"- {v} {k}")

        st.divider()

        # ---------------- Diff vs last upload ------------------------------
        diff = diff_against_snapshot(rows)
        with st.expander(
            f"Changes since last upload — "
            f"{len(diff['new_leads'])} new / "
            f"{len(diff['status_changes'])} status changes / "
            f"{len(diff['missing_leads'])} missing",
            expanded=bool(
                diff["status_changes"] or
                (diff["new_leads"] and diff["new_leads"] != rows)
            ),
        ):
            if diff["new_leads"]:
                st.write(f"**New leads ({len(diff['new_leads'])}):**")
                df_new = pd.DataFrame([
                    {
                        "Property ID": r.get("Property ID", ""),
                        "Name": f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip(),
                        "Address": r.get("Property Street Address", ""),
                        "Status": r.get("Lead Status", ""),
                        "Source": r.get("Lead Source", ""),
                    }
                    for r in diff["new_leads"]
                ])
                st.dataframe(df_new, width='stretch', height=200)
            if diff["status_changes"]:
                st.write(f"**Status changes ({len(diff['status_changes'])}):**")
                st.dataframe(
                    pd.DataFrame(diff["status_changes"]),
                    width='stretch', height=200,
                )
            if diff["missing_leads"]:
                st.write(
                    f"**Leads in previous import but not in this one "
                    f"({len(diff['missing_leads'])}):**"
                )
                df_miss = pd.DataFrame([
                    {
                        "Property ID": r.get("Property ID", ""),
                        "Name": f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip(),
                        "Address": r.get("Property Street Address", ""),
                        "Last Status": r.get("Lead Status", ""),
                    }
                    for r in diff["missing_leads"]
                ])
                st.dataframe(df_miss, width='stretch', height=200)
            if not diff["new_leads"] and not diff["status_changes"] and not diff["missing_leads"]:
                st.info("No changes since last upload.")

        st.divider()

        # ---------------- Foreclosure cross-reference table ----------------
        foreclosure_rows = [
            r for r in rows
            if "Foreclosure" in (r.get("Lead Source", "") or "")
        ]
        if foreclosure_rows:
            st.subheader("Foreclosure-Source Leads")
            st.caption(
                "REsimpli leads tagged 'Foreclosure Auction' — these came "
                "from your scraping pipeline. The 'Pipeline Match' column "
                "shows whether we found this address in leads.csv."
            )
            xref_df = pd.DataFrame([
                {
                    "Name": f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip(),
                    "Address": r.get("Property Street Address", ""),
                    "City": r.get("Property City", ""),
                    "Status": r.get("Lead Status", ""),
                    "Offer Price": r.get("Offer Price", ""),
                    "Under Contract Price": r.get("Under Contract Price", ""),
                    "Pipeline Match": (
                        "✅ matched"
                        if r.get("pipeline_match") else "—"
                    ),
                    "Pipeline Filing Date": (
                        r["pipeline_match"].get("filing_date", "")
                        if r.get("pipeline_match") else ""
                    ),
                }
                for r in foreclosure_rows
            ])
            st.dataframe(xref_df, width='stretch', height=240)

            st.divider()

        # ---------------- Acquisition Manager Performance ------------------
        st.subheader("Acquisition Manager Performance")

        def _parse_dollar(s: str) -> float:
            if not s:
                return 0.0
            try:
                return float(
                    str(s).replace("$", "").replace(",", "").strip() or 0
                )
            except (ValueError, TypeError):
                return 0.0

        # Build per-manager aggregates
        from collections import defaultdict
        agg = defaultdict(lambda: {
            "deals": 0, "profit": 0.0, "contract": 0.0, "offer": 0.0,
            "leads": [],
        })
        for r in rows:
            am = r.get("Acquisition Manager", "").strip() or "(unassigned)"
            agg[am]["deals"] += 1
            agg[am]["profit"] += _parse_dollar(r.get("Expected Profit", ""))
            agg[am]["contract"] += _parse_dollar(r.get("Under Contract Price", ""))
            agg[am]["offer"] += _parse_dollar(r.get("Offer Price", ""))
            agg[am]["leads"].append(r)

        # Top-level metrics
        total_deals = sum(d["deals"] for d in agg.values())
        total_profit = sum(d["profit"] for d in agg.values())
        total_contract = sum(d["contract"] for d in agg.values())
        avg_profit_per_deal = (
            total_profit / total_deals if total_deals else 0.0
        )

        m_col1, m_col2, m_col3, m_col4 = st.columns(4)
        m_col1.metric("Total Deals", total_deals)
        m_col2.metric("Total Expected Profit", f"${total_profit:,.0f}")
        m_col3.metric("Total Under-Contract", f"${total_contract:,.0f}")
        m_col4.metric("Avg Profit / Deal", f"${avg_profit_per_deal:,.0f}")

        # Build the per-manager dataframe sorted by profit
        perf_df = pd.DataFrame([
            {
                "Acquisition Manager": am,
                "Deals": d["deals"],
                "Expected Profit": d["profit"],
                "Avg Profit / Deal": d["profit"] / d["deals"] if d["deals"] else 0,
                "Total Under-Contract": d["contract"],
                "Total Offered": d["offer"],
            }
            for am, d in agg.items()
        ]).sort_values("Expected Profit", ascending=False).reset_index(drop=True)

        # Color palette per manager (consistent across charts)
        palette = ["#FF4B4B", "#4CC9F0", "#F8961E", "#90BE6D",
                   "#9B5DE5", "#577590", "#F94144", "#43AA8B"]
        color_map = {
            am: palette[i % len(palette)]
            for i, am in enumerate(perf_df["Acquisition Manager"])
        }

        # Two side-by-side charts: Profit + Deal Count
        try:
            import plotly.express as px

            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                fig = px.bar(
                    perf_df,
                    x="Expected Profit", y="Acquisition Manager",
                    orientation="h",
                    text=perf_df["Expected Profit"].apply(
                        lambda x: f"${x:,.0f}"
                    ),
                    color="Acquisition Manager",
                    color_discrete_map=color_map,
                    title="Expected Profit by Acquisition Manager",
                )
                fig.update_layout(
                    showlegend=False, height=320,
                    yaxis={"categoryorder": "total ascending"},
                    margin=dict(l=10, r=10, t=50, b=10),
                )
                fig.update_traces(textposition="outside")
                st.plotly_chart(fig, width='stretch')

            with chart_col2:
                fig2 = px.bar(
                    perf_df,
                    x="Deals", y="Acquisition Manager",
                    orientation="h",
                    text="Deals",
                    color="Acquisition Manager",
                    color_discrete_map=color_map,
                    title="Deal Count by Acquisition Manager",
                )
                fig2.update_layout(
                    showlegend=False, height=320,
                    yaxis={"categoryorder": "total ascending"},
                    margin=dict(l=10, r=10, t=50, b=10),
                )
                fig2.update_traces(textposition="outside")
                st.plotly_chart(fig2, width='stretch')

            # Per-deal profit scatter
            scatter_df = pd.DataFrame([
                {
                    "Acquisition Manager": (
                        r.get("Acquisition Manager", "").strip() or "(unassigned)"
                    ),
                    "Property": r.get("Property Street Address", ""),
                    "Lead": (
                        f"{r.get('First Name','')} {r.get('Last Name','')}".strip()
                    ),
                    "Expected Profit": _parse_dollar(r.get("Expected Profit", "")),
                    "Under Contract Price": _parse_dollar(
                        r.get("Under Contract Price", "")
                    ),
                }
                for r in rows
            ])
            fig3 = px.scatter(
                scatter_df,
                x="Under Contract Price", y="Expected Profit",
                color="Acquisition Manager",
                color_discrete_map=color_map,
                hover_data=["Property", "Lead"],
                size="Expected Profit",
                size_max=40,
                title="Expected Profit vs. Contract Price (per deal)",
            )
            fig3.update_layout(
                height=400, margin=dict(l=10, r=10, t=50, b=10),
            )
            st.plotly_chart(fig3, width='stretch')

        except ImportError:
            # Fallback to native streamlit chart if plotly missing
            st.bar_chart(
                perf_df.set_index("Acquisition Manager")[["Expected Profit"]]
            )

        # Leaderboard table
        st.caption("**Leaderboard**")
        display_df = perf_df.copy()
        display_df["Expected Profit"] = display_df["Expected Profit"].apply(
            lambda x: f"${x:,.0f}"
        )
        display_df["Avg Profit / Deal"] = display_df["Avg Profit / Deal"].apply(
            lambda x: f"${x:,.0f}"
        )
        display_df["Total Under-Contract"] = display_df["Total Under-Contract"].apply(
            lambda x: f"${x:,.0f}"
        )
        display_df["Total Offered"] = display_df["Total Offered"].apply(
            lambda x: f"${x:,.0f}"
        )
        st.dataframe(display_df, width='stretch', hide_index=True)

        # Per-manager drill-down
        with st.expander("Drill into a specific Acquisition Manager"):
            am_choice = st.selectbox(
                "Pick a manager",
                ["(all)"] + list(perf_df["Acquisition Manager"]),
                key="resimpli_am_drill",
            )
            if am_choice and am_choice != "(all)":
                drill_rows = agg[am_choice]["leads"]
                drill_df = pd.DataFrame([
                    {
                        "Property": r.get("Property Street Address", ""),
                        "City": r.get("Property City", ""),
                        "Lead": f"{r.get('First Name','')} {r.get('Last Name','')}".strip(),
                        "Status": r.get("Lead Status", ""),
                        "Source": r.get("Lead Source", ""),
                        "Offer": r.get("Offer Price", ""),
                        "Contract": r.get("Under Contract Price", ""),
                        "Expected Profit": r.get("Expected Profit", ""),
                        "Closing Date": r.get("Schedule Closing Date", ""),
                    }
                    for r in drill_rows
                ])
                st.dataframe(drill_df, width='stretch', hide_index=True)
                st.caption(
                    f"**{am_choice}** has **{len(drill_rows)}** active deal(s) "
                    f"totaling **${agg[am_choice]['profit']:,.0f}** in expected profit."
                )

        st.divider()

        # ---------------- Full lead table ----------------------------------
        st.subheader("All Leads (filtered to populated columns)")
        # Drop columns that are 100% empty for cleaner display
        populated_cols = [
            c for c in CORE_COLUMNS
            if any(r.get(c, "").strip() for r in rows if isinstance(r.get(c, ""), str))
        ]
        df_all = pd.DataFrame([
            {c: r.get(c, "") for c in populated_cols} for r in rows
        ])
        st.dataframe(df_all, width='stretch', height=400)

        st.download_button(
            "Download cleaned CSV",
            data=df_all.to_csv(index=False).encode("utf-8"),
            file_name=uploaded.name.replace(".csv", "_cleaned.csv"),
            mime="text/csv",
        )

        st.divider()

        # ---------------- Push to Google Sheets ----------------------------
        st.subheader("Push to Google Sheets")
        default_sheet_id = (
            os.getenv("RESIMPLI_SHEET_ID") or
            os.getenv("FORECLOSURE_SHEET_ID") or ""
        )
        col_p1, col_p2 = st.columns([2, 1])
        with col_p1:
            sheet_id = st.text_input(
                "Google Sheet ID",
                value=default_sheet_id,
                key="resimpli_sheet_id",
                help="The long ID from the sheet URL "
                     "(.../spreadsheets/d/THIS_PART/edit). "
                     "Will create/replace a tab named 'REsimpli Leads'.",
            )
        with col_p2:
            tab_name = st.text_input(
                "Tab Name", value=RESIMPLI_TAB_NAME, key="resimpli_tab_name",
            )

        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("Push to Google Sheets", type="primary", key="resimpli_push"):
                if not sheet_id:
                    st.error("Please provide a Google Sheet ID")
                else:
                    with st.spinner("Pushing to Sheets..."):
                        try:
                            count, url = push_to_sheets(
                                rows, sheet_id=sheet_id, tab_name=tab_name,
                            )
                            save_snapshot(rows)
                            st.success(f"Pushed {count} lead(s) to Google Sheets")
                            st.markdown(f"[Open Sheet]({url})")
                        except Exception as e:
                            st.error(f"Push failed: {e}")
        with col_btn2:
            if st.button("Save Snapshot Only (skip push)", key="resimpli_snapshot_only"):
                save_snapshot(rows)
                st.success(
                    "Snapshot saved. Next upload will diff against this one."
                )

    # =====================================================================
    # INVENTORY (current portfolio) — separate uploader, separate metrics
    # =====================================================================
    st.divider()
    st.header("📦 Current Inventory")
    st.caption(
        "Houses the team currently owns or has under contract. "
        "Headline metric: total expected profit across the active portfolio."
    )

    with st.expander("📋 How to get the Inventory CSV", expanded=False):
        st.markdown(
            """
            1. In REsimpli, go to the **Inventory** tab (separate from Leads)
            2. Click **"Select All"** at the top of the inventory table
            3. Click **"Export"** in the toolbar
            4. Drop the downloaded CSV in the box below
            """
        )

    inventory_uploaded = st.file_uploader(
        "Upload REsimpli Inventory CSV",
        type=["csv"],
        key="inventory_upload",
    )

    if inventory_uploaded:
        from inventory_importer import (
            parse_inventory_csv,
            summarize as inv_summarize,
            parse_dollar,
            CORE_COLUMNS as INV_CORE_COLUMNS,
        )

        try:
            inv_rows = parse_inventory_csv(inventory_uploaded)
            # Persist to disk for TV Dashboard
            try:
                with open(_data("resimpli_latest_inventory.csv"), "wb") as fh:
                    inventory_uploaded.seek(0)
                    fh.write(inventory_uploaded.read())
            except Exception:
                pass
        except Exception as e:
            st.error(f"Failed to parse inventory CSV: {e}")
            st.stop()

        inv_stats = inv_summarize(inv_rows)

        # ---------------- HERO METRIC ------------------------------------
        # Big banner-style display for total expected profit
        st.markdown(
            f"""
            <div style='
                background: linear-gradient(135deg, #1e3a5f 0%, #2d5f8f 100%);
                padding: 28px 32px;
                border-radius: 14px;
                border-left: 6px solid #4CC9F0;
                margin: 12px 0 18px 0;
                box-shadow: 0 4px 12px rgba(0,0,0,0.25);
            '>
                <div style='font-size: 13px; color: #b8d4ea; letter-spacing: 1px;
                            text-transform: uppercase; margin-bottom: 6px;'>
                    Total Expected Profit — Current Portfolio
                </div>
                <div style='font-size: 52px; font-weight: 800; color: #ffffff;
                            line-height: 1.1;'>
                    ${inv_stats['total_expected_profit']:,.0f}
                </div>
                <div style='font-size: 14px; color: #b8d4ea; margin-top: 8px;'>
                    {inv_stats['total_props']} properties •
                    {inv_stats['props_with_profit']} with profit estimates •
                    {inv_stats['props_no_profit_yet']} pending
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ---------------- Status breakdown metrics -----------------------
        status_cols = st.columns(min(len(inv_stats["by_status"]), 4) or 1)
        for i, (status, data) in enumerate(inv_stats["by_status"].items()):
            with status_cols[i % len(status_cols)]:
                st.metric(
                    f"{status} ({data['count']})",
                    f"${data['profit']:,.0f}",
                    help=f"{data['count']} properties expected to net "
                         f"${data['profit']:,.0f}",
                )

        st.divider()

        # ---------------- Charts: by status + by project type ------------
        try:
            import plotly.express as px

            chart_col1, chart_col2 = st.columns(2)

            with chart_col1:
                status_df = pd.DataFrame([
                    {"Status": s, "Profit": d["profit"], "Count": d["count"]}
                    for s, d in inv_stats["by_status"].items()
                ])
                fig = px.pie(
                    status_df,
                    values="Profit",
                    names="Status",
                    title="Expected Profit by Status",
                    hole=0.55,
                    color_discrete_sequence=px.colors.qualitative.Set2,
                )
                fig.update_traces(
                    textposition="outside",
                    textinfo="label+percent",
                    hovertemplate=(
                        "<b>%{label}</b><br>"
                        "Profit: $%{value:,.0f}<br>"
                        "Share: %{percent}<extra></extra>"
                    ),
                )
                fig.update_layout(
                    height=360, margin=dict(l=10, r=10, t=50, b=10),
                    showlegend=False,
                )
                st.plotly_chart(fig, width='stretch')

            with chart_col2:
                type_df = pd.DataFrame([
                    {"Project Type": t, "Profit": d["profit"], "Count": d["count"]}
                    for t, d in inv_stats["by_project_type"].items()
                ])
                fig2 = px.bar(
                    type_df.sort_values("Profit"),
                    x="Profit", y="Project Type",
                    orientation="h",
                    text=type_df.sort_values("Profit")["Profit"].apply(
                        lambda x: f"${x:,.0f}"
                    ),
                    color="Project Type",
                    color_discrete_sequence=px.colors.qualitative.Set2,
                    title="Expected Profit by Project Type",
                )
                fig2.update_layout(
                    height=360, margin=dict(l=10, r=10, t=50, b=10),
                    showlegend=False,
                )
                fig2.update_traces(textposition="outside")
                st.plotly_chart(fig2, width='stretch')

            # Per-property profit waterfall
            sorted_inv = sorted(
                inv_rows,
                key=lambda r: -parse_dollar(r.get("Expected Profit", "")),
            )
            prop_df = pd.DataFrame([
                {
                    "Property": (
                        r.get("Property Street Address", "")[:30]
                        + (", " + r.get("Property City", "")
                           if r.get("Property City") else "")
                    ),
                    "Status": r.get("Property Status", ""),
                    "Project Type": r.get("Project Type", ""),
                    "Profit": parse_dollar(r.get("Expected Profit", "")),
                }
                for r in sorted_inv
            ])
            fig3 = px.bar(
                prop_df,
                x="Profit", y="Property",
                orientation="h",
                color="Status",
                color_discrete_sequence=px.colors.qualitative.Set2,
                hover_data=["Project Type"],
                title="Per-Property Expected Profit",
                text=prop_df["Profit"].apply(lambda x: f"${x:,.0f}"),
            )
            fig3.update_layout(
                height=max(400, 22 * len(prop_df)),
                yaxis={"categoryorder": "total ascending"},
                margin=dict(l=10, r=10, t=50, b=10),
            )
            fig3.update_traces(textposition="outside")
            st.plotly_chart(fig3, width='stretch')

        except ImportError:
            pass

        # ---------------- Inventory table --------------------------------
        st.subheader("Property Detail")
        inv_populated = [
            c for c in INV_CORE_COLUMNS
            if any(r.get(c, "").strip() for r in inv_rows
                   if isinstance(r.get(c, ""), str))
        ]
        inv_df = pd.DataFrame([
            {c: r.get(c, "") for c in inv_populated} for r in inv_rows
        ])
        # Sort by Expected Profit descending
        if "Expected Profit" in inv_df.columns:
            inv_df["_sort"] = inv_df["Expected Profit"].apply(parse_dollar)
            inv_df = inv_df.sort_values("_sort", ascending=False).drop(
                columns=["_sort"]
            )
        st.dataframe(inv_df, width='stretch', height=400)

        st.download_button(
            "Download cleaned inventory CSV",
            data=inv_df.to_csv(index=False).encode("utf-8"),
            file_name=inventory_uploaded.name.replace(".csv", "_cleaned.csv"),
            mime="text/csv",
        )
