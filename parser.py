"""
parser.py
---------
Parses raw text from Texas foreclosure notices and extracts structured fields.

Supported formats (confirmed against real PDFs):

  Format A — Vylla / Carrington (e.g. Burnet County)
    "WHEREAS, on [date], [NAME], as Grantor/Borrower, executed..."
    "Commonly known as: [ADDRESS]"
    "NOTICE IS HEREBY GIVEN, that on [DATE]"
    "TS#: [NUMBER]"

  Format B — Barrett Daffin / ECTX_NTSS (e.g. Williamson County)
    Numbered sections (1. Date... 3. Instrument... 4. Obligations...)
    "[NAME], grantor(s)" or "executed by [NAME], securing"
    "Date: [DATE]"
    "CLERK'S FILE NO. [NUMBER]"
    "[LENDER] is the current mortgagee"
    Property address appears BEFORE the NOTICE header (preamble)

  Format C — Miller George & Suggs / De Cubas & Lewis (e.g. Bell, Hays County)
    Labeled sections (Property:, Security Instrument:, Sale Information:, Obligation Secured:)
    "executed by [NAME] secures the repayment"
    "Sale Information: [DATE]"
    "Instrument Number [NUMBER]"
    "[LENDER], whose address is c/o ..."
    Property address and case number appear BEFORE the NOTICE header (preamble)

  Format D — Malcolm Cisneros / Trustee Corps
    "WHEREAS, on March 3, 2023, [NAME], AN UNMARRIED WOMAN, as Grantor"
    "TS No TX06000052-25-1"
    "payable to the order of [LENDER]"
    "that on Tuesday, June 3, 2025"

  Format E — McCarthy & Holthus
    "Grantor(s)/Mortgagor(s): NAME"
    "Current Beneficiary/Mortgagee: LENDER"
    "Instrument No: NNNN"
    "Date of Sale: MM/DD/YYYY"

  Format F — Codilis & Mood
    "with [NAME] as Grantor(s)"
    "Date of Sale: 01/06/2026"
    "C&MNo. 44-25-02183"

  Format G — labeled sections
    "Grantor(s): SHAWN NEILL"
    "Current Mortgagee: PNC BANK"
    "Amount: $145,590.00"
    "Document No. 2020067612"

  Format H — AVT Title
    "executed by [NAME], provides"
    "Date: 01/06/2026" (MM/DD/YYYY)

  Format I — April batch
    "Grantors: © AFORDAHOMES"
    "BORROWER: NAME" / "LENDER: NAME"
    "On April 7, 2026"
    "Original Principal Amount: $X"

  Format J — Planet Home Lending batch
    "Trustor(s): NAME\nNAME"
    "Property Address: 148 COPPER LN"
    "T.S. #: 26-17935"
    "Curent Beneficiary: Planet Home Lending"

To add a new county format: add regex patterns to the lists below in priority order.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex Patterns
# ---------------------------------------------------------------------------

# --- Owner / Grantor ---
OWNER_PATTERNS = [
    # Format McCarthy/Aldridge: "ORIGINAL MORTGAGOR: SPENCER M, GILLILAND, AN UNMARRIED MAN"
    r"ORIGINAL\s+MORTGAGOR[:\s]+([A-Z][A-Za-z\s.,&'-]{2,80}?)(?=,\s*(?:AN?\s+UNMARRIED|A\s+MARRIED|HUSBAND|WIFE|A\s+SINGLE)\b)",
    r"ORIGINAL\s+MORTGAGOR[:\s]+([A-Z][A-Za-z\s.,&'-]{2,80}?)(?=\n)",
    # Format A (numeric date): "WHEREAS, on 10/24/2014, Michelle Lee Hobbs, as Grantor/Borrower"
    r"WHEREAS,\s+on\s+\d{1,2}/\d{1,2}/\d{4},\s+([^,]+(?:,[^,]+)??)(?=,\s*(?:unmarried|married|a\s|an\s|as\s+Grantor))",
    r"WHEREAS,\s+on\s+\d{1,2}/\d{1,2}/\d{4},\s+([A-Za-z][A-Za-z\s.'-]{2,60}?),\s+(?:unmarried|married|husband|wife|as\s+Grantor)",
    # Format D (written-out date): "WHEREAS, on March 3, 2023, NAME, AN UNMARRIED WOMAN, as Grantor"
    r"WHEREAS,\s+on\s+\w+\s+\d{1,2},?\s+\d{4},\s+([^,]+(?:,[^,]+)??)(?=,\s*(?:AN?\s+UNMARRIED|A\s+MARRIED|as\s+Grantor|HUSBAND|WIFE))",
    # Format E: "Grantor(s)/Mortgagor(s):\nNAME" (labeled, next line)
    r"Grantor\(?s?\)?/Mortgagor\(?s?\)?[:\s\n]+([A-Z][A-Z\s.'-]{2,80}?)(?=\n\n|\nCurrent|\nDeed|\nOriginal|\nProperty|\nInstrument)",
    # Format April 8 labeled: "Grantor: ERA.CM. Holdings, LLC"
    r"^Grantor[:\s]+([A-Z][A-Za-z\s.,&'-]{2,80}?)(?=\n)",
    # Format G / 026: "Grantor(s): SHAWN NEILL" or "Grantor(s)si SHAWN NEILL" (OCR separator)
    # Separator: ONLY non-letter chars OR the specific 2-char OCR artifact "si"/"sl"
    # Negative lookahead blocks "grantor(s) and MERS" misfire
    r"Grantor\(?s?\)?(?!\s+and\b)(?:[:\s]{1,4}|s[il!:]\s*)([A-Z][A-Z\s.,'-]{4,60}?)(?=,|\n)",
    # Format I: "Grantors: © AFORDAHOMES" (© is OCR artifact)
    r"Grantors?[:\s]+©?\s*([A-Z][A-Z\s.,&'-]{2,80}?)(?=\n|,\s*(?:a\s+Texas|AN?\s+))",
    # Format I fallback: "BORROWER: NAME"
    r"BORROWER[:\s]+([A-Z][A-Z\s.,&'-]{2,80}?)(?=\n|,\s)",
    # Format J: "Trustor(s): CHRISTOPHER A HIEBERT AND Original MORTGAGE..."
    # Name ends at " Original/Current/Curent" on same line (allow _ OCR artifact) OR double newline
    r"Trustor\(?s?\)?[:\s]+([A-Z][A-Z\s,&.'-]{3,80}?)(?=[\s_]+(?:Original|Curent|Current|Beneficiary)|\n\n|\nLoan|\nProperty)",
    # Format C: "The Deed of Trust executed by CODY LYNN DODSON AND ROSARIO ESQUIVEL secures"
    # Also handles OCR typo "sccures", AVT "provides", "Deed of Trust or Contract Lien",
    # garbled OCR like "? res" (for "secures"), and A/K/A names
    r"(?:Deed\s+of\s+Trust(?:\s+or\s+Contract\s+Lien)?|Contract\s+Lien)\s+executed\s+by\s+©?\s*([A-Z][A-Za-z\s,&=./'-]{3,100}?)(?=,?\s*(?:provides|secures|sccures|securing|\?\s*r?es\b))",
    # Obligation Secured format: "Obligation Secured: The Deed of Trust executed by NAME secures/secure"
    r"Obligation\s+Secured[^.]{0,40}executed\s+by\s+([A-Z][A-Za-z\s&.,/'-]{4,80}?)(?=\s+secures?\b|\s+provides?\b|\n|\s{2,})",
    # Fournier / generic: "executed by NAME and recorded" or "executed by NAME, husband..."
    r"executed\s+by\s+([A-Z][A-Z\s&./'-]{4,80}?)(?=\s+and\s+recorded|\s*,\s*(?:husband|wife|securing|provides|\$))",
    # Format K: "a deed to Patrick Ryan Nelson and Jessica Nelson, as recorded"
    # Used in legal descriptions of rural/acreage properties (Burnet County etc.)
    # Require uppercase start to avoid matching lowercase legal description phrases
    r"a deed to ([A-Z][A-Za-z\s.'-]{4,80}?)(?=,\s*(?:as recorded|recorded)|\s+(?:as recorded|recorded))",
    # Format F (more specific — tried first): "with Conny Thibodeaux, a single woman as Grantor(s)"
    # Stop name at first comma (before status text) OR just before "as Grantor"
    # Require UPPERCASE start ([A-Z]) to prevent matching lowercase legal-description phrases
    # like "with the existing fence line" from property metes-and-bounds descriptions
    r"with\s+([A-Z][A-Za-z\s.'-]{2,60}?)(?=,|\s+as\s+Grantor)",
    # Format B: "JAVIER DELGADILLO {AND ELISSA M ANZURES, HUSBAND AND WIFE, grantor(s)"
    # Includes / for A/K/A names (e.g. "VERA C. JENNINGS A/K/A VERA JENNINGS")
    r"(?:with|by)\s+([A-Z][A-Z\s,{}&./'-]{5,100}?),?\s*(?:HUSBAND\s+AND\s+WIFE\s*,\s*)?grantor\(?s?\)?",
    # Format B section 4 / May batch: "Lien executed by NAME HUSBAND AND WIFE, securing"
    r"(?:Trust|Lien)\s+executed\s+by\s+([A-Z][A-Za-z\s,=./'-]{5,100}?)(?=,?\s*(?:HUSBAND\s+AND\s+WIFE,?\s+securing|securing|HUSBAND\s+AND\s+WIFE))",
    # Generic labeled fallback
    r"(?:Grantor|Borrower|Obligor)[:/\s]+([A-Z][A-Za-z\s.'-]{2,60}?)(?=\n|,\s*(?:a |an |as ))",
]

# --- Property Address ---
PROPERTY_ADDRESS_PATTERNS = [
    # Format A: "Commonly known as: 205 SAGEHILL DRIVE GRANITE SHOALS, TX 78654"
    r"Commonly\s+known\s+as[:\s]+\**([0-9][^\n*]{5,80}?)\**\s*\n",
    r"Commonly\s+known\s+as[:\s]+([0-9][^\n]{5,80})",
    # Formats J/E/G: "Property Address: 148 COPPER LN ..." or "Street Address: ..."
    r"(?:Property\s+Address|Street\s+Address)[:\s]+([0-9][^\n]{5,80})",
    # Codilis & Moody / Prestige: property address follows the TX case number "25TX373-..."
    r"25TX[\d]+-[\d]+\s+(\d{1,5}[^\n]{5,80}?(?:TX|Texas)[^\n]{1,10}\d{5})",
    # Single-line TX address (Formats B & C preamble when on one line)
    # Use [ \t]+ (not \s+) to prevent crossing newlines and grabbing attorney headers
    r"\b(\d{1,5}[ \t]+[A-Za-z][A-Za-z0-9 .,-]{5,60},[ \t]*(?:TX|Texas)[ \t]+\d{5}(?:-\d{4})?)\b",
    # OCR variant: period instead of comma before TX (e.g. "2319 Duntov Dr, TEMPLE. TX 76504")
    r"\b(\d{1,5}[ \t]+[A-Za-z][A-Za-z0-9 .,#-]{5,60}\.[ \t]*(?:TX|Texas)[ \t]+\d{5}(?:-\d{4})?)\b",
]

# --- Mailing Address ---
MAILING_ADDRESS_PATTERNS = [
    r"(?:Mailing\s+Address|Mail\s+To|Send\s+Notice\s+To)[:\s]+([^\n]{5,100})",
    r"(?:owner(?:'s)?\s+address)[:\s]+([^\n]{5,100})",
]

# --- Filing / Recording Date ---
FILING_DATE_PATTERNS = [
    # Format A: "Recorded on 10/30/2014"
    r"Recorded\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
    # Format B/C: "recorded on [date] as Instrument Number..."
    r"recorded\s+on\s+(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+(?:as\s+)?(?:Instrument|Document|Clerk)",
    r"(?:Date\s+of\s+Filing|Filed(?:\s+on)?)[:\s]+(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    r"WITNESS,\s+my\s+hand\s+this\s+(\d{1,2}/\d{1,2}/\d{4}|\w+\s+\d{1,2},?\s+\d{4})",
    # Format B: deed date as fallback filing reference
    r"(?:Deed\s+of\s+Trust|Contract\s+Lien)\s+dated\s+(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
]

# --- Sale / Auction Date ---
SALE_DATE_PATTERNS = [
    # Format A (numeric): "NOTICE IS HEREBY GIVEN, that on 4/7/2026"
    r"NOTICE\s+IS\s+HEREBY\s+GIVEN[^,]*,\s+that\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
    # Format D: "that on Tuesday, June 3, 2025" (weekday name before date)
    r"NOTICE\s+IS\s+HEREBY\s+GIVEN\s+that\s+on\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(\w+\s+\d{1,2},?\s+\d{4})",
    # Format B: "Date: January 06, 2026" (written-out month)
    r"^Date:\s+(\w+\s+\d{1,2},?\s+\d{4})",
    r"Date:\s+(\w+\s+\d{1,2},?\s+\d{4})",
    # Format H / F: "Date: 01/06/2026" or "Date of Sale: 01/06/2026" (MM/DD/YYYY)
    r"Date\s+of\s+Sale[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
    r"^Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
    r"Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
    # Format J: "Date: 6/2/2026"
    r"Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
    # Format C: "Sale Information: April 7, 2026, at 12:00 PM"
    r"Sale\s+Information[:\s]+(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})",
    # Format I: "On April 7, 2026;"
    r"(?<!\w)On\s+(\w+\s+\d{1,2},?\s+\d{4})[;,]",
    # Format E/G: "DATE: March 4, 2026"
    r"^DATE[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
    # Generic
    r"(?:Sale\s+Date|Trustee(?:'s)?\s+Sale\s+Date)[:\s]+(\d{1,2}/\d{1,2}/\d{4}|\w+\s+\d{1,2},?\s+\d{4})",
    r"will\s+sell[^.]{0,80}?on\s+(\d{1,2}/\d{1,2}/\d{4}|\w+\s+\d{1,2},?\s+\d{4})",
]

# --- Lender / Mortgagee ---
LENDER_PATTERNS = [
    # Format McCarthy: "CURRENT MORTGAGEE: LAKEVIEW LOAN SERVICING. LLC"
    r"CURRENT\s+MORTGAGEE[:\s]+([A-Z][A-Za-z0-9\s.,&'-]{2,80}?)(?=\n)",
    # Format A: "mortgage servicer for Carrington\nMortgage Services, LLC"
    # Negative lookahead blocks "for the/this/a/an mortgagee" false matches
    r"mortgage\s+servicer\s+for\s+(?!the\b|this\b|a\b|an\b|its\b)([A-Z][A-Za-z0-9 .,&'\-\n]{3,80}?)(?=,\s*(?:which\s+is|whose|a\s)|(?:\s*\n){2})",
    # Formats B & C: "[LENDER] is the current mortgagee"
    # Anchored to start of line (^) or after sentence-ending punctuation ([.!?])
    r"(?:^|[.!?])\s+([A-Z][A-Z\s.,&'-]{2,59}?)\s+is\s+the\s+current\s+mortgagee(?:\s+of\s+the\s+note)?",
    # Format E/J: "Current Beneficiary/Mortgagee: LENDER" or "Curent Beneficiary: LENDER" (OCR typo)
    r"Cu?rrent\s+(?:Beneficiary|Mortgagee)(?:/Mortgagee)?[:\s]+([A-Z][A-Za-z\s.,&'-]{2,80}?)(?=\n|,\s*(?:LLC|Inc|N\.A\.|Corp))",
    # Format G: "Current Mortgagee: PNC BANK"
    r"Current\s+Mortgagee[:\s]+([A-Z][A-Z\s.,&'-]{2,80}?)(?=\n)",
    # Format I labeled: "LENDER: NAME"
    r"^LENDER[:\s]+([A-Z][A-Z\s.,&'-]{2,80}?)(?=\n|,\s*a\s+loan|\s+according)",
    # Format April 8 labeled: "Mortgagee: Cynthia Koffman and James Koffman"
    r"^Mortgagee[:\s]+(?!=)([A-Za-z][A-Za-z0-9\s.,&'-]{2,80}?)(?=\n)",
    # Format D: "payable to the order of [LENDER]"
    r"payable\s+to\s+the\s+order\s+of\s+([A-Z][A-Za-z0-9\s.,&'-]{2,80}?)(?=\n|,|\s{2,})",
    # Format C: "FREEDOM MORTGAGE CORPORATION, whose address is c/o..."
    r"([A-Z][A-Z\s.,&'-]{2,59}?),\s+whose\s+address\s+is",
    # Generic labeled
    r"(?:Beneficiary|Mortgagee|Lender|Current\s+Holder)[:\s]+([A-Z][A-Za-z0-9 .,&'-]{3,80}?)(?=\n|\s{2,}|,\s*(?:its|a\s|LLC|Inc))",
    r"in\s+favor\s+of\s+([A-Z][A-Za-z0-9 .,&'-]{3,80}?)(?=\n|,|\s{2,})",
]

# --- Case / Instrument Number ---
CASE_NUMBER_PATTERNS = [
    # Format A: "TS#: 25-38206"
    r"TS#[:\s]*([\w-]{4,30})",
    # Format D/J: "TS No TX06000052-25-1" or "T.S. #: 26-17935"
    r"T\.?S\.?\s*(?:No\.?|#)[:\s]*([\w-]{4,30})",
    # Format B: "CLERK'S FILE NO. 2013082132"
    r"CLERK'?S?\s+FILE\s+NO\.?\s*([\w-]{4,30})",
    r"Document\s+CLERK'?S?\s+FILE\s+NO\.?\s*([\w-]{4,30})",
    # Format C: "Instrument Number 2018-39467"
    r"Instrument\s+Number\s+([\w-]{4,30})",
    # Format E: "Instrument No: 2023106342"
    r"Instrument\s+No\.?[:\s]*([\w-]{4,30})",
    # Format G: "Document No. 2020067612"
    r"Document\s+No\.?\s*([\w-]{4,30})",
    # Format F: "C&MNo. 44-25-02183"
    r"C&M\s*No\.?\s*([\w-]{4,30})",
    # Format C preamble: "22TX373-0706" or "25-05788" at top of notice
    r"^(\d{2}[A-Z]*\d*-\d{3,})\s*$",
    # Format K: "File No.: 2418076" (appears in Exhibit A of legal description PDFs)
    r"File\s+No\.?[:\s]*([\w-]{4,20})",
    # Generic
    r"(?:Case\s+No\.?|Cause\s+No\.?)[:\s]*([\w-]{4,30})",
    r"(?:Recording\s+No\.?)[:\s]*([\w-]{4,30})",
    r"Volume\s+(\d{6,12})",
]

# --- Loan / Note Amount ---
LOAN_AMOUNT_PATTERNS = [
    # Format A: "original amount of $93,367.00"
    r"original\s+amount\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)",
    # Format B: OCR may split "original principal\n$112,917.00" across lines,
    # and may omit the word "amount" entirely. Use [^$]{0,30} to cross newlines.
    r"original\s+principal[^$]{0,30}\$\s*([\d,]+(?:\.\d{2})?)",
    r"(?:original\s+(?:principal|note|loan)\s+amount|principal\s+sum)[^$]{0,50}\$\s*([\d,]+(?:\.\d{2})?)",
    # Format I/G: "Original Principal Amount: $X" or "Amount: $145,590.00"
    r"(?:Original\s+Principal\s+Amount|Amount)[:\s]+\$\s*([\d,]+(?:\.\d{2})?)",
    # Format C: "Note dated [date] in the amount of $245,471.00"
    r"in\s+the\s+amount\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)",
    # Generic
    r"indebtedness[^$]{0,60}\$\s*([\d,]+(?:\.\d{2})?)",
    r"promissory\s+note[^$\n]{0,60}\$\s*([\d,]+(?:\.\d{2})?)",
    # Last resort: "Loan Amount: $X" or "Note Amount: $X"
    r"(?:Loan|Note)\s+Amount[:\s]+\$\s*([\d,]+(?:\.\d{2})?)",
]

# --- Attorney / Substitute Trustee ---
ATTORNEY_PATTERNS = [
    # Labeled "Substitute Trustee:\n NAME" (Graham format — label on its own line)
    r"Substitute\s+Trustee:\s*\n\s*([A-Z][A-Za-z\s.,&'-]{2,80}?)(?=\n)",
    # Labeled "Substitute Trustee: NAME" (on same line, reject boilerplate words)
    r"Substitute\s+Trustee:\s+(?!under\b|need\b|shall\b|will\b|may\b|is\b|the\b|to\b|of\b|or\b)([A-Z][A-Za-z\s.,&'-]{2,80}?)(?=\n)",
    # "following Substitute Trustees:\n D. Wade Hayden, Michael W. Bitter"
    r"(?:Substitute\s+)?Trustee[s]?:\s*\n\s*([A-Z][A-Za-z\s.,&'-]{3,80}?)(?=\s*\n)",
    # "Appointment of Substitute Trustee ... conducted by ... :\n NAME, NAME"
    r"Appointment\s+of\s+Substitute\s+Trustee.*?:\s*\n\s*([A-Z][A-Za-z\s.,&'-]{3,80}?)(?=\s*\n)",
    # "By: [ATTORNEY NAME]" signature block (near end of document)
    r"(?:^|\n)\s*By[:\s]+/?[Ss]/?[:\s]*([A-Z][A-Za-z\s.,&'-]{3,60}?)(?=\s*\n|,\s*(?:Substitute|Trustee|Attorney))",
    # "Attorney for Mortgagee: FIRM NAME"
    r"Attorney\s+for\s+(?:Mortgagee|Beneficiary|Lender)[:\s]+([A-Z][A-Za-z\s.,&'-]{3,80}?)(?=\n)",
    # Known firm names that appear in headers/footers/signature blocks
    r"(Barrett\s+Daffin\s+Frappier[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(McCarthy\s+&?\s*Holthus[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(Codilis\s+&?\s*Mood[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(Hughes,?\s+Watters?\s+&?\s*Askanase[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(Mackie\s+Wolf\s+Zientz[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(De\s+Cubas\s+&?\s*Lewis[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(Miller\s+George\s+&?\s*Suggs[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(Malcolm\s+Cisneros[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(Aldridge\s+Pite[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(Trujillo\s+&?\s*Foster[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(AVT\s+Title\s+Services?[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
    r"(Jack\s+O'?Boyle\s+&?\s*Associates?[A-Za-z\s&.,]*?)(?=\n|\s{2,})",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_match(text: str, patterns: list[str]) -> Optional[str]:
    """Try each pattern in order; return the first captured group found."""
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


# Known attorney/servicer office addresses — must not be returned as property addresses
# Known attorney/servicer office addresses — must not be returned as property addresses
# Known attorney/servicer office addresses — must not be returned as property addresses
_ADDRESS_BLOCKLIST = re.compile(
    r'20405\s+S(?:tate|IAR)\s+Highway' # Codilis & Moody, P.C. (Houston) — OCR variant "SIAR"
    r'|1255\s+West\s+15th'             # McCarthy & Holthus, LLP (Plano)
    r'|1201\s+Louisiana'               # Hughes, Watters & Askanase (Houston)
    r'|1500\s+Dallas'                  # miscellaneous servicer office (Houston)
    r'|2727\s+Lyndon\s+B'              # Jack O'Boyle & Associates (Dallas)
    r'|5177\s+Richmond\s+Ave'          # AVT Title Services (Houston)
    r'|2499\s+S\.?\s+Capital'          # Mackie Wolf Zientz & Mann (Austin)
    r'|14160\s+Dallas\s+Parkway'       # Mackie Wolf Zientz & Mann (Dallas)
    r'|4400\s+Post\s+Oak'             # attorney office (Houston)
    r'|14800\s+Landmark'               # Trujillo & Foster (Dallas)
    r'|14841\s+Dallas\s+Parkway'       # AVT Title (Dallas)
    r'|3313\s+W\s+Commercial'          # De Cubas & Lewis (FL)
    r'|3333\s+Camino\s+Del\s+Rio'      # Aldridge Pite (San Diego)
    r'|6700\s+N\.?\s+New\s+Braunfels'  # attorney office (San Antonio)
    r'|7750\s+Broadway'                # attorney office (San Antonio area)
    r'|906\s+W\.?\s+McDermott'         # office address (Allen, TX)
    r'|1717\s+Main\s+St'              # attorney office (Dallas)
    r'|5926\s+Balcones'               # attorney office (Austin)
    r'|SUITE\s+\d+.*HOUSTON.*770\d{2}' # generic Houston attorney suites
    r'|SUITE\s+\d+.*DALLAS.*752\d{2}', # generic Dallas attorney suites
    re.IGNORECASE,
)


def _extract_address(block: str) -> Optional[str]:
    """
    Extract property address, with a multi-line fallback for OCR-scanned docs
    where the street and city/state/zip appear on separate lines.
    """
    # Try single-line patterns first
    result = _first_match(block, PROPERTY_ADDRESS_PATTERNS)
    if result and _ADDRESS_BLOCKLIST.search(result):
        result = None
    if result:
        return result

    # Multi-line fallback: find "CITY, TX 12345" and look upward for a street number
    lines = block.split('\n')
    for i, line in enumerate(lines):
        city_m = re.search(
            r'([A-Za-z][A-Za-z\s]+),\s*(TX|Texas)\s+(\d{5})\b', line, re.IGNORECASE
        )
        if city_m:
            # Use only the matched city/state/zip portion — not the whole line
            city_part = f"{city_m.group(1).strip()}, {city_m.group(2)} {city_m.group(3)}"
            for j in range(max(0, i - 4), i):
                # Include digits (e.g. "15th", "249") in the street name match
                street_m = re.search(r'(\d{1,5}\s+[A-Za-z][A-Za-z0-9\s.#-]{3,60})', lines[j])
                if street_m:
                    # Strip OCR barcode noise (long runs of digits mid-line)
                    street = re.sub(r'\s+\d{5,}\s*', ' ', street_m.group(1)).strip()
                    candidate = f"{street}, {city_part}"
                    if not _ADDRESS_BLOCKLIST.search(candidate):
                        return candidate
    return None


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------

# Compiled once — used by _split_into_notices and _is_foreclosure_notice
_NOTICE_HEADER = re.compile(
    r"^[ \t]*(?:NOTICE\s+OF\s+(?:SUBSTITUTE\s+)?TRUSTEE(?:'S)?\s+SALE\b"
    r"|SUBSTITUTE\s+TRUSTEE(?:'S)?\s+SALE\b"
    r"|NOTICE\s+OF\s+FORECLOSURE\s+SALE\b(?:\s+AND\s+APPOINTMENT\s+OF\s+SUBSTITUTE\s+TRUSTEE)?"
    r"|DEED\s+OF\s+TRUST\s+SALE\b)",
    re.IGNORECASE | re.MULTILINE,
)

# Secondary signals that confirm a document is foreclosure-related even without
# a clean header match (e.g. OCR garbled the header but body is clearly a notice)
_FORECLOSURE_SIGNALS = re.compile(
    r"(?:Grantor|Borrower|Mortgagor|Trustor|deed\s+of\s+trust|foreclosure\s+sale"
    r"|substitute\s+trustee|executed\s+by|WHEREAS,\s+on)",
    re.IGNORECASE,
)


def _is_foreclosure_notice(text: str) -> bool:
    """Return True if the text looks like a foreclosure notice, not an unrelated document."""
    if _NOTICE_HEADER.search(text):
        return True
    # Require at least 2 secondary signals to accept a headerless document
    matches = _FORECLOSURE_SIGNALS.findall(text)
    return len(matches) >= 2


def _split_into_notices(text: str) -> list[str]:
    """
    Split a document into individual notice blocks.

    For Formats B & C the property address and case number sit BEFORE the
    NOTICE header. We include up to 20 lines of "preamble" before each header
    so those fields are visible to the parser.
    """
    # ^ with re.MULTILINE requires the header to be at the START of a line.
    # This prevents matching "posted this Notice of Foreclosure Sale" in boilerplate
    # where "Notice" is in the middle of a sentence.
    matches = list(_NOTICE_HEADER.finditer(text))
    if not matches:
        return [text]

    blocks = []
    for idx, match in enumerate(matches):
        header_pos = match.start()
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)

        # Grab up to 20 lines before this header as preamble (captures address / case # / filing stamp)
        pre_text = text[:header_pos]
        pre_lines = pre_text.split('\n')
        preamble = '\n'.join(pre_lines[-20:])

        blocks.append(preamble + '\n' + text[header_pos:block_end])

    return blocks


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_notice(text: str, source_file: str) -> list[dict]:
    """
    Parse raw text from a Texas foreclosure notice PDF.
    Returns a list of raw (uncleaned) record dicts.
    Automatically skips non-foreclosure documents (admin PDFs, fee schedules, etc.).
    """
    if not _is_foreclosure_notice(text):
        logger.info(f"  Parser: skipping '{source_file}' — not a foreclosure notice")
        return []

    blocks = _split_into_notices(text)
    logger.info(f"  Parser: found {len(blocks)} notice block(s) in '{source_file}'")

    records = []
    for i, block in enumerate(blocks, start=1):
        record = _parse_block(block, source_file)
        records.append(record)
        logger.debug(
            f"  Parser: block {i} → owner='{record.get('owner_name')}' "
            f"addr='{record.get('property_address')}'"
        )

    return records


def _parse_block(block: str, source_file: str) -> dict:
    """Extract all fields from a single notice block."""
    return {
        "owner_name":       _first_match(block, OWNER_PATTERNS),
        "property_address": _extract_address(block),
        "mailing_address":  _first_match(block, MAILING_ADDRESS_PATTERNS),
        "filing_date":      _first_match(block, FILING_DATE_PATTERNS),
        "sale_date":        _first_match(block, SALE_DATE_PATTERNS),
        "lender":           _first_match(block, LENDER_PATTERNS),
        "case_number":      _first_match(block, CASE_NUMBER_PATTERNS),
        "loan_amount":      _first_match(block, LOAN_AMOUNT_PATTERNS),
        "attorney":         _first_match(block, ATTORNEY_PATTERNS),
        "source_file":      source_file,
    }
