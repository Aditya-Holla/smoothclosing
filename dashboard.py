"""
dashboard.py
------------
Streamlit dashboard for the SmoothClosing foreclosure pipeline.

Run:
    streamlit run dashboard.py
"""

import asyncio
import csv
import io
import logging
import os
import re
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="SmoothClosing",
    page_icon="🏠",
    layout="wide",
)

st.title("🏠 SmoothClosing — Foreclosure Lead Pipeline")
st.caption("Upload foreclosure PDFs → extract leads → skip trace → text outreach.")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}

def normalize_date(value: str) -> str:
    if not value or not value.strip():
        return value
    v = value.strip()
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", v):
        m, d, y = v.split("/")
        return f"{int(m):02d}/{int(d):02d}/{y}"
    match = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", v)
    if match:
        mon, day, year = match.groups()
        mon_num = MONTH_MAP.get(mon.lower())
        if mon_num:
            return f"{mon_num}/{int(day):02d}/{year}"
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", v)
    if match:
        y, m, d = match.groups()
        return f"{m}/{d}/{y}"
    return v


def parse_dollar(value: str) -> float:
    cleaned = re.sub(r"[^\d.]", "", str(value))
    try:
        return float(cleaned) if cleaned else -1.0
    except ValueError:
        return -1.0


def equity_color(row: pd.Series) -> list[str]:
    equity = parse_dollar(row.get("estimated_equity", ""))
    if equity >= 100_000:
        bg = "background-color: #1a7a1a; color: white"
    elif equity >= 30_000:
        bg = "background-color: #a67c00; color: white"
    elif equity >= 0:
        bg = "background-color: #8b1a1a; color: white"
    else:
        bg = "background-color: #5a1a1a; color: #ccc"
    return [bg] * len(row)


DISPLAY_COLUMNS = [
    "owner_name", "property_address", "mailing_address",
    "filing_date", "sale_date", "lender", "loan_amount",
    "origination_year", "interest_rate_pct", "elapsed_months",
    "remaining_balance", "estimated_home_value", "estimated_equity",
    "equity_note", "needs_review",
]

DEFAULT_TEMPLATE = (
    "Hi {owner_first}, my name is {sender_name} and I'm a real estate investor. "
    "I came across your property at {property_address} and would love to make you "
    "a fair cash offer — no fees, no hassle. Would you be open to a quick chat? "
    "Reply STOP to opt out."
)

# ---------------------------------------------------------------------------
# Logging handler
# ---------------------------------------------------------------------------

class StreamlitLogHandler(logging.Handler):
    def __init__(self, container):
        super().__init__()
        self._lines = []
        self._container = container

    def emit(self, record):
        self._lines.append(self.format(record))
        self._container.code("\n".join(self._lines[-60:]), language=None)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Settings")

    st.markdown("**RentCast AVM**")
    run_equity = st.toggle("Enrich with RentCast AVM", value=False)
    rentcast_key = st.text_input("RentCast API Key", type="password",
                                 value=os.getenv("RENTCAST_API_KEY", ""),
                                 help="Overrides RENTCAST_API_KEY in .env")

    st.divider()
    st.markdown("**Skip Genie**")
    sg_user = st.text_input("Skip Genie Email", value=os.getenv("SKIPGENIE_EMAIL", ""))
    sg_pass = st.text_input("Skip Genie Password", type="password",
                            value=os.getenv("SKIPGENIE_PASSWORD", ""))
    sg_headless = st.toggle("Headless browser", value=True)

    st.divider()
    st.markdown("**RingCentral SMS**")
    rc_client_id     = st.text_input("RC Client ID",     type="password",
                                     value=os.getenv("RC_CLIENT_ID", ""))
    rc_client_secret = st.text_input("RC Client Secret", type="password",
                                     value=os.getenv("RC_CLIENT_SECRET", ""))
    rc_jwt           = st.text_input("RC JWT Token",     type="password",
                                     value=os.getenv("RC_JWT_TOKEN", ""))
    rc_from_number   = st.text_input("Your RC Phone Number", placeholder="+15551234567",
                                     value=os.getenv("RC_FROM_NUMBER", ""))
    sender_name      = st.text_input("Your Name (for SMS)", placeholder="John Smith",
                                     value=os.getenv("SENDER_NAME", ""))

    st.divider()
    st.markdown("**Equity color thresholds**")
    st.markdown("- 🟢 Green ≥ $100k\n- 🟡 Yellow $30k–$99k\n- 🔴 Red $0–$29k\n- ⬛ Dark = unknown")

# ---------------------------------------------------------------------------
# Run Full Pipeline
# ---------------------------------------------------------------------------

