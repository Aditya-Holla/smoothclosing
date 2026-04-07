"""
dashboard.py — SmoothClosing Team Dashboard

Simple interface for the acquisitions and dispositions teams.
Credentials are loaded from .env automatically — no sidebar config needed.

Run:
    streamlit run dashboard.py
"""

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

# Use the agents venv python if available
PYTHON = str(Path(__file__).parent / ".venv-agents" / "bin" / "python3")
if not Path(PYTHON).exists():
    PYTHON = sys.executable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_script(cmd: list[str], status_container) -> subprocess.CompletedProcess:
    """Run a pipeline script and stream output."""
    status_container.info(f"Running: `{' '.join(cmd)}`")
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent),
        timeout=600,
    )
    if result.stdout:
        status_container.code(result.stdout[-3000:], language=None)
    if result.returncode != 0 and result.stderr:
        # Filter to just ERROR/WARNING lines
        errors = [l for l in result.stderr.split('\n') if 'ERROR' in l or 'WARNING' in l or 'Error' in l]
        if errors:
            status_container.error('\n'.join(errors[-10:]))
    return result


def count_csv_rows(path: str) -> int:
    if not Path(path).exists():
        return 0
    with open(path) as f:
        return sum(1 for _ in csv.reader(f)) - 1  # minus header


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("🏠 SmoothClosing")

tab_chat, tab_acq, tab_dispo = st.tabs(["Chat", "Acquisitions", "Dispositions"])

# ===========================================================================
# CHAT TAB — Talk to the orchestrator
# ===========================================================================

