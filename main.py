"""
main.py
-------
Entry point for the Texas Foreclosure Notice PDF → CSV pipeline.

Incremental mode (default):
  - Only processes PDFs not yet in pipeline_state.json
  - Appends new leads to Google Sheet (coworker's daily working doc)
  - Writes leads.csv for downstream scripts (equity, skip trace, SMS)

Usage:
    python main.py                          # incremental run
    python main.py --input ./input_pdfs --output leads.csv
    python main.py --rebuild-state          # bootstrap state from existing files
    python main.py --debug
"""

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from pdf_handler import get_pdf_paths, extract_text_from_pdf
from parser import parse_notice
from cleaner import clean_records
from exporter import export_to_csv
from state import (
    load_state, save_state, get_known_lead_keys, add_known_lead_keys,
    mark_processed, is_processed,
)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("pipeline.log", encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# Rebuild state from existing files (migration helper)
# ---------------------------------------------------------------------------

def rebuild_state(input_folder: str, output_csv: str) -> None:
    """Bootstrap pipeline_state.json from existing PDFs and leads.csv."""
    logger = logging.getLogger(__name__)
    state = load_state()

    input_dir = Path(input_folder)
    pdf_paths = get_pdf_paths(input_folder)

    # Mark all existing PDFs as downloaded + processed
    for pdf_path in pdf_paths:
        rel_key = str(pdf_path.relative_to(input_dir))
        state["downloaded_pdfs"][rel_key] = {
            "url": "unknown (pre-existing)",
            "downloaded_at": "unknown",
            "size_bytes": pdf_path.stat().st_size,
        }
        mark_processed(state, rel_key, records_extracted=-1)

    # Read existing leads.csv for known lead keys
    csv_path = Path(output_csv)
    if csv_path.exists():
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            keys = set()
            for row in reader:
                name = row.get("owner_name", "").strip().lower()
                addr = row.get("property_address", "").strip().lower()
                if name or addr:
                    keys.add((name, addr))
            add_known_lead_keys(state, keys)
            logger.info(f"Rebuild: imported {len(keys)} lead keys from {csv_path}")

    save_state(state)
    logger.info(f"Rebuild: marked {len(pdf_paths)} PDFs as downloaded+processed.")
    logger.info(f"Rebuild: state saved to pipeline_state.json")
    print(f"\n✓ State rebuilt: {len(pdf_paths)} PDFs, "
          f"{len(state['known_lead_keys'])} known leads")


# ---------------------------------------------------------------------------
# Travis County CSV ingestion
# ---------------------------------------------------------------------------

def _ingest_travis_csv(input_folder: str) -> list[dict]:
    """
    Read Travis County auction CSV and convert to lead records.
    Travis data comes from RealAuction (no PDFs), so we handle it separately.
    Note: Travis listings do NOT include owner names — those fields will be blank.
    """
    logger = logging.getLogger(__name__)
    travis_csv = Path(input_folder) / "travis" / "travis_foreclosures.csv"
    if not travis_csv.exists():
        return []

    records = []
    with open(travis_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = row.get("Property Address", "").strip()
            # Fix address formatting: "7908GOLDENROD CV" → "7908 GOLDENROD CV"
            addr = re.sub(r'(\d)([A-Za-z])', r'\1 \2', addr)

            records.append({
                "owner_name": "",  # RealAuction doesn't provide owner names
                "property_address": addr.upper(),
                "mailing_address": "",
                "filing_date": row.get("auction_date", ""),
                "sale_date": row.get("auction_date", ""),
                "lender": "Travis County Tax",
                "case_number": row.get("Cause Number", ""),
                "loan_amount": row.get("Est. Min. Bid", ""),
                "source_file": "travis_foreclosures.csv",
            })

    logger.info(f"Travis CSV: loaded {len(records)} auction listing(s)")
    return records


# ---------------------------------------------------------------------------
# Agent-powered downstream pipeline
# ---------------------------------------------------------------------------

async def _run_agents(enriched_csv: str, logger) -> None:
    """Delegate skip trace → sheets → SMS to the orchestrator agent."""
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
    from agents.orchestrator import build_options

    opts = build_options()

    prompt = (
        f"I just extracted new foreclosure leads into {enriched_csv}. "
        f"Please run the full downstream pipeline on ONLY this file:\n"
        f"1. Skip trace the leads in {enriched_csv} using skipgenie.py "
        f"(Mode 1 address-first). Output to leads_new_traced.csv\n"
        f"2. Push the traced leads to the Google Sheet\n"
        f"3. Send SMS via RingCentral (do a dry-run first, then send for real)\n"
        f"Do NOT re-trace leads that already have a phone_1 value. "
        f"Proceed without asking for confirmation — this is an automated pipeline run."
    )

    logger.info("Delegating to orchestrator agent: skip trace → sheets → SMS")
    async for message in query(prompt=prompt, options=opts):
        if isinstance(message, ResultMessage):
            if message.subtype == "success" and message.result:
                logger.info(f"Agent result: {message.result[:500]}")
            elif message.subtype != "success":
                logger.error(f"Agent error: {message.subtype}")
                raise RuntimeError(f"Agent failed: {message.subtype}")
            cost = getattr(message, "total_cost_usd", None)
            if cost is not None:
                logger.info(f"Agent cost: ${cost:.4f}")


def _run_direct_fallback(enriched_csv: str, valid_records: list, output_csv: str, logger) -> None:
    """Fallback: run skip trace, sheets, SMS directly if agents are unavailable."""
    import asyncio

    # Skip trace
    try:
        from skipgenie import run as run_skip
        traced_csv = enriched_csv.replace(".csv", "_traced.csv")
        logger.info("Running skip trace (direct fallback)...")
        asyncio.run(run_skip(enriched_csv, traced_csv, headless=True))
        enriched_csv = traced_csv
    except Exception as e:
        logger.warning(f"Skip trace failed ({e}), continuing without phone data.")

    # Sheets push
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if sheet_id:
        try:
            from sheets_exporter import export_to_sheets
            enriched_records = []
            with open(enriched_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    enriched_records.append(row)
            new_with_names = [r for r in enriched_records if r.get("owner_name", "").strip()]
            if new_with_names:
                export_to_sheets(new_with_names, sheet_id=sheet_id)
        except Exception as e:
            logger.error(f"Google Sheets export failed: {e}")

    # SMS
    rc_ready = all([
        os.environ.get("RC_CLIENT_ID"),
        os.environ.get("RC_CLIENT_SECRET"),
        os.environ.get("RC_JWT_TOKEN"),
        os.environ.get("RC_FROM_NUMBER"),
    ])
    if rc_ready:
        try:
            from ringcentral_sms import run as run_sms
            sms_csv = enriched_csv.replace(".csv", "_sms_sent.csv")
            run_sms(
                input_csv=enriched_csv,
                output_csv=sms_csv,
                sender_name=os.environ.get("SENDER_NAME", ""),
            )
        except Exception as e:
            logger.warning(f"SMS outreach failed ({e}), continuing.")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(input_folder: str, output_csv: str) -> None:
    logger = logging.getLogger(__name__)

    state = load_state()
    input_dir = Path(input_folder)

    # 1. Discover PDFs
    all_pdf_paths = get_pdf_paths(input_folder)
    if not all_pdf_paths:
        logger.warning("No PDF files found. Exiting.")
        return

    # 2. Filter to unprocessed PDFs only
    pdf_paths = [
        p for p in all_pdf_paths
        if not is_processed(state, str(p.relative_to(input_dir)))
    ]
    logger.info(f"{len(all_pdf_paths)} total PDFs, {len(pdf_paths)} new/unprocessed")

    if not pdf_paths:
        logger.info("No new PDFs to process. Everything is up to date.")
        return

    all_raw_records: list[dict] = []

    # 3. Process each new PDF
    for pdf_path in pdf_paths:
        logger.info(f"Processing: {pdf_path.name}")
        try:
            text, used_ocr = extract_text_from_pdf(pdf_path)
        except Exception as e:
            logger.error(f"  Failed to extract text from '{pdf_path.name}': {e}")
            continue

        if not text.strip():
            logger.warning(f"  No text extracted from '{pdf_path.name}' — skipping.")
            continue

        # 4. Parse notices from extracted text
        raw_records = parse_notice(text, source_file=pdf_path.name)
        all_raw_records.extend(raw_records)
        logger.info(f"  → {len(raw_records)} raw record(s) parsed from '{pdf_path.name}'")

    # Also ingest Travis County CSV (auction listings, no PDFs)
    travis_records = _ingest_travis_csv(input_folder)
    all_raw_records.extend(travis_records)

    logger.info(f"\nTotal raw records parsed: {len(all_raw_records)}")

    # 5. Clean + validate
    valid_records = clean_records(all_raw_records)

    # 5b. Recover garbage names + missing addresses via Claude vision on source PDFs
    try:
        from lead_recovery import recover_leads
        valid_records = recover_leads(valid_records, pdf_dir=input_folder)
    except Exception as e:
        logger.warning(f"Lead recovery failed ({e}), continuing with existing data.")

    # 6. Within-run dedup: keep first occurrence of each (owner_name, property_address)
    seen: set = set()
    deduped: list[dict] = []
    for r in valid_records:
        key = (r["owner_name"].lower(), r["property_address"].lower())
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    if len(deduped) < len(valid_records):
        logger.info(f"Within-run dedup: removed {len(valid_records) - len(deduped)} duplicate(s)")
    valid_records = deduped

    # 7. Cross-run dedup: skip leads already in the spreadsheet
    existing_keys = get_known_lead_keys(state)
    new_records = []
    new_keys = set()
    for r in valid_records:
        key = (r["owner_name"].lower(), r["property_address"].lower())
        if key not in existing_keys:
            new_records.append(r)
            new_keys.add(key)
    if len(valid_records) > len(new_records):
        logger.info(f"Cross-run dedup: {len(valid_records) - len(new_records)} already known")
    valid_records = new_records

    if not valid_records:
        logger.info("All parsed leads already exist. Nothing new to export.")
        # Still mark PDFs as processed
        for pdf_path in pdf_paths:
            rel_key = str(pdf_path.relative_to(input_dir))
            mark_processed(state, rel_key, records_extracted=0)
        save_state(state)
        return

    # 8. Export to CSV (base leads for downstream scripts)
    export_to_csv(valid_records, output_csv, append=True)

    # 9. Write new-only CSV for downstream agents (skip trace, SMS)
    new_leads_csv = output_csv.replace(".csv", "_new.csv")
    with open(new_leads_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=valid_records[0].keys())
        writer.writeheader()
        writer.writerows(valid_records)
    logger.info(f"Wrote {len(valid_records)} new lead(s) to {new_leads_csv}")

    # 10. Run downstream enrichment via agents (skip trace → sheets → SMS)
    enriched_csv = new_leads_csv
    try:
        # Equity estimation (direct call — fast, no LLM needed)
        from equity_estimator import run as run_equity
        equity_csv = new_leads_csv.replace(".csv", "_equity.csv")
        logger.info("Running equity estimator...")
        run_equity(new_leads_csv, equity_csv)
        enriched_csv = equity_csv
        logger.info(f"  → Equity estimates written to {equity_csv}")
    except Exception as e:
        logger.warning(f"Equity estimator failed ({e}), continuing without equity data.")

    # Delegate to agents for skip trace, sheets push, and SMS
    import asyncio
    try:
        asyncio.run(_run_agents(enriched_csv, logger))
    except Exception as e:
        logger.error(f"Agent pipeline failed: {e}")
        logger.info("Falling back to direct script calls...")
        _run_direct_fallback(enriched_csv, valid_records, output_csv, logger)

    # 11. Update state
    for pdf_path in pdf_paths:
        rel_key = str(pdf_path.relative_to(input_dir))
        count = sum(1 for r in valid_records if r.get("source_file") == pdf_path.name)
        mark_processed(state, rel_key, records_extracted=count)

    add_known_lead_keys(state, new_keys)
    save_state(state)

    logger.info(f"\n✓ {len(valid_records)} new lead(s) exported.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Texas Foreclosure Notice PDF → Leads Pipeline"
    )
    parser.add_argument(
        "--input",
        default="./input_pdfs",
        help="Folder containing foreclosure notice PDF files (default: ./input_pdfs)",
    )
    parser.add_argument(
        "--output",
        default="leads.csv",
        help="Output CSV file path (default: leads.csv)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging",
    )
    parser.add_argument(
        "--rebuild-state",
        action="store_true",
        help="Bootstrap pipeline_state.json from existing PDFs and leads.csv, then exit.",
    )
    args = parser.parse_args()

    setup_logging(debug=args.debug)
    logger = logging.getLogger(__name__)

    if args.rebuild_state:
        logger.info("Rebuilding pipeline state from existing files...")
        rebuild_state(input_folder=args.input, output_csv=args.output)
        return

    logger.info("=" * 60)
    logger.info("Texas Foreclosure Notice Pipeline — Starting")
    logger.info(f"  Input folder : {Path(args.input).resolve()}")
    logger.info(f"  Output CSV   : {Path(args.output).resolve()}")
    logger.info("=" * 60)

    run_pipeline(input_folder=args.input, output_csv=args.output)

    logger.info("Pipeline complete.")


if __name__ == "__main__":
    main()
