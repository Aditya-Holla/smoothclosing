"""
lead_recovery.py
----------------
Uses Claude vision to read foreclosure PDFs and fix leads where the
OCR/regex parser produced garbage — missing addresses, garbled names, or both.

Usage:
    python lead_recovery.py --input leads.csv --output leads_recovered.csv
    python lead_recovery.py --input leads.csv --output leads_recovered.csv --pdf-dir ./input_pdfs
"""

import argparse
import base64
import csv
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import anthropic

logger = logging.getLogger(__name__)

# Names that are clearly OCR garbage or parser artifacts
GARBAGE_PATTERNS = [
    r'^(the|a|an|of|in|at|to|for|and|or|by|on|is|it|as|if|no|do)\b',  # starts with common word
    r'^(original|single|gonny|ee\b|i$)',  # known junk from this corpus
    r'^.{1,2}$',  # 1-2 chars
    r'\d{3,}',  # 3+ digits in a name
    r'(transfer|title|conclusion|sale|requirements|county|page \d|line of said)',
    r'(foreclosure|trustee|plaintiff|defendant|court|certificate|posted)',
]
GARBAGE_RE = re.compile('|'.join(GARBAGE_PATTERNS), re.IGNORECASE)


def is_garbage_name(name: str) -> bool:
    """Return True if the name looks like OCR garbage or a parser artifact."""
    name = name.strip()
    if not name:
        return True
    if GARBAGE_RE.search(name):
        return True
    # All caps single word under 4 chars that isn't a real name
    parts = name.split()
    if len(parts) == 1 and len(name) <= 3:
        return True
    # Contains unlikely character sequences
    if re.search(r"[^a-zA-Z\s\-'.,()/]", name):
        return True
    return False


RECOVERY_PROMPT = """\
This PDF contains one or more Texas foreclosure notices. I need you to find \
ONE SPECIFIC notice and extract data from it.

I will give you identifying details below so you can find the RIGHT notice. \
This PDF may have multiple notices — do NOT just grab the first one. Match \
using the address, case number, lender, or loan amount I provide.

Extract these TWO things from the MATCHING notice ONLY:

1. BORROWER/OWNER NAME — the person(s) being foreclosed on in that notice.
   - Look for "borrower", "grantor", "defendant", or the name right after \
"Notice of Trustee's Sale" / "Notice of Substitute Trustee's Sale"
   - Include all named persons (e.g. "John Smith and Jane Smith")
   - Do NOT include attorneys, trustees, lenders, or law firms
   - Do NOT return names from a different notice in the same PDF

2. PROPERTY ADDRESS — the property in that same notice.
   - Look for "property located at", "situated at", "commonly known as"
   - Street address, city, state, zip
   - Ignore mailing addresses, attorney addresses, or lender addresses

Respond in EXACTLY this format (two lines, nothing else):
NAME: John Smith And Jane Smith
ADDRESS: 123 Main Street, City, TX 78640

If you cannot find either one, use NOT FOUND for that field:
NAME: NOT FOUND
ADDRESS: NOT FOUND
"""


def _get_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


def _read_pdf_pages(pdf_path: Path) -> list[bytes]:
    """Read a PDF and return each page as a PNG image bytes."""
    import fitz  # PyMuPDF
    doc = fitz.open(str(pdf_path))
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        pages.append(pix.tobytes("png"))
    doc.close()
    return pages


def _find_pdf(source_file: str, pdf_base: Path) -> Path | None:
    """Locate a PDF by source_file name, checking subdirectories."""
    pdf_path = pdf_base / source_file
    if pdf_path.exists():
        return pdf_path
    candidates = list(pdf_base.rglob(source_file))
    return candidates[0] if candidates else None