with st.expander("Run Full Pipeline (Download → Extract → Skip Trace)", expanded=False):
    st.caption("Runs all three steps in sequence. Individual tabs below still work independently.")

    pipeline_run_equity = st.checkbox("Include RentCast AVM (costs API credits)", value=False, key="pipeline_equity")

    run_all_btn = st.button("Run Full Pipeline", type="primary", key="run_all")

    if run_all_btn:
        # --- Credential validation ---
        missing = []
        if pipeline_run_equity and not rentcast_key and not os.getenv("RENTCAST_API_KEY"):
            missing.append("RentCast API Key")
        if not sg_user:
            missing.append("Skip Genie Email")
        if not sg_pass:
            missing.append("Skip Genie Password")

        if missing:
            st.error(f"Missing credentials in sidebar: **{', '.join(missing)}**")
        else:
            if rentcast_key:
                os.environ["RENTCAST_API_KEY"] = rentcast_key
            os.environ["SKIPGENIE_EMAIL"] = sg_user
            os.environ["SKIPGENIE_PASSWORD"] = sg_pass

            pipeline_ok = True
            pipeline_status = st.empty()

            # ====== STEP 1: Download PDFs ======
            pipeline_status.info("**Step 1/3** — Downloading foreclosure PDFs...")
            step1_log = st.empty()
            s1_handler = StreamlitLogHandler(step1_log)
            s1_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
            logging.getLogger().addHandler(s1_handler)

            try:
                from county_downloader import COUNTIES as CD

                out_path = Path("./input_pdfs")
                out_path.mkdir(parents=True, exist_ok=True)

                total_downloaded = 0
                s1_bar = st.progress(0, text="Starting downloads...")
                county_keys = list(CD.keys())
                for i, key in enumerate(county_keys):
                    info = CD[key]
                    s1_bar.progress((i + 1) / len(county_keys),
                                   text=f"Downloading {info['name']}...")
                    count = info["downloader"](out_path)
                    total_downloaded += count
                    time.sleep(1)
                s1_bar.empty()
                st.success(f"Step 1 complete — **{total_downloaded}** file(s) downloaded.")
            except Exception as e:
                st.error(f"Step 1 failed: {e}")
                pipeline_ok = False
            finally:
                logging.getLogger().removeHandler(s1_handler)

            # ====== STEP 2: Extract Leads ======
            if pipeline_ok:
                pipeline_status.info("**Step 2/3** — Extracting leads from PDFs...")
                step2_log = st.empty()
                s2_handler = StreamlitLogHandler(step2_log)
                s2_handler.setFormatter(logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
                logging.getLogger().addHandler(s2_handler)

                try:
                    from pdf_handler import get_pdf_paths, extract_text_from_pdf
                    from parser import parse_notice
                    from cleaner import clean_records
                    from state import load_state, save_state, is_processed, mark_processed, add_known_lead_keys

                    src_path = Path("./input_pdfs")
                    all_pdf_paths = get_pdf_paths(str(src_path))

                    # Filter to only unprocessed PDFs
                    pipe_state = load_state()
                    pdf_paths = [
                        p for p in all_pdf_paths
                        if not is_processed(pipe_state, str(p.relative_to(src_path)))
                    ]

                    skipped = len(all_pdf_paths) - len(pdf_paths)
                    if skipped:
                        logging.info(f"Skipping {skipped} already-processed PDF(s)")

                    if not pdf_paths:
                        if all_pdf_paths:
                            st.success(f"All {len(all_pdf_paths)} PDFs already processed. Nothing new to extract.")
                        else:
                            st.warning("No PDFs found in input_pdfs/. Nothing to extract.")
                        # Not an error — just nothing new. Load existing leads so skip trace can run.
                        pipeline_ok = True
                        existing_csv = Path("leads_with_equity.csv")
                        if existing_csv.exists():
                            _df = pd.read_csv(existing_csv)
                            st.session_state["leads_records"] = _df.to_dict(orient="records")
                            st.info(f"Loaded {len(_df)} existing leads for skip trace.")
                        else:
                            st.session_state["leads_records"] = []
                    else:
                        st.write(f"**{len(pdf_paths)}** new PDF(s) to process ({skipped} already done).")
                        all_raw: list[dict] = []
                        s2_bar = st.progress(0, text="Processing PDFs...")
                        for i, pdf_path in enumerate(pdf_paths):
                            s2_bar.progress((i + 1) / len(pdf_paths), text=f"{pdf_path.name}...")
                            raw = []
                            try:
                                text, _ = extract_text_from_pdf(pdf_path)
                            except Exception as e:
                                logging.warning(f"Could not read {pdf_path.name}: {e}")
                                rel_key = str(pdf_path.relative_to(src_path))
                                mark_processed(pipe_state, rel_key, records_extracted=0)
                                continue
                            if text.strip():
                                raw = parse_notice(text, source_file=pdf_path.name)
                                all_raw.extend(raw)
                            # Mark as processed
                            rel_key = str(pdf_path.relative_to(src_path))
                            mark_processed(pipe_state, rel_key, records_extracted=len(raw))
                        s2_bar.empty()

                        valid_records = clean_records(all_raw)

                        # Travis County CSV
                        travis_csvs = list(src_path.rglob("travis_foreclosures.csv"))
                        for tc in travis_csvs:
                            try:
                                tc_df = pd.read_csv(tc)
                                for _, row in tc_df.iterrows():
                                    addr = str(row.get("Property Address", "")).strip()
                                    if addr and addr.lower() != "nan":
                                        # Fix address formatting: "7908GOLDENROD" → "7908 GOLDENROD"
                                        import re as _re
                                        addr = _re.sub(r'(\d)([A-Za-z])', r'\1 \2', addr)
                                        valid_records.append({
                                            "owner_name": "",
                                            "property_address": addr,
                                            "mailing_address": "",
                                            "filing_date": str(row.get("auction_date", "")),
                                            "sale_date": str(row.get("auction_date", "")),
                                            "lender": "Travis County Tax",
                                            "loan_amount": str(row.get("Est. Min. Bid", "")),
                                            "source_file": tc.name,
                                            "notes": "owner name not found" if not addr else "owner name not found",
                                        })
                            except Exception as e:
                                logging.warning(f"Could not read Travis CSV {tc.name}: {e}")

                        if not valid_records:
                            st.error("No valid records extracted from PDFs.")
                            pipeline_ok = False
                        else:
                            # RentCast enrichment
                            if pipeline_run_equity:
                                from equity_estimator import calculate_equity
                                enriched = []
                                eq_bar = st.progress(0, text="RentCast enrichment...")
                                for i, rec in enumerate(valid_records):
                                    eq_bar.progress((i + 1) / len(valid_records),
                                                    text=f"RentCast {i+1}/{len(valid_records)}...")
                                    enriched.append(calculate_equity(rec))
                                eq_bar.empty()
                                valid_records = enriched

                            st.session_state["leads_records"] = valid_records

                            # Save state: mark leads as known to avoid reprocessing
                            new_keys = set()
                            for r in valid_records:
                                name = (r.get("owner_name") or "").strip().lower()
                                addr = (r.get("property_address") or "").strip().lower()
                                if name or addr:
                                    new_keys.add((name, addr))
                            add_known_lead_keys(pipe_state, new_keys)
                            save_state(pipe_state)

                            st.success(f"Step 2 complete — **{len(valid_records)}** lead(s) extracted.")
                except Exception as e:
                    st.error(f"Step 2 failed: {e}")
                    pipeline_ok = False
                finally:
                    logging.getLogger().removeHandler(s2_handler)

            # ====== STEP 3: Skip Trace (headless) ======
            if pipeline_ok:
                all_leads = st.session_state.get("leads_records", [])
                # Only trace leads with valid addresses AND equity >= 65% (if equity data available)
                records_to_trace = []
                skipped_low_equity = 0
                for r in all_leads:
                    addr = str(r.get("property_address", "")).strip()
                    if not addr or addr.lower() in ("", "nan", "none"):
                        continue
                    # Check equity percentage — skip trace if below 65%
                    eq_str = str(r.get("equity_pct", "")).strip().replace("%", "")
                    if eq_str and eq_str.lower() != "nan":
                        try:
                            if float(eq_str) < 65:
                                skipped_low_equity += 1
                                continue
                        except ValueError:
                            pass
                    records_to_trace.append(r)
                if skipped_low_equity:
                    st.info(f"Skipped **{skipped_low_equity}** lead(s) with equity below 65%.")
                if not records_to_trace:
                    st.info("No new leads with valid addresses to trace. Everything is up to date.")
                else:
                    pipeline_status.info(f"**Step 3/3** — Skip tracing {len(records_to_trace)} lead(s) (headless)...")
                    step3_log = st.empty()
                    s3_handler = StreamlitLogHandler(step3_log)
                    s3_handler.setFormatter(logging.Formatter(
                        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
                    logging.getLogger().addHandler(s3_handler)

                    try:
                        import asyncio
                        from skipgenie import run as run_skip

                        # Set env vars for skipgenie.py
                        os.environ["SKIPGENIE_USERNAME"] = sg_user
                        os.environ["SKIPGENIE_PASSWORD"] = sg_pass

                        input_path = Path("leads_tmp_trace.csv")
                        output_path = Path("leads_traced.csv")

                        fieldnames = list(records_to_trace[0].keys())
                        with open(input_path, "w", newline="", encoding="utf-8") as f:
                            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                            w.writeheader()
                            w.writerows(records_to_trace)

                        with st.spinner("Running Skip Genie (headless)... this may take several minutes."):
                            asyncio.run(run_skip(str(input_path), str(output_path), headless=True, max_relatives=6))

                        if output_path.exists():
                            traced_df = pd.read_csv(output_path)
                            st.session_state["traced_df"] = traced_df
                            st.session_state["traced_records"] = traced_df.to_dict(orient="records")
                            found = traced_df["phone_1"].astype(str).str.strip().replace("nan", "").ne("").sum()
                            st.success(f"Step 3 complete — **{found}/{len(traced_df)}** leads with phone numbers.")
                            st.dataframe(traced_df, use_container_width=True, height=400)
                            st.download_button(
                                "Download leads_traced.csv",
                                data=traced_df.to_csv(index=False).encode("utf-8"),
                                file_name="leads_traced.csv",
                                mime="text/csv",
                                type="primary",
                                key="pipeline_download_traced",
                            )
                        else:
                            st.warning("Skip trace completed but no output file found.")

                        # Clean up temp file
                        input_path.unlink(missing_ok=True)
                    except Exception as e:
                        st.error(f"Step 3 failed: {e}")
                        pipeline_ok = False
                    finally:
                        logging.getLogger().removeHandler(s3_handler)

            # ====== STEP 4: Push to Google Sheet ======
            if pipeline_ok:
                sheet_id = os.environ.get("GOOGLE_SHEET_ID")
                if sheet_id:
                    try:
                        from sheets_exporter import export_to_sheets
                        final_records = st.session_state.get("traced_records") or st.session_state.get("leads_records", [])
                        # Push all leads (address-first search can look up missing names)
                        pushable = [r for r in final_records if r.get("owner_name", "").strip() or r.get("property_address", "").strip()]
                        if pushable:
                            count = export_to_sheets(pushable, sheet_id=sheet_id)
                            st.success(f"Pushed **{count}** lead(s) to Google Sheet.")
                        else:
                            st.info("No leads with owner names to push to Google Sheet.")
                    except Exception as e:
                        st.warning(f"Google Sheets push failed: {e}")
                        logging.warning(f"Google Sheets push failed: {e}")

            # Final status
            if pipeline_ok:
                pipeline_status.success("Pipeline complete! Results are ready in the tabs below.")
            else:
                pipeline_status.warning("Pipeline stopped — check errors above.")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab0, tab1, tab2, tab3 = st.tabs(["🌐 Download PDFs", "📄 Extract Leads", "🔍 Skip Trace", "💬 SMS Outreach"])

# ===========================================================================
# TAB 0 — Download PDFs from county websites
# ===========================================================================

with tab0:
    st.subheader("Download Foreclosure PDFs from County Websites")

    from county_downloader import COUNTIES

    all_county_keys = list(COUNTIES.keys())
    selected_counties = st.multiselect(
        "Select counties to download",
        options=all_county_keys,
        default=all_county_keys,
        format_func=lambda k: COUNTIES[k]["name"],
    )

    output_dir = st.text_input("Output folder", value="./input_pdfs")
    download_btn = st.button("Download PDFs", type="primary", key="download_pdfs")

    if download_btn:
        if not selected_counties:
            st.warning("Select at least one county.")
        else:
            from county_downloader import COUNTIES as CD
            import time as _time

            dl_log = st.empty()
            dl_handler = StreamlitLogHandler(dl_log)
            dl_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
            logging.getLogger().addHandler(dl_handler)

            out_path = Path(output_dir)
            out_path.mkdir(parents=True, exist_ok=True)

            total = 0
            dl_bar = st.progress(0, text="Starting downloads…")
            try:
                for i, key in enumerate(selected_counties):
                    info = CD[key]
                    dl_bar.progress((i + 1) / len(selected_counties),
                                    text=f"Downloading {info['name']}…")
                    count = info["downloader"](out_path)
                    total += count
                    _time.sleep(1)
                dl_bar.empty()
                st.success(f"Done — **{total}** file(s) downloaded to `{out_path.resolve()}`")
                st.info("Now go to the **Extract Leads** tab to process them.")
            except Exception as e:
                st.error(f"Download failed: {e}")
            finally:
                logging.getLogger().removeHandler(dl_handler)

    # Show what's already in the folder
    st.divider()
    st.markdown("**Files currently in output folder**")
    check_dir = Path(output_dir) if Path(output_dir).exists() else Path("./input_pdfs")
    if check_dir.exists():
        pdfs = list(check_dir.rglob("*.pdf"))
        if pdfs:
            summary = {}
            for p in pdfs:
                county = p.parent.name if p.parent != check_dir else "root"
                summary[county] = summary.get(county, 0) + 1
            for county, cnt in sorted(summary.items()):
                st.write(f"- **{county}**: {cnt} PDF(s)")
        else:
            st.write("No PDFs found yet.")
    else:
        st.write("Folder doesn't exist yet.")


# ===========================================================================
# TAB 1 — Extract leads from PDFs
# ===========================================================================

with tab1:
    st.subheader("Step 1 — Load PDFs")

    src_mode = st.radio(
        "PDF source",
        ["Use downloaded folder (input_pdfs/)", "Upload files manually"],
        horizontal=True,
    )

    uploaded_files = []
    folder_src = "./input_pdfs"
    ready = False

    if src_mode == "Upload files manually":
        uploaded_files = st.file_uploader(
            "Select foreclosure notice PDFs",
            type="pdf",
            accept_multiple_files=True,
            key="pdf_upload",
        )
        if not uploaded_files:
            st.info("Upload at least one PDF to begin.")
        else:
            st.success(f"{len(uploaded_files)} file(s) uploaded.")
            ready = True
    else:
        folder_src = st.text_input("Folder path", value="./input_pdfs", key="folder_src")
        src_path = Path(folder_src)
        if not src_path.exists():
            st.warning("Folder not found.")
        else:
            pdf_count = len(list(src_path.rglob("*.pdf")))
            if pdf_count == 0:
                st.warning("No PDFs found in that folder. Run the Download step first.")
            else:
                st.success(f"Found **{pdf_count}** PDF(s) in `{src_path.resolve()}`")
                ready = True

    run_btn = st.button("Run Pipeline", type="primary", key="run_pipeline", disabled=not ready)

    if run_btn and ready:
        if rentcast_key:
            os.environ["RENTCAST_API_KEY"] = rentcast_key

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

        if uploaded_files:
            _tmp_ctx = tempfile.TemporaryDirectory()
            tmp_path = Path(_tmp_ctx.name)
            for uf in uploaded_files:
                (tmp_path / uf.name).write_bytes(uf.read())
        else:
            _tmp_ctx = None
            tmp_path = Path(folder_src)

        try:
            # --- Extract + clean ---
            log_box = st.empty()
            handler = StreamlitLogHandler(log_box)
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                                   datefmt="%H:%M:%S"))
            root_logger.addHandler(handler)
            valid_records = []

            try:
                from pdf_handler import get_pdf_paths, extract_text_from_pdf
                from parser import parse_notice
                from cleaner import clean_records

                pdf_paths = get_pdf_paths(str(tmp_path))
                all_raw: list[dict] = []

                bar = st.progress(0, text="Processing PDFs…")
                for i, pdf_path in enumerate(pdf_paths):
                    bar.progress((i + 1) / len(pdf_paths), text=f"{pdf_path.name}…")
                    try:
                        text, _ = extract_text_from_pdf(pdf_path)
                    except Exception as e:
                        logging.warning(f"Could not read {pdf_path.name}: {e}")
                        continue
                    if text.strip():
                        all_raw.extend(parse_notice(text, source_file=pdf_path.name))
                bar.empty()
                valid_records = clean_records(all_raw)
            finally:
                root_logger.removeHandler(handler)

            # --- Travis County CSV ---
            travis_csvs = list(tmp_path.rglob("travis_foreclosures.csv"))
            travis_leads = []
            for tc in travis_csvs:
                try:
                    tc_df = pd.read_csv(tc)
                    for _, row in tc_df.iterrows():
                        addr = str(row.get("Property Address", "")).strip()
                        if addr and addr.lower() != "nan":
                            travis_leads.append({
                                "owner_name": "",
                                "property_address": addr,
                                "mailing_address": "",
                                "filing_date": str(row.get("auction_date", "")),
                                "sale_date": str(row.get("auction_date", "")),
                                "lender": "Travis County Tax",
                                "loan_amount": str(row.get("Est. Min. Bid", "")),
                                "source_file": tc.name,
                                "notes": "owner name not found",
                            })
                except Exception as e:
                    logging.warning(f"Could not read Travis CSV {tc.name}: {e}")

            if travis_leads:
                valid_records = valid_records + travis_leads
                st.info(f"Added **{len(travis_leads)}** Travis County listing(s).")

            if not valid_records:
                st.error("No valid records extracted. Check that your PDFs contain foreclosure notices.")
            else:
                st.success(f"Extracted **{len(valid_records)}** lead(s).")

                # --- RentCast ---
                if run_equity:
                    st.markdown("**RentCast AVM enrichment…**")
                    eq_log = st.empty()
                    h2 = StreamlitLogHandler(eq_log)
                    h2.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                                      datefmt="%H:%M:%S"))
                    root_logger.addHandler(h2)
                    try:
                        from equity_estimator import calculate_equity
                        enriched = []
                        eq_bar = st.progress(0)
                        for i, rec in enumerate(valid_records):
                            eq_bar.progress((i + 1) / len(valid_records),
                                            text=f"RentCast {i+1}/{len(valid_records)}…")
                            enriched.append(calculate_equity(rec))
                        eq_bar.empty()
                        valid_records = enriched
                    finally:
                        root_logger.removeHandler(h2)
                    st.success("AVM enrichment complete.")

                # --- Normalize + sort ---
                df = pd.DataFrame(valid_records)
                for col in ("filing_date", "sale_date"):
                    if col in df.columns:
                        df[col] = df[col].apply(normalize_date)
                if "estimated_equity" in df.columns:
                    df["_eq"] = df["estimated_equity"].apply(parse_dollar)
                    df = df.sort_values("_eq", ascending=False).drop(columns=["_eq"])
                df = df.reset_index(drop=True)

                show_cols = [c for c in DISPLAY_COLUMNS if c in df.columns]
                df_display = df[show_cols]

                st.session_state["leads_df"] = df
                st.session_state["leads_records"] = df.to_dict(orient="records")

                # --- Preview ---
                st.subheader("Results")
                if "estimated_equity" in df_display.columns:
                    styled = df_display.style.apply(equity_color, axis=1)
                else:
                    styled = df_display.style
                st.dataframe(styled, use_container_width=True, height=480)

                # --- Download ---
                csv_buf = io.StringIO()
                writer = csv.DictWriter(csv_buf, fieldnames=show_cols, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(df_display.to_dict(orient="records"))
                st.download_button(
                    "Download leads_with_equity.csv",
                    data=csv_buf.getvalue().encode("utf-8"),
                    file_name="leads_with_equity.csv",
                    mime="text/csv",
                    type="primary",
                )
        finally:
            if _tmp_ctx:
                _tmp_ctx.cleanup()

# ===========================================================================
# TAB 2 — Skip Trace via Skip Genie
# ===========================================================================

with tab2:
    st.subheader("Step 2 — Skip Trace with Skip Genie")

    # Allow uploading a previously downloaded CSV instead of re-running extraction
    uploaded_leads_csv = st.file_uploader(
        "Or upload a leads CSV from a previous run",
        type="csv",
        key="leads_csv_upload",
    )

    if uploaded_leads_csv:
        df_trace = pd.read_csv(uploaded_leads_csv)
        st.session_state["leads_records"] = df_trace.to_dict(orient="records")
        st.success(f"Loaded {len(df_trace)} lead(s) from uploaded CSV.")

    records_to_trace = st.session_state.get("leads_records", [])
    if not records_to_trace:
        st.info("Run the Extract step first (Tab 1) or upload a leads CSV above.")
    else:
        st.write(f"**{len(records_to_trace)}** lead(s) ready to trace.")

        if not sg_user or not sg_pass:
            st.warning("Enter your Skip Genie credentials in the sidebar.")
        else:
            sg_limit = st.number_input("Max leads to trace (0 = all)", min_value=0, value=0, step=1)
            trace_btn = st.button("Run Skip Trace", type="primary", key="run_trace")

            if trace_btn:
                os.environ["SKIPGENIE_USERNAME"] = sg_user
                os.environ["SKIPGENIE_PASSWORD"] = sg_pass

                import asyncio
                from skipgenie import run as run_skip

                # Write leads to temp CSV for skipgenie.py
                input_path = Path("leads_tmp_trace.csv")
                output_path = Path("leads_traced.csv")

                # Only trace leads with valid addresses AND equity >= 65% (if available)
                traceable = []
                skipped_eq = 0
                for r in records_to_trace:
                    addr = str(r.get("property_address", "")).strip()
                    if not addr or addr.lower() in ("", "nan", "none"):
                        continue
                    eq_str = str(r.get("equity_pct", "")).strip().replace("%", "")
                    if eq_str and eq_str.lower() != "nan":
                        try:
                            if float(eq_str) < 65:
                                skipped_eq += 1
                                continue
                        except ValueError:
                            pass
                    traceable.append(r)
                if skipped_eq:
                    st.info(f"Skipped **{skipped_eq}** lead(s) with equity below 65%.")
                if sg_limit > 0:
                    traceable = traceable[:int(sg_limit)]

                fieldnames = list(traceable[0].keys()) if traceable else []
                with open(input_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(traceable)

                trace_log = st.empty()
                t_handler = StreamlitLogHandler(trace_log)
                t_handler.setFormatter(logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
                logging.getLogger().addHandler(t_handler)

                try:
                    with st.spinner("Running Skip Genie (headless)… this may take several minutes."):
                        asyncio.run(run_skip(str(input_path), str(output_path), headless=True, max_relatives=6))

                    if output_path.exists():
                        traced_df = pd.read_csv(output_path)
                        st.session_state["traced_df"] = traced_df
                        st.session_state["traced_records"] = traced_df.to_dict(orient="records")
                        found = traced_df["phone_1"].astype(str).str.strip().replace("nan", "").ne("").sum()
                        st.success(f"Skip trace complete — {found}/{len(traced_df)} leads with phones.")
                        st.dataframe(traced_df, use_container_width=True, height=400)
                        st.download_button("Download leads_traced.csv",
                                           data=traced_df.to_csv(index=False).encode("utf-8"),
                                           file_name="leads_traced.csv",
                                           mime="text/csv", type="primary")
                    else:
                        st.error("Output file not found — skip trace may have failed.")

                    input_path.unlink(missing_ok=True)
                except Exception as e:
                    st.error(f"Skip trace failed: {e}")
                finally:
                    logging.getLogger().removeHandler(t_handler)

# ===========================================================================
# TAB 3 — SMS Outreach via RingCentral
# ===========================================================================

with tab3:
    st.subheader("Step 3 — SMS Outreach via RingCentral")

    uploaded_traced_csv = st.file_uploader(
        "Or upload a traced leads CSV",
        type="csv",
        key="traced_csv_upload",
    )
    if uploaded_traced_csv:
        traced_df = pd.read_csv(uploaded_traced_csv)
        st.session_state["traced_records"] = traced_df.to_dict(orient="records")
        st.success(f"Loaded {len(traced_df)} traced lead(s).")

    traced_records = st.session_state.get("traced_records", [])

    if not traced_records:
        st.info("Complete the Skip Trace step first (Tab 2) or upload a traced CSV above.")
    else:
        # Count sendable — skip_genie_search outputs phone_1, phone_2, phone_3
        def _get_nums(r):
            # Owner: first valid phone only
            seen = set()
            nums = []
            for i in range(1, 4):
                num = str(r.get(f"phone_{i}", "")).strip()
                if num and num.lower() not in ("nan", ""):
                    nums.append(num)
                    seen.add(num)
                    break
            # Same-address relatives: first valid phone per relative, skip dupes
            for ri in range(1, 7):
                same_addr = str(r.get(f"rel_{ri}_same_address", "")).strip().lower()
                if same_addr in ("yes", "true", "1"):
                    for pi in range(1, 4):
                        num = str(r.get(f"rel_{ri}_phone_{pi}", "")).strip()
                        if num and num.lower() not in ("nan", "") and num not in seen:
                            nums.append(num)
                            seen.add(num)
                            break
            return nums

        # Filter out leads with equity below 65%
        def _passes_equity(r):
            eq_str = str(r.get("equity_pct", "")).strip().replace("%", "")
            if eq_str and eq_str.lower() != "nan":
                try:
                    return float(eq_str) >= 65
                except ValueError:
                    pass
            return True  # no equity data = allow

        sendable = [r for r in traced_records if _get_nums(r) and _passes_equity(r)]
        low_equity_skipped = sum(1 for r in traced_records if _get_nums(r) and not _passes_equity(r))
        total_nums = sum(len(_get_nums(r)) for r in sendable)
        if low_equity_skipped:
            st.info(f"Skipped **{low_equity_skipped}** lead(s) with equity below 65%.")
        st.write(f"**{len(sendable)}** leads with phone numbers — **{total_nums}** total numbers to text.")

        # Message templates — rotate randomly per lead
        from ringcentral_sms import TEMPLATES, _extract_street, pick_template
        st.markdown("**Message Templates** (one is picked randomly per lead)")
        st.caption("Placeholder: `{street}` (auto-extracted from address)")
        for idx, tmpl in enumerate(TEMPLATES, 1):
            st.text(f"{idx}. {tmpl}")

        use_custom = st.checkbox("Use a custom template instead", value=False)
        custom_template = None
        if use_custom:
            st.caption("Placeholders: `{street}`, `{owner_first}`, `{owner_name}`, `{property_address}`, `{sender_name}`")
            custom_template = st.text_area("Custom Template", value=TEMPLATES[0], height=120)

        # Preview
        if sendable:
            preview_rec = dict(sendable[0])
            preview_rec["sender_name"] = sender_name or "Your Name"
            preview_rec["street"] = _extract_street(preview_rec.get("property_address", ""))
            name_parts = (preview_rec.get("owner_name") or "").split()
            preview_rec["owner_first"] = name_parts[0].title() if name_parts else ""
            preview_tmpl = custom_template if use_custom else TEMPLATES[0]
            try:
                preview_msg = preview_tmpl.format(**{k: (v or "") for k, v in preview_rec.items()})
            except KeyError:
                preview_msg = preview_tmpl
            st.markdown(f"**Preview** (first lead):\n> {preview_msg}")

        dry_run = st.checkbox("Dry run (log only — don't actually send)", value=True)

        creds_ok = all([rc_client_id, rc_client_secret, rc_jwt, rc_from_number])
        if not creds_ok and not dry_run:
            st.warning("Enter RingCentral credentials in the sidebar before sending.")

        send_btn = st.button("Send SMS", type="primary", key="send_sms",
                             disabled=(not creds_ok and not dry_run))

        if send_btn:
            if rc_client_id:     os.environ["RC_CLIENT_ID"]     = rc_client_id
            if rc_client_secret: os.environ["RC_CLIENT_SECRET"] = rc_client_secret
            if rc_jwt:           os.environ["RC_JWT_TOKEN"]     = rc_jwt
            if rc_from_number:   os.environ["RC_FROM_NUMBER"]   = rc_from_number

            from ringcentral_sms import run as rc_run, render_message, get_access_token, send_sms

            sms_log = st.empty()
            s_handler = StreamlitLogHandler(sms_log)
            s_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
            logging.getLogger().addHandler(s_handler)

            results = []
            sent_count = 0
            fail_count = 0
            access_token = None

            try:
                if not dry_run:
                    access_token = get_access_token()

                sms_bar = st.progress(0, text="Sending…")
                for i, rec in enumerate(traced_records):
                    sms_bar.progress((i + 1) / len(traced_records),
                                     text=f"Lead {i+1}/{len(traced_records)}…")

                    owner = rec.get("owner_name", "Unknown")
                    name_parts = (owner or "").split()
                    rec["owner_first"] = name_parts[0].title() if name_parts else ""
                    rec["sender_name"] = sender_name or ""
                    rec["street"] = _extract_street(rec.get("property_address", ""))

                    active_tmpl = custom_template if use_custom else pick_template()
                    try:
                        message = active_tmpl.format(
                            **{k: (v or "") for k, v in rec.items()}
                        )
                    except KeyError:
                        message = active_tmpl
                    rec["sms_template"] = active_tmpl[:60] + "…"

                    all_nums = _get_nums(rec)

                    statuses = []
                    for num in all_nums:
                        if dry_run:
                            logging.info(f"[DRY RUN] → {owner} @ {num}: {message[:60]}…")
                            statuses.append(f"{num}:dry_run")
                        else:
                            try:
                                send_sms(access_token, rc_from_number, num, message)
                                logging.info(f"Sent → {owner} @ {num}")
                                statuses.append(f"{num}:sent")
                                sent_count += 1
                                time.sleep(1.5)
                            except Exception as e:
                                logging.error(f"Failed → {owner} @ {num}: {e}")
                                statuses.append(f"{num}:failed")
                                fail_count += 1

                    rec["sms_status"] = " | ".join(statuses) if statuses else "no_numbers"
                    results.append(rec)

                sms_bar.empty()

                if dry_run:
                    st.success(f"Dry run complete — {len([r for r in results if r.get('sms_status','') != 'no_numbers'])} leads would be texted.")
                else:
                    st.success(f"Done — {sent_count} sent, {fail_count} failed.")

                out_df = pd.DataFrame(results)
                st.dataframe(out_df, use_container_width=True, height=300)
                st.download_button(
                    "Download leads_sms_sent.csv",
                    data=out_df.to_csv(index=False).encode("utf-8"),
                    file_name="leads_sms_sent.csv",
                    mime="text/csv",
                    type="primary",
                )

            except Exception as e:
                st.error(f"SMS outreach failed: {e}")
            finally:
                logging.getLogger().removeHandler(s_handler)
