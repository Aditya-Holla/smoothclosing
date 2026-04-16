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

tab_chat, tab_acq, tab_dispo, tab_aoh = st.tabs(["Chat", "Acquisitions", "Dispositions", "Heirship Affidavit"])

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
                    st.dataframe(df, use_container_width=True, height=400)
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
        decedent_full_name = st.text_input("Decedent Full Name", key="aoh_dec_name",
                                            placeholder="e.g. Ethel Mae Hafernik Hummell")
        decedent_aka = st.text_input("Decedent AKA (leave blank if none)", key="aoh_dec_aka",
                                      placeholder="e.g. Ethel M. Hummell")
        decedent_dob = st.text_input("Decedent Date of Birth", key="aoh_dec_dob",
                                      placeholder="MM/DD/YYYY")
    with col_d2:
        death_date = st.text_input("Date of Death", key="aoh_death_date",
                                    placeholder="e.g. October 6, 2010")
        death_city = st.text_input("City of Death", key="aoh_death_city", value="Austin")
        death_county = st.text_input("County of Death", key="aoh_death_county", value="Travis")

    st.caption("Decedent's Residential Address at Time of Death")
    col_r1, col_r2 = st.columns(2)
    with col_r1:
        res_address = st.text_input("Street Address", key="aoh_res_addr",
                                     placeholder="e.g. 8710 Colonial Dr.")
        res_city = st.text_input("City", key="aoh_res_city", value="Austin")
        res_state = st.text_input("State", key="aoh_res_state", value="Texas")
    with col_r2:
        res_zip = st.text_input("Zip", key="aoh_res_zip")
        res_county = st.text_input("County", key="aoh_res_county", value="Travis")

    st.divider()

    # -----------------------------------------------------------------------
    # Section 2: Affiant (family member)
    # -----------------------------------------------------------------------
    st.subheader("Affiant (Family Member)")
    col_a1, col_a2 = st.columns(2)
    with col_a1:
        aff_name = st.text_input("Affiant Name", key="aoh_aff_name",
                                  placeholder="e.g. Norman Hummell")
        aff_aka = st.text_input("Affiant AKA (leave blank if none)", key="aoh_aff_aka",
                                 placeholder="e.g. Norman S. Hummell")
        aff_relationship = st.text_input("Relationship to Decedent", key="aoh_aff_rel",
                                          placeholder="e.g. husband")
        aff_years = st.text_input("Years Known Decedent", key="aoh_aff_years",
                                   placeholder="e.g. forty-eight (48)")
    with col_a2:
        aff_address = st.text_input("Street Address", key="aoh_aff_addr",
                                     placeholder="e.g. 8710 Colonial Dr.")
        aff_city = st.text_input("City", key="aoh_aff_city", value="Austin")
        aff_county = st.text_input("County", key="aoh_aff_county", value="Travis")
        aff_state = st.text_input("State", key="aoh_aff_state", value="Texas")
        aff_zip = st.text_input("Zip", key="aoh_aff_zip")

    st.divider()

    # -----------------------------------------------------------------------
    # Section 3 & 4: Witnesses
    # -----------------------------------------------------------------------
    st.subheader("Witnesses (2 required — not related to decedent, knew decedent 10+ years)")
    col_w1, col_w2 = st.columns(2)

    with col_w1:
        st.caption("Witness 1")
        w1_name = st.text_input("Name", key="aoh_w1_name")
        w1_address = st.text_input("Street Address", key="aoh_w1_addr")
        w1_city = st.text_input("City", key="aoh_w1_city")
        w1_county = st.text_input("County", key="aoh_w1_county")
        w1_state = st.text_input("State", key="aoh_w1_state", value="Texas")
        w1_zip = st.text_input("Zip", key="aoh_w1_zip")
        w1_relationship = st.text_input("Relationship", key="aoh_w1_rel",
                                         placeholder="e.g. friend")
        w1_years = st.text_input("Years Known", key="aoh_w1_years",
                                  placeholder="e.g. fifteen (15)")

    with col_w2:
        st.caption("Witness 2")
        w2_name = st.text_input("Name", key="aoh_w2_name")
        w2_address = st.text_input("Street Address", key="aoh_w2_addr")
        w2_city = st.text_input("City", key="aoh_w2_city")
        w2_county = st.text_input("County", key="aoh_w2_county")
        w2_state = st.text_input("State", key="aoh_w2_state", value="Texas")
        w2_zip = st.text_input("Zip", key="aoh_w2_zip")
        w2_relationship = st.text_input("Relationship", key="aoh_w2_rel",
                                         placeholder="e.g. friend")
        w2_years = st.text_input("Years Known", key="aoh_w2_years",
                                  placeholder="e.g. fifteen (15)")

    st.divider()

    # -----------------------------------------------------------------------
    # Section 5: Marital History
    # -----------------------------------------------------------------------
    st.subheader("Marital History")
    num_marriages = st.number_input(
        "Number of marriages", min_value=0, max_value=5, value=1, key="aoh_num_marriages_input",
    )
    marriages_data = []
    for i in range(num_marriages):
        col_m1, col_m2, col_m3 = st.columns(3)
        with col_m1:
            m_date = st.text_input(f"Marriage {i+1} Date", key=f"aoh_m{i}_date",
                                    placeholder="MM/DD/YYYY")
        with col_m2:
            m_spouse = st.text_input(f"Spouse Name", key=f"aoh_m{i}_spouse")
        with col_m3:
            m_spouse_aka = st.text_input(f"Spouse AKA", key=f"aoh_m{i}_spouse_aka",
                                          placeholder="leave blank if none")
        marriages_data.append({"date": m_date, "spouse_name": m_spouse, "spouse_aka": m_spouse_aka})

    divorced = st.selectbox("Was decedent divorced?", ["No", "Yes"], key="aoh_divorced")
    divorce_dates = []
    if divorced == "Yes":
        num_divorces = st.number_input("Number of divorces", min_value=1, max_value=5, value=1, key="aoh_num_div")
        for i in range(num_divorces):
            dd = st.text_input(f"Divorce {i+1} Date", key=f"aoh_div{i}_date")
            divorce_dates.append(dd)

    st.caption("Remarriages (if any)")
    num_remarriages = st.number_input(
        "Number of remarriages", min_value=0, max_value=5, value=0, key="aoh_num_remarriages",
    )
    remarriages_data = []
    for i in range(num_remarriages):
        col_rm1, col_rm2, col_rm3 = st.columns(3)
        with col_rm1:
            rm_date = st.text_input(f"Remarriage {i+1} Date", key=f"aoh_rm{i}_date",
                                     placeholder="MM/DD/YYYY")
        with col_rm2:
            rm_spouse = st.text_input(f"Spouse Name", key=f"aoh_rm{i}_spouse")
        with col_rm3:
            rm_spouse_aka = st.text_input(f"Spouse AKA", key=f"aoh_rm{i}_spouse_aka",
                                           placeholder="leave blank if none")
        remarriages_data.append({"date": rm_date, "spouse_name": rm_spouse, "spouse_aka": rm_spouse_aka})

    st.divider()

    # -----------------------------------------------------------------------
    # Section 6: Children
    # -----------------------------------------------------------------------
    st.subheader("Children")
    num_children = st.number_input(
        "Number of children", min_value=0, max_value=20, value=1, key="aoh_num_children_input",
    )
    children_data = []
    for i in range(num_children):
        col_c1, col_c2, col_c3, col_c4 = st.columns(4)
        with col_c1:
            c_name = st.text_input(f"Child {i+1} Full Name", key=f"aoh_c{i}_name")
        with col_c2:
            c_dob = st.text_input(f"Date of Birth", key=f"aoh_c{i}_dob", placeholder="MM/DD/YYYY")
        with col_c3:
            c_rel = st.selectbox(f"Type", ["Biological", "Step", "Adopted"], key=f"aoh_c{i}_rel")
        with col_c4:
            c_parent = st.text_input(f"Other Parent", key=f"aoh_c{i}_parent")
        children_data.append({"name": c_name, "dob": c_dob, "relationship": c_rel, "other_parent": c_parent})

    st.divider()

    # -----------------------------------------------------------------------
    # Section 7: Deceased Children
    # -----------------------------------------------------------------------
    st.subheader("Deceased Children (if any)")
    num_deceased = st.number_input(
        "Number of deceased children", min_value=0, max_value=20, value=0, key="aoh_num_dec_children",
    )
    deceased_children_data = []
    for i in range(num_deceased):
        col_dc1, col_dc2 = st.columns(2)
        with col_dc1:
            dc_name = st.text_input(f"Deceased Child {i+1} Name", key=f"aoh_dc{i}_name")
        with col_dc2:
            dc_death = st.text_input(f"Date of Death", key=f"aoh_dc{i}_death", placeholder="MM/DD/YYYY")
        deceased_children_data.append({"name": dc_name, "death_date": dc_death})

    st.divider()

    # -----------------------------------------------------------------------
    # Section 8: Grandchildren of Deceased Children
    # -----------------------------------------------------------------------
    st.subheader("Children of Deceased Children (if any)")
    num_gc = st.number_input(
        "Number of children of deceased children", min_value=0, max_value=20, value=0, key="aoh_num_gc",
    )
    grandchildren_data = []
    for i in range(num_gc):
        col_g1, col_g2, col_g3 = st.columns(3)
        with col_g1:
            g_name = st.text_input(f"Grandchild {i+1} Name", key=f"aoh_gc{i}_name")
        with col_g2:
            g_dob = st.text_input(f"Date of Birth", key=f"aoh_gc{i}_dob", placeholder="MM/DD/YYYY")
        with col_g3:
            g_parents = st.text_input(f"Parents", key=f"aoh_gc{i}_parents")
        grandchildren_data.append({"name": g_name, "dob": g_dob, "parents": g_parents})

    st.divider()

    # -----------------------------------------------------------------------
    # Section 9: Additional Info
    # -----------------------------------------------------------------------
    st.subheader("Additional Information")
    had_will = st.text_input(
        "Did decedent have a Last Will and Testament? Was it probated?",
        key="aoh_will",
        placeholder="e.g. No",
    )
    unpaid_debts = st.text_input(
        "Any debts when decedent died? Any unpaid debts remaining?",
        key="aoh_debts",
        placeholder="e.g. No",
    )
    unpaid_taxes = st.selectbox("Unpaid estate or inheritance taxes?", ["No", "Yes"], key="aoh_taxes")

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
        return {
            "decedent_full_name": decedent_full_name,
            "decedent_aka": decedent_aka,
            "decedent_dob": decedent_dob,
            "death_date": death_date,
            "death_city": death_city,
            "death_county": death_county,
            "residence_address": res_address,
            "residence_city": res_city,
            "residence_state": res_state,
            "residence_zip": res_zip,
            "residence_county": res_county,
            "affiant_name": aff_name,
            "affiant_aka": aff_aka,
            "affiant_relationship": aff_relationship,
            "affiant_years_known": aff_years,
            "affiant_address": aff_address,
            "affiant_city": aff_city,
            "affiant_county": aff_county,
            "affiant_state": aff_state,
            "affiant_zip": aff_zip,
            "w1_name": w1_name,
            "w1_address": w1_address,
            "w1_city": w1_city,
            "w1_county": w1_county,
            "w1_state": w1_state,
            "w1_zip": w1_zip,
            "w1_relationship": w1_relationship,
            "w1_years_known": w1_years,
            "w2_name": w2_name,
            "w2_address": w2_address,
            "w2_city": w2_city,
            "w2_county": w2_county,
            "w2_state": w2_state,
            "w2_zip": w2_zip,
            "w2_relationship": w2_relationship,
            "w2_years_known": w2_years,
            "marriages": marriages_data,
            "divorced": divorced == "Yes",
            "divorce_dates": divorce_dates,
            "remarriages": remarriages_data,
            "children": children_data,
            "deceased_children": deceased_children_data,
            "grandchildren": grandchildren_data,
            "had_will": had_will,
            "unpaid_debts": unpaid_debts,
            "unpaid_taxes": unpaid_taxes == "Yes",
            "property_description": "",
            "signing_county": death_county,
            "signing_day": "____",
            "signing_month_year": "__________, ______",
        }

    # -----------------------------------------------------------------------
    # Generate buttons
    # -----------------------------------------------------------------------
    btn_col1, btn_col2 = st.columns(2)

    with btn_col1:
        if st.button("Generate Affidavit PDF", type="primary", key="aoh_generate"):
            data = _build_aoh_data()
            if data:
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