def recover_from_pdf(pdf_path: Path, client: anthropic.Anthropic,
                     record: dict, needs_name: bool, needs_address: bool) -> dict:
    """Use Claude vision to extract name and/or address from a foreclosure PDF.
    Uses all available fields from the record to identify the correct notice.
    Returns {"name": str|None, "address": str|None}.
    """
    pages = _read_pdf_pages(pdf_path)
    if not pages:
        return {"name": None, "address": None}

    content = []
    for page_bytes in pages[:5]:
        b64 = base64.standard_b64encode(page_bytes).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        })

    # Build identification context from all available fields
    context_lines = ["IDENTIFYING DETAILS for the notice I need (use these to find the right one):"]
    addr = record.get("property_address", "").strip()
    if addr:
        context_lines.append(f"  Property address: {addr}")
    case = record.get("case_number", "").strip()
    if case:
        context_lines.append(f"  Case number: {case}")
    lender = record.get("lender", "").strip()
    if lender:
        context_lines.append(f"  Lender: {lender}")
    loan = record.get("loan_amount", "").strip()
    if loan:
        context_lines.append(f"  Loan amount: {loan}")
    attorney = record.get("attorney", "").strip()
    if attorney:
        context_lines.append(f"  Attorney/trustee: {attorney}")
    filing = record.get("filing_date", "").strip()
    if filing:
        context_lines.append(f"  Filing date: {filing}")
    sale = record.get("sale_date", "").strip()
    if sale:
        context_lines.append(f"  Sale date: {sale}")

    ocr_name = record.get("owner_name", "").strip()
    if ocr_name and needs_name:
        context_lines.append(f"  OCR read the borrower name as: \"{ocr_name}\" (likely garbled)")

    what_i_need = []
    if needs_name:
        what_i_need.append("the correct BORROWER NAME")
    if needs_address:
        what_i_need.append("the PROPERTY ADDRESS")
    context_lines.append(f"\nI need you to find: {' and '.join(what_i_need)}")

    context = "\n".join(context_lines)
    content.append({"type": "text", "text": f"{context}\n\n{RECOVERY_PROMPT}"})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": content}],
        )
        text = response.content[0].text.strip()

        result = {"name": None, "address": None}

        name_match = re.search(r'NAME:\s*(.+)', text)
        if name_match:
            val = name_match.group(1).strip().split("\n")[0].strip()
            if "NOT FOUND" not in val.upper():
                result["name"] = val

        addr_match = re.search(r'ADDRESS:\s*(.+)', text)
        if addr_match:
            val = addr_match.group(1).strip().split("\n")[0].strip()
            if "NOT FOUND" not in val.upper():
                result["address"] = val

        return result
    except Exception as e:
        logger.warning(f"  Claude vision failed for {pdf_path.name}: {e}")
        return {"name": None, "address": None}


def recover_leads(records: list[dict], pdf_dir: str = "./input_pdfs") -> list[dict]:
    """Fix records with missing addresses or garbage names by reading source PDFs.
    Modifies records in-place and returns the list.
    """
    pdf_base = Path(pdf_dir)

    needs_recovery = []
    for r in records:
        name = r.get("owner_name", "").strip()
        addr = r.get("property_address", "").strip()
        source = r.get("source_file", "")
        if not source:
            continue

        bad_name = is_garbage_name(name)
        missing_addr = not addr and bool(name)

        if bad_name or missing_addr:
            needs_recovery.append((r, bad_name, missing_addr))

    if not needs_recovery:
        logger.info("All records look clean — no recovery needed.")
        return records

    logger.info(f"Recovering {len(needs_recovery)} lead(s) with bad names or missing addresses...")

    # Group by source PDF to avoid reading the same PDF multiple times
    by_pdf: dict[str, list] = {}
    for r, bad_name, missing_addr in needs_recovery:
        src = r["source_file"]
        if src not in by_pdf:
            by_pdf[src] = []
        by_pdf[src].append((r, bad_name, missing_addr))

    try:
        client = _get_client()
    except RuntimeError as e:
        logger.warning(str(e))
        return records

    recovered_names = 0
    recovered_addrs = 0

    for source_file, group in by_pdf.items():
        pdf_path = _find_pdf(source_file, pdf_base)
        if not pdf_path:
            logger.warning(f"  PDF not found: {source_file}")
            continue

        # For batch PDFs with multiple leads, we make one call per lead
        # (each lead's name/address may be on different pages)
        for r, bad_name, missing_addr in group:
            name = r.get("owner_name", "").strip()
            addr = r.get("property_address", "").strip()
            issue = []
            if bad_name:
                issue.append(f"bad name '{name}'")
            if missing_addr:
                issue.append("missing address")
            logger.info(f"  {pdf_path.name}: {' + '.join(issue)}")

            result = recover_from_pdf(pdf_path, client, record=r,
                                      needs_name=bad_name,
                                      needs_address=missing_addr)

            notes = r.get("notes", "")

            if bad_name and result["name"]:
                logger.info(f"    Name: '{name}' → '{result['name']}'")
                r["owner_name"] = result["name"]
                recovered_names += 1
            elif bad_name:
                logger.info(f"    Name: '{name}' → could not recover")
                if notes:
                    notes += "; "
                notes += "name unreadable in PDF — needs manual review"

            if missing_addr and result["address"]:
                logger.info(f"    Address: → '{result['address']}'")
                r["property_address"] = result["address"].upper()
                recovered_addrs += 1
            elif missing_addr:
                logger.info(f"    Address: could not recover")
                if notes:
                    notes += "; "
                notes += "address not found in PDF — needs manual review"

            r["notes"] = notes

    logger.info(f"Recovery complete: {recovered_names} name(s), {recovered_addrs} address(es) fixed.")
    return records


def main():
    parser = argparse.ArgumentParser(description="Recover bad names and missing addresses from PDFs")
    parser.add_argument("--input", default="leads.csv")
    parser.add_argument("--output", default="leads_recovered.csv")
    parser.add_argument("--pdf-dir", default="./input_pdfs")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    with open(args.input, newline="", encoding="utf-8") as f:
        records = list(csv.DictReader(f))

    logger.info(f"Loaded {len(records)} record(s) from {args.input}")
    records = recover_leads(records, pdf_dir=args.pdf_dir)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)

    logger.info(f"Output → {args.output}")


if __name__ == "__main__":
    main()
