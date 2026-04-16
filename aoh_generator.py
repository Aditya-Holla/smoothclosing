"""
aoh_generator.py — Affidavit of Heirship PDF generator

Generates a formatted legal PDF matching the Brooks & Brooks, P.C. template.
Uses reportlab for PDF construction.

Usage:
    from aoh_generator import generate_aoh_pdf
    pdf_bytes = generate_aoh_pdf(data)
"""

import io
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOTARY_NAME = "JOSEPH N HERMOSA"
NOTARY_ID = "136078672"
NOTARY_EXP = "01-15-2027"

PERJURY_TEXT = (
    "I am aware of the penalties of perjury under Federal Law, which includes "
    "the execution of a false affidavit, pursuant to 18 U.S.C.S., Section 1621 "
    "wherein it is provided that anyone found guilty shall not be fined more than "
    "$2,000 or imprisoned not more than 5 years or both. I am also aware that "
    "perjury in the execution of a false affidavit is a criminal act pursuant to "
    "Section 37.02 of the Texas Penal Code. Finally I am also aware that under "
    "Section 32.46 of the Texas Penal Code, a person commits an offense, if with "
    "intent to defraud or harm a person, he, by deception, causes another to sign "
    "or execute any document affecting the property or service of the pecuniary "
    "interest of any person, and that an offense under such Section is a felony of "
    "the third degree which is punishable by a fine of $5,000 and confinement in "
    "the Texas Department of Corrections for a term of not more than 10 years or "
    "less than 2 years."
)

PLURAL_TEXT = (
    "When the context requires, singular nouns and pronouns include the plural. "
    "Additionally, this instrument may be executed in multiple counterparts and by "
    "different parties in separate counterparts, which, when taken together, shall "
    "constitute one original instrument."
)

# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

BODY = ParagraphStyle(
    "body",
    fontSize=11,
    leading=14,
    fontName="Times-Roman",
    spaceAfter=6,
)

BODY_BOLD = ParagraphStyle(
    "body_bold",
    parent=BODY,
    fontName="Times-Bold",
)

TITLE_STYLE = ParagraphStyle(
    "title",
    fontSize=13,
    leading=16,
    fontName="Times-Bold",
    alignment=1,  # center
    spaceAfter=18,
)

HEADER_STYLE = ParagraphStyle(
    "header",
    fontSize=11,
    leading=14,
    fontName="Times-Roman",
    spaceAfter=2,
)

INDENT_STYLE = ParagraphStyle(
    "indent",
    parent=BODY,
    leftIndent=36,
    spaceAfter=4,
)

SMALL_STYLE = ParagraphStyle(
    "small",
    fontSize=9,
    leading=11,
    fontName="Times-Roman",
)

