"""
main.py
-------
Entry point for the Texas Foreclosure Notice PDF -> CSV pipeline.

Scope: PURE PARSER. This script parses PDFs and writes CSVs only.
It does NOT skip trace, push to Google Sheets, or send SMS. The
dashboard (dashboard.py) owns those steps as explicit user actions
so the team can choose who gets texted. Historical note: main.py
used to auto-run downstream via an orchestrator agent, which made
"only text new leads" impossible. See the DEPRECATED block below.

Incremental mode (default):
  - Discovers PDFs in ./input_pdfs/ and subfolders
  - Skips any PDF already in pipeline_state.json["processed_pdfs"]
  - Skips any (owner, address) already in known_lead_keys
  - Appends the survivors to leads.csv
  - Writes leads_new.csv: ONLY the leads added in this run
  - Runs equity on leads_new.csv -> leads_new_equity.csv

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

    # 10. Equity estimation (direct call, fast, no LLM needed).
    #     Writes leads_new_equity.csv. Skip trace / sheets / SMS are NOT
    #     triggered here anymore — the dashboard owns those steps explicitly
    #     so you can choose who gets texted. See the DEPRECATED block above.
    try:
        from equity_estimator import run as run_equity
        equity_csv = new_leads_csv.replace(".csv", "_equity.csv")
        logger.info("Running equity estimator...")
        run_equity(new_leads_csv, equity_csv)
        logger.info(f"  -> Equity estimates written to {equity_csv}")
    except Exception as e:
        logger.warning(f"Equity estimator failed ({e}), continuing without equity data.")

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
