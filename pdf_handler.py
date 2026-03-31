"""
pdf_handler.py
--------------
Handles PDF loading and type detection (text-based vs. scanned/image-based).
Uses PyMuPDF (fitz) as the primary extraction engine.
"""

import logging
import os
from pathlib import Path
from typing import Tuple

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# A page with fewer than this many characters is treated as image-based
# and will be OCR'd. Set high enough to catch PDFs where only a stamp/
# overlay has text but the body content is an embedded image.
MIN_PAGE_TEXT_CHARS = 300


def get_pdf_paths(folder: str) -> list[Path]:
    """Return all PDF file paths in the given folder."""
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise ValueError(f"Input folder does not exist: {folder}")

    pdfs = sorted(folder_path.rglob("*.pdf"))
    logger.info(f"Found {len(pdfs)} PDF(s) in '{folder}' (including subdirectories)")
    return pdfs


def _page_needs_ocr(page: fitz.Page) -> bool:
    """Return True if a page has too little text to be considered text-based."""
    return len(page.get_text("text").strip()) < MIN_PAGE_TEXT_CHARS


def extract_text_from_pdf(pdf_path: Path) -> Tuple[str, bool]:
    """
    Extract raw text from a PDF file, page by page.

    Pages with sufficient embedded text are extracted directly.
    Pages with little/no text (image-based) are sent through OCR.

    Returns:
        (text, was_ocr_used)
    """
    from ocr import ocr_page

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        logger.error(f"Failed to open PDF '{pdf_path.name}': {e}")
        return "", False

    text_parts = []
    ocr_used = False
    page_count = len(doc)

    for i, page in enumerate(doc, start=1):
        page_text = page.get_text("text").strip()
        if len(page_text) >= MIN_PAGE_TEXT_CHARS:
            logger.debug(f"  Page {i}: text-based ({len(page_text)} chars)")
            text_parts.append(page_text)
        else:
            logger.info(f"  Page {i}: image-based ({len(page_text)} chars) → OCR")
            ocr_text = ocr_page(page)
            text_parts.append(ocr_text)
            ocr_used = True

    doc.close()

    mode = "[MIXED/OCR]" if ocr_used else "[TEXT]"
    logger.info(f"{mode} '{pdf_path.name}' → {page_count} page(s) processed")
    return "\n".join(text_parts), ocr_used