SIG_STYLE = ParagraphStyle(
    "signature",
    fontSize=11,
    leading=14,
    fontName="Times-Roman",
    spaceAfter=0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b(text: str) -> str:
    """Wrap text in bold tags."""
    return f"<b>{text}</b>"


def _u(text: str) -> str:
    """Wrap text in underline tags."""
    return f"<u>{text}</u>"


def _para(text: str, style=None) -> Paragraph:
    return Paragraph(text, style or BODY)


def _spacer(pts: float = 12) -> Spacer:
    return Spacer(1, pts)


def _signature_block(name: str, county: str, day: str, month_year: str) -> list:
    """Generate a notary signature block for one signer."""
    elements = []
    # Signature line
    elements.append(_spacer(30))
    elements.append(Paragraph(
        f"____________________________________<br/>{_b(name)}",
        SIG_STYLE,
    ))
    elements.append(_spacer(18))

    # Notary section
    elements.append(Paragraph("THE STATE OF TEXAS\u2003\u2003\u2003\u00a7", HEADER_STYLE))
    elements.append(Paragraph("\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u00a7", HEADER_STYLE))
    elements.append(Paragraph(f"COUNTY OF {_u(county)}\u2003\u2003\u00a7", HEADER_STYLE))
    elements.append(_spacer(6))
    elements.append(_para(
        f"The foregoing instrument was SWORN TO AND SUBSCRIBED BEFORE ME on this the "
        f"{_b(day)} day of {month_year}, by {name}, to certify which witness my hand "
        f"and seal of office."
    ))
    elements.append(_spacer(6))
    elements.append(_para(f"{NOTARY_NAME}"))
    elements.append(_para(f"NOTARY PUBLIC, STATE OF TEXAS"))
    elements.append(_para(f"ID# {NOTARY_ID}"))
    elements.append(_para(f"COMM. EXP. {NOTARY_EXP}"))
    elements.append(_spacer(6))
    elements.append(Paragraph(
        "____________________________________<br/>Notary Public, State of Texas",
        SIG_STYLE,
    ))
    return elements


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate_aoh_pdf(data: dict) -> bytes:
    """Build an Affidavit of Heirship PDF and return it as bytes.

    Expected keys in *data*:
        # Decedent
        decedent_full_name, decedent_aka (str or ""), decedent_dob,
        death_date, death_city, death_county,
        residence_address, residence_city, residence_state, residence_zip,
        residence_county,

        # Affiant (family member)
        affiant_name, affiant_aka (str or ""),
        affiant_address, affiant_city, affiant_state, affiant_zip,
        affiant_relationship, affiant_years_known,

        # Witness 1
        w1_name, w1_address, w1_city, w1_state, w1_zip,
        w1_relationship, w1_years_known,

        # Witness 2
        w2_name, w2_address, w2_city, w2_state, w2_zip,
        w2_relationship, w2_years_known,

        # Marriage
        marriages: list[dict] with keys date, spouse_name
        divorced: bool, divorce_dates: list[str]
        remarriages: list[dict] with keys date, spouse_name  (or empty)

        # Children
        children: list[dict] with keys name, dob, relationship, other_parent

        # Deceased children
        deceased_children: list[dict] with keys name, death_date

        # Grandchildren of deceased children
        grandchildren: list[dict] with keys name, dob, parents

        # Additional
        had_will: bool,
        unpaid_debts: bool,
        unpaid_taxes: bool,

        # Property
        property_description: str,  # full legal description

        # Signing
        signing_county: str,
        signing_day: str,
        signing_month_year: str,   # e.g. "April, 2026"
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
        leftMargin=1 * inch,
        rightMargin=1 * inch,
    )

    elements: list = []

    # --- Title ---
    elements.append(Paragraph(_u("AFFIDAVIT OF HEIRSHIP"), TITLE_STYLE))
    elements.append(_spacer(6))

    # --- State / County header ---
    elements.append(Paragraph("THE STATE OF TEXAS\u2003\u2003\u2003\u00a7", HEADER_STYLE))
    elements.append(Paragraph(
        "\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u2003\u00a7"
        "\u2003\u2003KNOW ALL MEN BY THESE PRESENTS:",
        HEADER_STYLE,
    ))
    county = data.get("signing_county", data.get("death_county", ""))
    elements.append(Paragraph(f"COUNTY OF {county.upper()}\u2003\u2003\u2003\u00a7", HEADER_STYLE))
    elements.append(_spacer(12))

    # --- Parties ---
    affiant = data["affiant_name"]
    affiant_aka = data.get("affiant_aka", "")
    affiant_display = f"{affiant} A/K/A {affiant_aka}" if affiant_aka else affiant
    w1 = data["w1_name"]
    w2 = data["w2_name"]

    elements.append(_para(
        f"BEFORE ME, the undersigned authority, on this day personally appeared "
        f"{_b(affiant_display.upper())}, Affiant, and {_b(w1.upper())} and "
        f"{_b(w2.upper())}, Witnesses, who, being first duly sworn, upon their oaths, stated:"
    ))
    elements.append(_spacer(8))

    # --- Affiant paragraph ---
    decedent = data["decedent_full_name"]
    decedent_aka = data.get("decedent_aka", "")
    decedent_display = f"{decedent} A/K/A {decedent_aka}" if decedent_aka else decedent

    affiant_addr = f"{data['affiant_address']}, {data['affiant_city']}, {data['affiant_state']} {data['affiant_zip']}"
    elements.append(_para(
        f"My name is {_b(affiant_display.upper())}. I reside at {affiant_addr}. "
        f"I am personally familiar with the family and marital history of "
        f"{_b(decedent_display.upper())} (\"Decedent\"), and I have personal "
        f"knowledge of the facts stated in this affidavit. I was the "
        f"{data['affiant_relationship'].lower()} of the Decedent and knew Decedent for "
        f"more than {data['affiant_years_known']} years."
    ))
    elements.append(_spacer(8))

    # --- Witness 1 paragraph ---
    w1_addr = f"{data['w1_address']}, {data['w1_city']}, {data['w1_state']} {data['w1_zip']}"
    elements.append(_para(
        f"My name is {_b(w1.upper())}. I reside at {w1_addr}. I am personally "
        f"familiar with the family and marital history of Decedent, and I have personal "
        f"knowledge of the facts stated in this affidavit. I was a "
        f"{data['w1_relationship'].lower()} of the Decedent and knew Decedent for more "
        f"than {data['w1_years_known']} years."
    ))
    elements.append(_spacer(8))

    # --- Witness 2 paragraph ---
    w2_addr = f"{data['w2_address']}, {data['w2_city']}, {data['w2_state']} {data['w2_zip']}"
    elements.append(_para(
        f"My name is {_b(w2.upper())}. I reside at {w2_addr}. I am personally "
        f"familiar with the family and marital history of Decedent, and I have personal "
        f"knowledge of the facts stated in this affidavit. I was a "
        f"{data['w2_relationship'].lower()} of the Decedent and knew Decedent for more "
        f"than {data['w2_years_known']} years."
    ))
    elements.append(_spacer(12))

    # -----------------------------------------------------------------------
    # Numbered facts
    # -----------------------------------------------------------------------

    fact_num = 0

    # 1. Death info
    fact_num += 1
    residence_full = (
        f"{data['residence_address']}, {data['residence_city']}, "
        f"{data['residence_state']} {data['residence_zip']}"
    )
    elements.append(_para(
        f"{fact_num}. Decedent died on {data['death_date']} in {data['death_county']} County, "
        f"{data['death_city']}, Texas. At the time of Decedent's death, Decedent's residence was "
        f"{residence_full}, being located in {data['residence_county']} County, Texas.",
        INDENT_STYLE,
    ))
    elements.append(_spacer(8))

    # 2. Marital history
    fact_num += 1
    marriages = data.get("marriages", [])
    num_marriages = len(marriages)
    marriage_word = {0: "never married", 1: "married once", 2: "married twice", 3: "married three times"}.get(num_marriages, f"married {num_marriages} times")

    if num_marriages == 0:
        elements.append(_para(
            f"{fact_num}. Decedent {marriage_word}.",
            INDENT_STYLE,
        ))
    else:
        m = marriages[0]
        spouse_name = m["spouse_name"]
        # Build the AKA variants for the spouse if provided
        spouse_aka = m.get("spouse_aka", "")
        spouse_display = f"{spouse_name} a/k/a {spouse_aka}" if spouse_aka else spouse_name
        elements.append(_para(
            f"{fact_num}. Decedent was {marriage_word}. Decedent married {spouse_display} "
            f"on {m['date']} and remained married to {spouse_display} until the date of "
            f"Decedent's death as detailed above.",
            INDENT_STYLE,
        ))
    elements.append(_spacer(8))

    # 3. Children
    fact_num += 1
    children = data.get("children", [])
    if not children:
        elements.append(_para(
            f"{fact_num}. Decedent had no children.",
            INDENT_STYLE,
        ))
    else:
        other_parent = children[0].get("other_parent", "")
        other_parent_aka = children[0].get("other_parent_aka", "")
        parent_display = f"{other_parent} a/k/a {other_parent_aka}" if other_parent_aka else other_parent
        elements.append(_para(
            f"{fact_num}. Decedent had {_number_word(len(children))} "
            f"{'child' if len(children) == 1 else 'children'} born of Decedent's marriage to "
            f"{parent_display}, whose names and birth dates are as follows:",
            INDENT_STYLE,
        ))
        elements.append(_spacer(4))

        # Children table
        child_header = [
            Paragraph(_u("NAME"), INDENT_STYLE),
            Paragraph(_u("BIRTHDATE"), INDENT_STYLE),
        ]
        child_rows = [child_header]
        for c in children:
            child_rows.append([
                Paragraph(c["name"], INDENT_STYLE),
                Paragraph(c["dob"], INDENT_STYLE),
            ])
        t = Table(child_rows, colWidths=[3.5 * inch, 2.5 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 36),
        ]))
        elements.append(t)
    elements.append(_spacer(8))

    # 4. No other children
    fact_num += 1
    elements.append(_para(
        f"{fact_num}. Decedent did not have or adopt any other children, nor did "
        f"Decedent raise any other children or take any other children into Decedent's home.",
        INDENT_STYLE,
    ))
    elements.append(_spacer(8))

    # 5. Died intestate
    fact_num += 1
    elements.append(_para(
        f"{fact_num}. Decedent died intestate. No application to administer the estate "
        f"of Decedent has been filed to date. No need for the administration of Decedent's "
        f"estate exists, nor do any of the heirs of Decedent intend to file any application "
        f"to administer Decedent's estate.",
        INDENT_STYLE,
    ))
    elements.append(_spacer(8))

    # 6. No unpaid debts
    fact_num += 1
    elements.append(_para(
        f"{fact_num}. Decedent left no unpaid debts that remain unsatisfied. There are no "
        f"unpaid estate or inheritance taxes.",
        INDENT_STYLE,
    ))
    elements.append(_spacer(8))

    # 7. Estate value
    fact_num += 1
    elements.append(_para(
        f"{fact_num}. Decedent left an estate with a probable value of less than "
        f"$5,000,000.00 and did not make lifetime gifts that, when coupled with the value "
        f"of the estate for calculation of available unified tax credit available to "
        f"Decedent, would have subjected it to liability for federal estate tax purposes.",
        INDENT_STYLE,
    ))
    elements.append(_spacer(8))

    # 8. Property (only include if description provided)
    prop_desc = data.get("property_description", "")
    if prop_desc:
        fact_num += 1
        elements.append(_para(
            f"{fact_num}. Decedent owned an interest in that certain real property situated in "
            f"{data.get('residence_county', '')} County, Texas, being more particularly described as: "
            f"{prop_desc}",
            INDENT_STYLE,
        ))
        elements.append(_spacer(14))

    # --- Perjury notice ---
    elements.append(_para(PERJURY_TEXT))
    elements.append(_spacer(8))
    elements.append(_para(PLURAL_TEXT))
    elements.append(_spacer(8))

    # --- Signed effective ---
    day = data.get("signing_day", "____")
    month_year = data.get("signing_month_year", "__________, ______")
    elements.append(_para(
        f"SIGNED to be effective the {_b(day)} day of {month_year}, regardless of the "
        f"date or dates actually executed by the undersigned."
    ))

    # --- Signature blocks ---
    # Affiant
    elements += _signature_block(
        affiant_display.upper(),
        county.upper(),
        day,
        month_year,
    )

    # Witness 1
    elements += _signature_block(
        w1.upper(),
        county.upper(),
        day,
        month_year,
    )

    # Witness 2
    elements += _signature_block(
        w2.upper(),
        county.upper(),
        day,
        month_year,
    )

    # --- After Recording Return To ---
    elements.append(_spacer(30))
    elements.append(_para("After Recording Return To:"))
    elements.append(_para(f"{affiant_display}"))
    elements.append(_para(f"{data['affiant_address']}"))
    elements.append(_para(
        f"{data['affiant_city']}, {data['affiant_state']} {data['affiant_zip']}"
    ))

    # Build PDF
    doc.build(elements)
    return buf.getvalue()


def _number_word(n: int) -> str:
    """Convert small integers to words."""
    words = {
        0: "zero", 1: "one", 2: "two", 3: "three", 4: "four",
        5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    }
    return words.get(n, str(n))


# ---------------------------------------------------------------------------
# Questionnaire styles — mimic original form with "typed on" answers
# ---------------------------------------------------------------------------

# Form labels / question text — standard serif like the original
Q_FORM = ParagraphStyle(
    "q_form", fontSize=11, leading=15, fontName="Times-Roman", spaceAfter=4,
)
Q_FORM_BOLD = ParagraphStyle(
    "q_form_bold", parent=Q_FORM, fontName="Times-Bold",
)
Q_TITLE_FORM = ParagraphStyle(
    "q_title_form", fontSize=14, leading=18, fontName="Times-Bold",
    alignment=1, spaceAfter=14,
)
Q_INDENT = ParagraphStyle(
    "q_indent", parent=Q_FORM, leftIndent=36, spaceAfter=2,
)

# "Typed" answers — Courier to look like someone filled it in on a typewriter
Q_TYPED = ParagraphStyle(
    "q_typed", fontSize=11, leading=15, fontName="Courier", spaceAfter=2,
)
Q_TYPED_INDENT = ParagraphStyle(
    "q_typed_indent", parent=Q_TYPED, leftIndent=36, spaceAfter=2,
)

BLANK_LINE = "_" * 50
BLANK_SHORT = "_" * 22
BLANK_MED = "_" * 35


def _qf(text: str, style=None) -> Paragraph:
    return Paragraph(text, style or Q_FORM)


def _typed(text: str, style=None) -> Paragraph:
    """Render a value in Courier as if typed on the form."""
    return Paragraph(text, style or Q_TYPED_INDENT)


def _form_field(label: str, value: str, line_len: int = 50) -> Paragraph:
    """Label in form font, then underline with typed value sitting on it."""
    blank = "_" * line_len
    if value:
        # Show the label in Times, value in Courier on the line
        return Paragraph(
            f'{label}: <font face="Courier"><u>&nbsp;{value}&nbsp;</u></font>',
            Q_INDENT,
        )
    return Paragraph(f"{label}: {blank}", Q_INDENT)


def _two_fields(lbl1: str, val1: str, lbl2: str, val2: str) -> Paragraph:
    """Two fields side by side like  City: ___value___  County: ___value___"""
    def _fld(lbl, val):
        if val:
            return f'{lbl}: <font face="Courier"><u>&nbsp;{val}&nbsp;</u></font>'
        return f"{lbl}: {BLANK_SHORT}"
    return Paragraph(
        f"{_fld(lbl1, val1)}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{_fld(lbl2, val2)}",
        Q_INDENT,
    )


def _draw_checkboxes(taxes: bool):
    """Draw Yes/No with white boxes, X inside the checked one."""
    from reportlab.lib.colors import black, white
    from reportlab.platypus import Flowable

    class CheckboxRow(Flowable):
        def __init__(self, checked_yes: bool):
            super().__init__()
            self.checked_yes = checked_yes
            self.width = 300
            self.height = 18

        def draw(self):
            c = self.canv
            box_size = 10
            x_start = 120  # indent to align under question

            # "Yes" label
            c.setFont("Times-Roman", 11)
            c.drawString(x_start, 3, "Yes")

            # Yes checkbox
            bx = x_start + 28
            by = 2
            c.setStrokeColor(black)
            c.setFillColor(white)
            c.rect(bx, by, box_size, box_size, fill=1, stroke=1)
            if self.checked_yes:
                c.setFillColor(black)
                c.setFont("Times-Roman", 11)
                c.drawCentredString(bx + box_size / 2, by + 1.5, "x")

            # "No" label
            c.setFillColor(black)
            c.setFont("Times-Roman", 11)
            c.drawString(bx + box_size + 14, 3, "No")

            # No checkbox
            bx2 = bx + box_size + 32
            c.setStrokeColor(black)
            c.setFillColor(white)
            c.rect(bx2, by, box_size, box_size, fill=1, stroke=1)
            if not self.checked_yes:
                c.setFillColor(black)
                c.setFont("Times-Roman", 11)
                c.drawCentredString(bx2 + box_size / 2, by + 1.5, "x")

    return CheckboxRow(checked_yes=taxes)


def _blank_lines(count: int = 3) -> list:
    """Empty underlines for unfilled fields (matching original form)."""
    return [Paragraph(BLANK_LINE, Q_INDENT) for _ in range(count)]


# ---------------------------------------------------------------------------
# Questionnaire generator
# ---------------------------------------------------------------------------

def generate_questionnaire_pdf(data: dict) -> bytes:
    """Build a filled-out Affidavit of Heirship Questionnaire PDF
    that looks like someone typed answers onto the original blank form."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        leftMargin=1 * inch, rightMargin=1 * inch,
    )

    elements: list = []
    decedent = data.get("decedent_full_name", "")
    dob = data.get("decedent_dob", "")

    # --- Page header ---
    elements.append(Paragraph(
        f'DOB: {dob}', Q_FORM,
    ))
    elements.append(Paragraph(
        f'Decedent\'s Name: <font face="Courier"><u>&nbsp;{decedent}&nbsp;</u></font>',
        Q_FORM,
    ))
    elements.append(_spacer(10))
    elements.append(_qf("Affidavit of Heirship Questionnaire", Q_TITLE_FORM))
    elements.append(_spacer(4))

    # =======================================================================
    # Q1: Family member (Affiant)
    # =======================================================================
    elements.append(_qf(
        "1)&nbsp;&nbsp;&nbsp;Name and address of one family member familiar "
        "with the personal history of the Decedent."
    ))
    elements.append(_spacer(4))
    elements.append(_form_field("Name", data.get("affiant_name", "")))
    elements.append(_form_field("Address", data.get("affiant_address", "")))
    elements.append(_two_fields(
        "City", data.get("affiant_city", ""),
        "County", data.get("affiant_county", ""),
    ))
    elements.append(_two_fields(
        "State", data.get("affiant_state", ""),
        "Zip code", data.get("affiant_zip", ""),
    ))
    elements.append(_spacer(10))

    # =======================================================================
    # Q2: Witnesses
    # =======================================================================
    elements.append(_qf(
        "2)&nbsp;&nbsp;&nbsp;Names and addresses of two witnesses who are not "
        "related to (or at least have no interest in the property of) the "
        "Decedent. <b><u>Must have known decedent for at least 10 years</u></b>"
    ))
    elements.append(_spacer(6))

    for prefix in ("w1", "w2"):
        elements.append(_form_field("Name", data.get(f"{prefix}_name", "")))
        elements.append(_form_field("Address", data.get(f"{prefix}_address", "")))
        elements.append(_two_fields(
            "City", data.get(f"{prefix}_city", ""),
            "County", data.get(f"{prefix}_county", ""),
        ))
        elements.append(_two_fields(
            "State", data.get(f"{prefix}_state", ""),
            "Zip code", data.get(f"{prefix}_zip", ""),
        ))
        elements.append(_spacer(8))

    # =======================================================================
    # Q3: Relationships
    # =======================================================================
    elements.append(_qf(
        "3)&nbsp;&nbsp;&nbsp;Relationships of the witnesses and affiant to "
        "the Decedent. How long has each known the Decedent, and how did each "
        "know the Decedent?"
    ))
    elements.append(_spacer(4))
    elements.append(_form_field("Affiant", data.get("affiant_relationship", ""), 55))
    elements.append(_form_field("1st witness", data.get("w1_relationship", ""), 55))
    elements.append(_form_field("2nd witness", data.get("w2_relationship", ""), 55))
    elements.append(_spacer(10))

    # =======================================================================
    # Q4: Date of death
    # =======================================================================
    death_date = data.get("death_date", "")
    elements.append(Paragraph(
        f'4)&nbsp;&nbsp;&nbsp;Date the Decedent died.'
        f'<font face="Courier"><u>&nbsp;{death_date}&nbsp;</u></font>',
        Q_FORM,
    ))
    elements.append(_spacer(10))

    # =======================================================================
    # Q5: Where died
    # =======================================================================
    elements.append(_qf(
        "5)&nbsp;&nbsp;&nbsp;Where the Decedent died (city and county)."
    ))
    elements.append(_two_fields(
        "City", data.get("death_city", ""),
        "County", data.get("death_county", ""),
    ))
    elements.append(_spacer(10))

    # =======================================================================
    # Q6: Residential address at death
    # =======================================================================
    elements.append(_qf(
        "6)&nbsp;&nbsp;&nbsp;Decedent's residential address (city and county) "
        "at the time of his/her death."
    ))
    elements.append(_two_fields(
        "City", data.get("residence_city", ""),
        "County", data.get("residence_county", ""),
    ))
    elements.append(_spacer(4))
    elements.append(Paragraph(
        f'Decedent\'s Name: <font face="Courier"><u>&nbsp;{decedent}&nbsp;</u></font>',
        Q_FORM,
    ))
    elements.append(_spacer(10))

    # =======================================================================
    # Q7: Marital history
    # =======================================================================
    elements.append(_qf("7)&nbsp;&nbsp;&nbsp;Decedent's marital history:"))

    # 7a - Marriages
    elements.append(_qf(
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;a.&nbsp;&nbsp;Any and all dates "
        "Decedent got married and names of spouses to whom the Decedent "
        "married on each date (as specific as possible).",
    ))
    elements.append(_spacer(4))
    marriages = data.get("marriages", [])
    for i in range(max(len(marriages), 3)):
        if i < len(marriages):
            m = marriages[i]
            aka = m.get("spouse_aka", "")
            spouse = f"{m['spouse_name']} a/k/a {aka}" if aka else m["spouse_name"]
            elements.append(Paragraph(
                f'Date Married: <font face="Courier"><u>&nbsp;{m.get("date", "")}&nbsp;</u></font>'
                f'&nbsp;&nbsp;Name of Spouse: <font face="Courier"><u>&nbsp;{spouse}&nbsp;</u></font>',
                Q_INDENT,
            ))
        else:
            elements.append(Paragraph(
                f"Date Married: {BLANK_SHORT}&nbsp;&nbsp;Name of Spouse: {BLANK_MED}",
                Q_INDENT,
            ))
    elements.append(_spacer(6))

    # 7b - Divorces
    elements.append(_qf(
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;b.&nbsp;&nbsp;If divorced, any and "
        "all dates of divorces (as specific as possible).",
    ))
    elements.append(_spacer(4))
    divorce_dates = data.get("divorce_dates", [])
    if data.get("divorced") and divorce_dates:
        for dd in divorce_dates:
            elements.append(Paragraph(
                f'Date Divorced: <font face="Courier"><u>&nbsp;{dd}&nbsp;</u></font>',
                Q_INDENT,
            ))
    else:
        elements.append(Paragraph(
            f'Date Divorced: <font face="Courier"><u>&nbsp;N/A&nbsp;</u></font>',
            Q_INDENT,
        ))
    # Show blank lines for remaining slots
    for _ in range(2):
        elements.append(Paragraph(f"Date Divorced: {BLANK_SHORT}", Q_INDENT))
    elements.append(_spacer(6))

    # 7c - Remarriages
    elements.append(_qf(
        "C.&nbsp;&nbsp;Dates of any remarriages.",
    ))
    elements.append(_spacer(4))
    remarriages = data.get("remarriages", [])
    if remarriages:
        for rm in remarriages:
            aka = rm.get("spouse_aka", "")
            spouse = f"{rm['spouse_name']} a/k/a {aka}" if aka else rm["spouse_name"]
            elements.append(Paragraph(
                f'Date Married: <font face="Courier"><u>&nbsp;{rm.get("date", "")}&nbsp;</u></font>'
                f'&nbsp;&nbsp;Name of Spouse: <font face="Courier"><u>&nbsp;{spouse}&nbsp;</u></font>',
                Q_INDENT,
            ))
    else:
        elements.append(Paragraph(
            f'Date Married: {BLANK_SHORT}&nbsp;&nbsp;Name of Spouse: '
            f'<font face="Courier"><u>&nbsp;N/A&nbsp;</u></font>',
            Q_INDENT,
        ))
    for _ in range(2):
        elements.append(Paragraph(
            f"Date Married: {BLANK_SHORT}&nbsp;&nbsp;Name of Spouse: {BLANK_MED}",
            Q_INDENT,
        ))
    elements.append(_spacer(10))

    # =======================================================================
    # Q8, Q9, Q10 — Canvas-drawn lined forms matching the original exactly
    # =======================================================================
    from reportlab.platypus import Flowable as _Flowable

    class LinedFormSection(_Flowable):
        """Draws a form section with header labels, filled rows on lines,
        and blank underlines — exactly matching the original questionnaire."""

        def __init__(self, col_headers, col_widths, filled_rows, blank_count,
                     total_width=460, row_height=18):
            super().__init__()
            self.col_headers = col_headers
            self.col_widths = col_widths
            self.filled_rows = filled_rows
            self.blank_count = blank_count
            self.total_width = total_width
            self.row_height = row_height
            self.width = total_width
            self.height = row_height * (1 + len(filled_rows) + blank_count) + 4

        def draw(self):
            c = self.canv
            rh = self.row_height
            y = self.height - rh  # start from top

            # Header row — plain Times-Roman, no underlines
            x = 0
            c.setFont("Times-Roman", 10)
            for i, hdr in enumerate(self.col_headers):
                c.drawString(x, y + 4, hdr)
                x += self.col_widths[i]
            y -= rh

            # Filled rows — Courier text sitting on underlines
            for row_vals in self.filled_rows:
                x = 0
                for i, val in enumerate(row_vals):
                    w = self.col_widths[i]
                    # Draw the underline
                    c.setLineWidth(0.5)
                    c.line(x, y, x + w - 8, y)
                    # Draw the typed value on the line
                    if val:
                        c.setFont("Courier", 10)
                        c.drawString(x + 2, y + 3, val)
                    x += w
                y -= rh

            # Blank rows — just underlines
            for _ in range(self.blank_count):
                x = 0
                c.setLineWidth(0.5)
                for i in range(len(self.col_widths)):
                    w = self.col_widths[i]
                    c.line(x, y, x + w - 8, y)
                    x += w
                y -= rh

    # --- Q8: Children ---
    elements.append(_qf(
        "8)&nbsp;&nbsp;&nbsp;Decedent's history of children. Names and "
        "birthdates of children and their relationship to decedent."
    ))
    elements.append(_spacer(4))

    children = data.get("children", [])
    child_filled = []
    for c in children:
        child_filled.append([
            c.get("name", ""),
            c.get("dob", ""),
            c.get("other_parent", ""),
        ])
    elements.append(LinedFormSection(
        col_headers=["Full name", "Date of Birth", "Biological/Step/Adopted  Name of other Parent"],
        col_widths=[190, 100, 170],
        filled_rows=child_filled,
        blank_count=max(0, 8 - len(children)),
    ))
    elements.append(_spacer(10))

    # --- Q9: Deceased children ---
    elements.append(_qf(
        "9)&nbsp;&nbsp;&nbsp;Names of any deceased children of the decedent, "
        "and dates of their death."
    ))
    elements.append(_spacer(4))

    deceased = data.get("deceased_children", [])
    dc_filled = []
    for dc in deceased:
        dc_filled.append([
            dc.get("name", ""),
            dc.get("death_date", ""),
        ])
    elements.append(LinedFormSection(
        col_headers=["Full name", "Date of death"],
        col_widths=[280, 170],
        filled_rows=dc_filled,
        blank_count=max(0, 3 - len(deceased)),
    ))
    elements.append(_spacer(10))

    # --- Q10: Children of deceased children ---
    elements.append(_qf(
        "10)&nbsp;&nbsp;If any deceased child of the decedent had children, "
        "please provide the full names, and birthdates of said children, "
        "and names of their parents."
    ))
    elements.append(_spacer(4))

    grandchildren = data.get("grandchildren", [])
    gc_filled = []
    if grandchildren:
        for g in grandchildren:
            gc_filled.append([
                g.get("name", ""),
                g.get("dob", ""),
                g.get("parents", ""),
            ])
    else:
        gc_filled.append(["N/A", "", ""])
    elements.append(LinedFormSection(
        col_headers=["Full name", "Date of birth", "Names of parents"],
        col_widths=[180, 120, 160],
        filled_rows=gc_filled,
        blank_count=7,
    ))
    elements.append(_spacer(10))

    # =======================================================================
    # Q11: Will
    # =======================================================================
    will_text = data.get("had_will", "No") or "No"
    elements.append(_qf(
        "11)&nbsp;&nbsp;Did decedent have a Last Will and Testament? Was the "
        "Will probated? If he/she had a Will, please provide a copy."
    ))
    elements.append(Paragraph(
        f'<font face="Courier"><u>&nbsp;{will_text}&nbsp;</u></font>',
        Q_INDENT,
    ))
    elements += _blank_lines(2)
    elements.append(_spacer(10))

    # =======================================================================
    # Q12: Debts
    # =======================================================================
    debts_text = data.get("unpaid_debts", "No") or "No"
    elements.append(_qf(
        "12)&nbsp;&nbsp;Any debts that Decedent had when he/she died. Does "
        "he/she still have any unpaid debts?"
    ))
    elements.append(Paragraph(
        f'<font face="Courier"><u>&nbsp;{debts_text}&nbsp;</u></font>',
        Q_INDENT,
    ))
    elements += _blank_lines(2)
    elements.append(_spacer(10))

    # =======================================================================
    # Q13: Taxes
    # =======================================================================
    taxes = data.get("unpaid_taxes", False)
    elements.append(_qf(
        f"13)&nbsp;&nbsp;Any unpaid estate or inheritance taxes?"
    ))
    elements.append(_spacer(4))
    elements.append(_draw_checkboxes(taxes))
    elements.append(_spacer(14))

    # =======================================================================
    # Footer
    # =======================================================================
    elements.append(_qf(
        "If you have questions regarding this questionnaire, please contact the "
        "Law Offices of Brooks &amp; Brooks, P.C. Our phone number is (806) 371-3476.",
    ))
    elements.append(_spacer(6))
    elements.append(Paragraph(
        "**********Provide copy of death certificate of Decedent if available**********",
        Q_FORM,
    ))

    doc.build(elements)
    return buf.getvalue()