with tab_chat:
    st.header("Ask the Assistant")
    st.caption(
        "Talk to the SmoothClosing assistant. It can run the pipeline, "
        "skip trace leads, look up properties on CAD, trace buyers, and more."
    )

    # Initialize chat history
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    # Display chat history
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat input
    if prompt := st.chat_input("e.g. 'who owns 503 Pintail Ln in Williamson county?'"):
        # Add user message
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Run the orchestrator
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    result = subprocess.run(
                        [PYTHON, "-c", f"""
import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock
from agents.orchestrator import build_options

async def main():
    opts = build_options()
    result_text = ""
    async for message in query(prompt={prompt!r}, options=opts):
        if isinstance(message, ResultMessage):
            if message.result:
                result_text = message.result
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_text = block.text
    print(result_text)

asyncio.run(main())
"""],
                        capture_output=True, text=True,
                        cwd=str(Path(__file__).parent),
                        timeout=300,
                    )
                    response = result.stdout.strip()
                    if not response and result.stderr:
                        # Check for errors
                        error_lines = [l for l in result.stderr.split('\n') if 'Error' in l or 'error' in l]
                        response = "Sorry, something went wrong. " + (error_lines[-1] if error_lines else "Check the logs.")
                    if not response:
                        response = "No response from the assistant."
                except subprocess.TimeoutExpired:
                    response = "Request timed out (5 min limit). Try a simpler question."
                except Exception as e:
                    response = f"Error: {e}"

            st.markdown(response)
            st.session_state.chat_messages.append({"role": "assistant", "content": response})


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
                out = run_script([PYTHON, "county_downloader.py"], st.empty())
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
            log_area = st.empty()
            with st.spinner("Parsing PDFs..."):
                out = run_script([PYTHON, "main.py", "--input", "./input_pdfs", "--output", "leads.csv"], log_area)
            if out.returncode == 0:
                count = count_csv_rows("leads.csv")
                st.success(f"{count} leads extracted")
                if include_equity:
                    with st.spinner("Running equity estimator..."):
                        out2 = run_script([PYTHON, "equity_estimator.py", "--input", "leads.csv", "--output", "leads_with_equity.csv"], log_area)
                    if out2.returncode == 0:
                        st.success("Equity estimates added")
            else:
                st.error("Processing failed")

    # --- Step 3: Skip Trace ---
    with col3:
        st.subheader("3. Skip Trace")
        st.caption("Find phone numbers via Skip Genie")
        trace_limit = st.number_input("Max leads", min_value=0, value=0, step=5, key="trace_lim",
                                       help="0 = all leads")
        if st.button("Skip Trace", type="primary", key="trace"):
            input_file = "leads_with_equity.csv" if Path("leads_with_equity.csv").exists() else "leads.csv"
            cmd = [PYTHON, "skipgenie.py", "--input", input_file, "--output", "leads_traced.csv"]
            if trace_limit > 0:
                cmd.extend(["--max-relatives", "0"])  # faster
            with st.spinner("Skip tracing... this takes a while"):
                out = run_script(cmd, st.empty())
            if out.returncode == 0:
                st.success("Skip trace complete")

    # --- Step 4: Text ---
    with col4:
        st.subheader("4. Text")
        st.caption("Send SMS via RingCentral")
        dry_run = st.checkbox("Dry run first", value=True, key="sms_dry")
        if st.button("Send Texts", type="primary", key="sms"):
            cmd = [PYTHON, "ringcentral_sms.py", "--input", "leads_traced.csv", "--output", "leads_sms_sent.csv"]
            if dry_run:
                cmd.append("--dry-run")
            with st.spinner("Sending..." if not dry_run else "Dry run..."):
                out = run_script(cmd, st.empty())
            if out.returncode == 0:
                if dry_run:
                    st.info("Dry run complete — review output above. Uncheck 'Dry run' to send for real.")
                else:
                    st.success("Texts sent")

    # --- Run All ---
    st.divider()
    if st.button("Run Full Pipeline", type="secondary", key="run_all"):
        progress = st.empty()

        progress.info("Step 1/4 — Downloading PDFs...")
        run_script([PYTHON, "county_downloader.py"], st.empty())

        progress.info("Step 2/4 — Processing leads...")
        run_script([PYTHON, "main.py", "--input", "./input_pdfs", "--output", "leads.csv"], st.empty())

        progress.info("Step 3/4 — Skip tracing...")
        run_script([PYTHON, "skipgenie.py", "--input", "leads.csv", "--output", "leads_traced.csv"], st.empty())

        progress.info("Step 4/4 — Pushing to Google Sheets...")
        run_script([PYTHON, "-c", """
import csv
from sheets_exporter import export_to_sheets
with open('leads_traced.csv') as f:
    records = list(csv.DictReader(f))
export_to_sheets(records)
"""], st.empty())

        progress.success("Pipeline complete!")

    # --- View Current Leads ---
    st.divider()
    st.subheader("Current Leads")
    for csv_name in ["leads_traced.csv", "leads_with_equity.csv", "leads.csv"]:
        if Path(csv_name).exists():
            df = pd.read_csv(csv_name)
            st.write(f"**{csv_name}** — {len(df)} leads")
            st.dataframe(df, use_container_width=True, height=400)
            st.download_button(
                f"Download {csv_name}",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=csv_name,
                mime="text/csv",
            )
            break
    else:
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
                                         key="dispo_limit", help="0 = all pending rows")

        if st.button("Trace Buyers", type="primary", key="trace_buyers"):
            cmd = [PYTHON, "buyer_tracer.py", "--tab", metro]
            if trace_limit_d > 0:
                cmd.extend(["--limit", str(trace_limit_d)])
            with st.spinner(f"Tracing {metro} buyers... this takes ~30s per name"):
                log_area = st.empty()
                out = run_script(cmd, log_area)
            if out.returncode == 0:
                st.success(f"Done — check the Dispositions sheet")
            else:
                st.error("Trace failed — check logs above")

    with col_right:
        # Show current state of the selected tab
        try:
            from sheets_exporter import _get_client
            DISPO_SHEET_ID = "1CCcMeIP8we_HsnUBnE4Pe-RAm2JYnousjtuCRhGK6x8"
            client = _get_client()
            sheet = client.open_by_key(DISPO_SHEET_ID)
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
                    st.dataframe(df, use_container_width=True, height=400)
                else:
                    st.info(f"No data in {metro} yet. Add buyer names to the sheet first.")
            else:
                st.info(f"No data in {metro} yet. Add buyer names to the sheet first.")
        except Exception as e:
            st.warning(f"Could not load sheet: {e}")

    st.divider()
    st.caption(
        "Dispositions sheet: "
        "[Open in Google Sheets](https://docs.google.com/spreadsheets/d/1CCcMeIP8we_HsnUBnE4Pe-RAm2JYnousjtuCRhGK6x8)"
    )
