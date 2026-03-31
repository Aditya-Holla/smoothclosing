"""
ocr.py
------
OCR fallback for scanned/image-based pages.
Uses PyMuPDF to render pages to images and pytesseract to extract text.
pdf2image is kept as a whole-file fallback.
"""

import logging
from pathlib import Path

import fitz
import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)

# PSM 4 = single column of variable-size text (good for legal notices)
TESSERACT_CONFIG = "--psm 4 --oem 3"
# Render resolution — higher = more accurate OCR, slower
DPI = 300
SCALE = DPI / 72  # fitz uses 72 dpi baseline


def ocr_page(page: fitz.Page) -> str:
    """
    OCR a single fitz.Page object (already open in memory).
    Renders it to an image then runs pytesseract.
    """
    mat = fitz.Matrix(SCALE, SCALE)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    try:
        text = pytesseract.image_to_string(img, config=TESSERACT_CONFIG)
        logger.debug(f"  OCR page: extracted {len(text)} chars")
        return text
    except Exception as e:
        logger.warning(f"  OCR page failed: {e}")
        return ""


def ocr_pdf(pdf_path: Path) -> str:
    """
    OCR every page of a PDF file (whole-file fallback).
    Used when the PDF cannot be opened page-by-page.
    """
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        logger.error(f"  OCR: failed to open '{pdf_path.name}': {e}")
        return ""

    text_parts = []
    for i, page in enumerate(doc, start=1):
        page_text = ocr_page(page)
        text_parts.append(page_text)
        logger.debug(f"  OCR: page {i} → {len(page_text)} chars")

    doc.close()
    return "\n".join(text_parts)
