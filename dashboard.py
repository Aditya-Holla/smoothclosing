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

def run_script(cmd: list[str], status_container, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a pipeline script and stream output.

    timeout defaults to 10 min. Long-running steps (SMS with 5-min relative
    waits, big skip-trace batches) must pass a larger value explicitly.
    """
    status_container.info(f"Running: `{' '.join(cmd)}`")
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent),
        timeout=timeout,
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
        st.caption("Find phone numbers for leads from the LAST pipeline run only")
        trace_limit = st.number_input("Max leads", min_value=0, value=0, step=5, key="trace_lim",
                                       help="0 = all leads from the latest run")
        if st.button("Skip Trace", type="primary", key="trace"):
            # Prefer leads_new_equity.csv (has equity data) over leads_new.csv.
            # Intentionally NOT falling back to leads.csv - that would re-trace
            # accumulated old leads from prior runs.
            if Path("leads_new_equity.csv").exists():
                input_file = "leads_new_equity.csv"
            elif Path("leads_new.csv").exists():
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
            cmd = [PYTHON, "skipgenie.py", "--input", input_file, "--output", "leads_new_traced.csv"]
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
            if not Path("leads_new_traced.csv").exists():
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
            cmd = [PYTHON, "ringcentral_sms.py",
                   "--input", "leads_new_traced.csv",
                   "--output", "leads_new_sms_sent.csv"]
            if dry_run:
                cmd.append("--dry-run")
            # SMS can take hours due to 5-min wait before each relative.
            # Estimate: ~5 min per relative + ~2 sec per owner. Give 4 hours.
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
                    [PYTHON, "sync_call_status.py"],
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
    if st.button("Run Full Pipeline", type="secondary", key="run_all"):
        progress = st.empty()

        progress.info("Step 1/4 - Downloading PDFs...")
        run_script([PYTHON, "county_downloader.py"], st.empty())

        progress.info("Step 2/4 - Parsing new PDFs (main.py writes leads_new.csv)...")
        run_script([PYTHON, "main.py", "--input", "./input_pdfs", "--output", "leads.csv"], st.empty())

        if Path("leads_new_equity.csv").exists():
            new_file = "leads_new_equity.csv"
        elif Path("leads_new.csv").exists():
            new_file = "leads_new.csv"
        else:
            new_file = None

        if not new_file or count_csv_rows(new_file) == 0:
            progress.success(
                "Pipeline finished early - no new leads this run. Nothing to trace or push."
            )
            st.stop()

        progress.info(f"Step 3/4 - Skip tracing {count_csv_rows(new_file)} new lead(s) from {new_file}...")
        run_script(
            [PYTHON, "skipgenie.py", "--input", new_file, "--output", "leads_new_traced.csv"],
            st.empty(),
        )

        progress.info("Step 4/4 - Pushing NEW leads to Google Sheets...")
        run_script([PYTHON, "-c", """
import csv
from sheets_exporter import export_to_sheets
with open('leads_new_traced.csv') as f:
    records = list(csv.DictReader(f))
export_to_sheets(records)
"""], st.empty())

        progress.success(
            "Pipeline complete! Review leads_new_traced.csv below, then click Send Texts when ready."
        )

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
        if not Path(csv_name).exists():
            continue
        # Render in Google Sheet format: 1 owner row + relative rows beneath,
        # with Call Status populated from sms_history.csv so you can see at
        # a glance who's been texted. Matches what ends up in the sheet.
        try:
            import csv as _csv, re as _re
            from sheets_exporter import records_to_sheet_rows, HEADER_ROW

            with open(csv_name, encoding="utf-8") as f:
                records = list(_csv.DictReader(f))

            # Build phone -> sent_at lookup from sms_history for Call Status
            def _norm(p):
                d = _re.sub(r"\D", "", p or "")
                return d[-10:] if len(d) >= 10 else d
            sent_lookup = {}
            if Path("sms_history.csv").exists():
                with open("sms_history.csv", encoding="utf-8") as hf:
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

            df = pd.DataFrame(rows, columns=HEADER_ROW)
            owners_count = len(records)
            st.write(f"**{csv_name}** - {label} - {owners_count} lead(s), {len(rows)} total rows (owner + relatives)")
            st.dataframe(df, use_container_width=True, height=400)
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
            st.dataframe(df, use_container_width=True, height=400)
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
                cmd = [PYTHON, "buyer_tracer.py", "--all-tabs"]
                label = "all tabs"
            else:
                cmd = [PYTHON, "buyer_tracer.py", "--tab", metro]
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
