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

    # 9. Run downstream enrichment pipeline
    enriched_csv = output_csv  # start with base leads.csv
    try:
        # Equity estimation
        from equity_estimator import run as run_equity
        equity_csv = output_csv.replace(".csv", "_with_equity.csv")
        logger.info("Running equity estimator...")
        run_equity(output_csv, equity_csv)
        enriched_csv = equity_csv
        logger.info(f"  → Equity estimates written to {equity_csv}")
    except Exception as e:
        logger.warning(f"Equity estimator failed ({e}), continuing without equity data.")

    try:
        # Skip trace (phone numbers)
        import asyncio
        from skipgenie import run as run_skip
        traced_csv = output_csv.replace(".csv", "_traced.csv")
        logger.info("Running skip trace...")
        asyncio.run(run_skip(enriched_csv, traced_csv, headless=True))
        enriched_csv = traced_csv
        logger.info(f"  → Skip trace results written to {traced_csv}")
    except Exception as e:
        logger.warning(f"Skip trace failed ({e}), continuing without phone data.")

    # 10. Read the final enriched CSV and push to Google Sheet
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if sheet_id:
        try:
            import csv as csv_mod
            from sheets_exporter import export_to_sheets

            # Read the enriched records
            enriched_records = []
            with open(enriched_csv, "r", encoding="utf-8") as f:
                reader = csv_mod.DictReader(f)
                for row in reader:
                    enriched_records.append(row)

            # Only push records that are in our new batch AND have an owner name
            # (leads without owner names are useless for calling)
            new_names = {(r["owner_name"].lower(), r["property_address"].lower()) for r in valid_records}
            new_enriched = [
                r for r in enriched_records
                if (r.get("owner_name", "").lower(), r.get("property_address", "").lower()) in new_names
                and r.get("owner_name", "").strip()  # skip blank owner names
            ]

            if new_enriched:
                export_to_sheets(new_enriched, sheet_id=sheet_id)
            else:
                logger.info("No leads with owner names to push to Google Sheet.")
        except Exception as e:
            logger.error(f"Google Sheets export failed: {e}")
            logger.info("CSV exports succeeded — enriched CSVs are up to date.")
    else:
        logger.info("GOOGLE_SHEET_ID not set — skipping Google Sheets export.")
        logger.info("Set GOOGLE_SHEET_ID and GOOGLE_SHEETS_CREDS env vars to enable.")

    # 11. SMS outreach to new leads
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
            logger.info("Running SMS outreach...")
            run_sms(
                input_csv=enriched_csv,
                output_csv=sms_csv,
                sender_name=os.environ.get("SENDER_NAME", ""),
            )
            logger.info(f"  → SMS results written to {sms_csv}")
        except Exception as e:
            logger.warning(f"SMS outreach failed ({e}), continuing.")
    else:
        logger.info("RingCentral credentials not set — skipping SMS outreach.")

    # 12. Update state
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
