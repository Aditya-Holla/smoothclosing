"""
aoh_generator.py — Affidavit of Heirship + Questionnaire PDF generator.

Two main functions:
- generate_aoh_pdf(data) — builds the legal Affidavit of Heirship document
- generate_questionnaire_pdf(data) — builds the filled-out intake questionnaire

Built to match the actual lawyer template patterns observed across many real
affidavits (see /tmp/aoh_samples). Notary blocks are blank for hand-fill
(matching the firm's actual workflow); signature pages are page-broken;
multi-marriage / step-children / subsequent-death cases are supported.
"""

import io

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    Flowable,
)

# ---------------------------------------------------------------------------
# Constants — legal boilerplate (same in every affidavit)
# ---------------------------------------------------------------------------

PERJURY_TEXT = (
    "I am aware of the penalties of perjury under Federal Law, which includes "
    "the execution of a false affidavit, pursuant to 18 U.S.C.S., Section 1621 "
    "wherein it is provided that anyone found guilty shall not be fined more than "
    "$2,000 or imprisoned not more than 5 years or both. I am also aware that "
    "perjury in the execution of a false affidavit is a criminal act pursuant to "
    "Section 37.02 of the Texas Penal Code. Finally I am also aware that under "
    "Section 32.46 of the Texas Penal Code, a person commits an offense, if with "
    "intent to defraud or harm a person, he, by deception, causes another to sign "
    "or execute any document affecting property or service of the pecuniary "
    "interest of any person, and that an offense under such Section is a felony "
    "of the third degree which is punishable by a fine of $5,000 and confinement "
    "in the Texas Department of Corrections for a term of not more than 10 years "
    "or less than 2 years."
)

PLURAL_TEXT = (
    "When the context requires, singular nouns and pronouns include the plural. "
    "Additionally, this instrument may be executed in multiple counterparts and "
    "by different parties in separate counterparts, which, when taken together, "
    "shall constitute one original instrument."
)

# ---------------------------------------------------------------------------
# Styles — affidavit document
# ---------------------------------------------------------------------------

BODY = ParagraphStyle(
    "body", fontSize=11, leading=14, fontName="Times-Roman", spaceAfter=6,
)

BODY_BOLD = ParagraphStyle("body_bold", parent=BODY, fontName="Times-Bold")

TITLE_STYLE = ParagraphStyle(
    "title", fontSize=13, leading=16, fontName="Times-Bold",
    alignment=1, spaceAfter=18,
)

HEADER_STYLE = ParagraphStyle(
    "header", fontSize=11, leading=14, fontName="Times-Roman", spaceAfter=2,
)

INDENT_STYLE = ParagraphStyle(
    "indent", parent=BODY, leftIndent=36, spaceAfter=4,
)

SMALL_STYLE = ParagraphStyle(
    "small", fontSize=9, leading=11, fontName="Times-Roman",
)

SIG_STYLE = ParagraphStyle(
    "signature", fontSize=11, leading=14, fontName="Times-Roman", spaceAfter=0,
)

SIG_MARKER_STYLE = ParagraphStyle(
    "sig_marker", parent=BODY, alignment=1, fontName="Times-Italic",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b(text: str) -> str:
    return f"<b>{text}</b>"


def _u(text: str) -> str:
    return f"<u>{text}</u>"


def _para(text: str, style=None) -> Paragraph:
    return Paragraph(text, style or BODY)


def _spacer(pts: float = 12) -> Spacer:
    return Spacer(1, pts)


def _ordinal(n: int) -> str:
    """Day with ordinal suffix: 1st, 2nd, 3rd, 4th, ..., 17th, 21st, 22nd."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _possessive(pronoun: str) -> str:
    """her / his based on she / he."""
    return "her" if pronoun.lower() == "she" else "his"


def _number_word(n: int) -> str:
    words = {
        0: "zero", 1: "one", 2: "two", 3: "three", 4: "four",
        5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    }
    return words.get(n, str(n))


def _children_table(children: list) -> Table:
    """Build a NAME / BIRTHDATE table for a children list."""
    rows = [[
        Paragraph(_u("NAME"), INDENT_STYLE),
        Paragraph(_u("BIRTHDATE"), INDENT_STYLE),
    ]]
    for c in children:
        rows.append([
            Paragraph(c.get("name", ""), INDENT_STYLE),
            Paragraph(c.get("dob", ""), INDENT_STYLE),
        ])
    t = Table(rows, colWidths=[3.5 * inch, 2.5 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 36),
    ]))
    return t


def _signature_block(name_display: str, signer_label: str, month_year: str) -> list:
    """A blank notary signature block — county/state/day all blank for hand-fill.

    Matches the firm's actual signing workflow where the notary fills in
    these fields when the document is executed.
    """
    elements = []
    # Signature line
    elements.append(_spacer(48))
    elements.append(Paragraph(
        f"____________________________________<br/>{name_display.upper()}",
        SIG_STYLE,
    ))
    elements.append(_spacer(24))

    # Notary preamble (blank state/county for remote / out-of-state notaries)
    elements.append(Paragraph("THE STATE OF TEXAS   §", HEADER_STYLE))
    elements.append(Paragraph("                    §", HEADER_STYLE))
    elements.append(Paragraph(
        "COUNTY OF _______________  §", HEADER_STYLE,
    ))
    elements.append(_spacer(8))

    elements.append(_para(
        f"The foregoing instrument was SWORN TO AND SUBSCRIBED BEFORE ME on this "
        f"the _____ day of {month_year}, by {signer_label}, to certify which "
        f"witness my hand and seal of office."
    ))
    elements.append(_spacer(36))
    elements.append(Paragraph(
        "____________________________________<br/>Notary Public, State of Texas",
        SIG_STYLE,
    ))
    return elements


# ---------------------------------------------------------------------------
# Main affidavit generator
# ---------------------------------------------------------------------------

def generate_aoh_pdf(data: dict) -> bytes:
    """Build an Affidavit of Heirship PDF.

    See module docstring for the supported data keys. All optional fields fall
    back to sensible defaults so a minimal data dict still produces a valid PDF.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        leftMargin=1 * inch, rightMargin=1 * inch,
    )

    elements: list = []

    # ---- Setup -----------------------------------------------------------
    decedent_name = data["decedent_full_name"]
    decedent_aka = data.get("decedent_aka", "")
    decedent_display = (
        f"{decedent_name} A/K/A {decedent_aka}" if decedent_aka else decedent_name
    )
    decedent_pronoun = data.get("decedent_pronoun", "she")
    home_pronoun = "her" if decedent_pronoun.lower() == "she" else "his"

    affiant_name = data["affiant_name"]
    affiant_aka = data.get("affiant_aka", "")
    affiant_display = (
        f"{affiant_name} A/K/A {affiant_aka}" if affiant_aka else affiant_name
    )
    w1 = data["w1_name"]
    w2 = data["w2_name"]

    # ---- Title -----------------------------------------------------------
    elements.append(Paragraph(_u("AFFIDAVIT OF HEIRSHIP"), TITLE_STYLE))
    elements.append(_spacer(6))

    # ---- State / County header ------------------------------------------
    signing_county = data.get(
        "signing_county",
        data.get("death_county", data.get("residence_county", "")),
    )
    elements.append(Paragraph("THE STATE OF TEXAS   §", HEADER_STYLE))
    elements.append(Paragraph(
        "                  §   KNOW ALL MEN BY THESE PRESENTS:",
        HEADER_STYLE,
    ))
    elements.append(Paragraph(
        f"COUNTY OF {signing_county.upper()}   §", HEADER_STYLE,
    ))
    elements.append(_spacer(12))

    # ---- BEFORE ME parties ----------------------------------------------
    elements.append(_para(
        f"BEFORE ME, the undersigned authority, on this day personally appeared "
        f"{_b(affiant_display.upper())}, Affiant, and {_b(w1.upper())} and "
        f"{_b(w2.upper())}, Witnesses, who, being first duly sworn, upon their "
        f"oaths, stated:"
    ))
    elements.append(_spacer(8))

    # ---- Affiant intro paragraph ----------------------------------------
    aff_addr = (
        f"{data['affiant_address']}, {data['affiant_city']}, "
        f"{data.get('affiant_state', 'Texas')} {data['affiant_zip']}"
    )
    aff_verb = data.get("affiant_verb", "was")
    aff_article = data.get("affiant_article", "the")
    aff_relationship = data.get("affiant_relationship", "")
    aff_duration = data.get(
        "affiant_duration",
        f"more than {data.get('affiant_years_known', '')} years"
    )
    elements.append(_para(
        f"My name is {_b(affiant_display.upper())}. I reside at {aff_addr}. "
        f"I am personally familiar with the family and marital history of "
        f"{_b(decedent_display.upper())} (\"Decedent\"), and I have personal "
        f"knowledge of the facts stated in this affidavit. I {aff_verb} "
        f"{aff_article} {aff_relationship} of the Decedent and knew Decedent for "
        f"{aff_duration}."
    ))
    elements.append(_spacer(8))

    # ---- Witness 1 intro -------------------------------------------------
    w1_addr = (
        f"{data['w1_address']}, {data['w1_city']}, "
        f"{data.get('w1_state', 'Texas')} {data['w1_zip']}"
    )
    w1_duration = data.get("w1_duration") or (
        f"more than {data.get('w1_years_known', '')} years"
    )
    elements.append(_para(
        f"My name is {_b(w1.upper())}. I reside at {w1_addr}. I am personally "
        f"familiar with the family and marital history of Decedent, and I have "
        f"personal knowledge of the facts stated in this affidavit. "
        f"I {data.get('w1_verb', 'was')} {data.get('w1_article', 'a')} "
        f"{data.get('w1_relationship', '')} of the Decedent and knew Decedent for "
        f"{w1_duration}."
    ))
    elements.append(_spacer(8))

    # ---- Witness 2 intro -------------------------------------------------
    w2_addr = (
        f"{data['w2_address']}, {data['w2_city']}, "
        f"{data.get('w2_state', 'Texas')} {data['w2_zip']}"
    )
    w2_duration = data.get("w2_duration") or (
        f"more than {data.get('w2_years_known', '')} years"
    )
    elements.append(_para(
        f"My name is {_b(w2.upper())}. I reside at {w2_addr}. I am personally "
        f"familiar with the family and marital history of Decedent, and I have "
        f"personal knowledge of the facts stated in this affidavit. "
        f"I {data.get('w2_verb', 'was')} {data.get('w2_article', 'a')} "
        f"{data.get('w2_relationship', '')} of the Decedent and knew Decedent for "
        f"{w2_duration}."
    ))
    elements.append(_spacer(12))

    # =====================================================================
    # Numbered facts
    # =====================================================================
    fact_num = 0

    # ---- 1. Death + residence -------------------------------------------
    fact_num += 1
    death_county = data.get("death_county", "")
    death_state = data.get("death_state", "Texas")
    death_city = data.get("death_city", "")
    res_county = data.get("residence_county", "")
    res_state = data.get("residence_state", "Texas")

    # Death location: "in COUNTY County, Texas" for TX, otherwise "in CITY, STATE"
    if death_state.strip().lower() == "texas" and death_county:
        death_loc = f"in {death_county} County, Texas"
    elif death_city and death_state:
        death_loc = f"in {death_city}, {death_state}"
    elif death_state:
        death_loc = f"in {death_state}"
    else:
        death_loc = ""

    # Residence sentence — three formats observed:
    #   1. Texas with full address: "1346 Glenfalls, Houston, Texas 77049, being located in Harris County, Texas"
    #   2. Texas without street: "in Travis County, Texas"
    #   3. Out-of-state: "in Van Nuys, California"
    res_addr = data.get("residence_address", "")
    res_city = data.get("residence_city", "")
    res_zip = data.get("residence_zip", "")

    if res_state.strip().lower() != "texas":
        # Out-of-state residence
        if res_city:
            residence_clause = f"was located in {res_city}, {res_state}"
        else:
            residence_clause = f"was located in {res_state}"
    elif res_addr:
        # Full Texas address
        addr_pieces = [res_addr]
        if res_city:
            addr_pieces.append(res_city)
        if res_zip:
            addr_pieces.append(f"Texas {res_zip}")
        else:
            addr_pieces.append("Texas")
        residence_clause = (
            f"was {', '.join(addr_pieces)}"
            + (f", being located in {res_county} County, Texas"
               if res_county else "")
        )
    elif res_county:
        # Texas without specific street address
        residence_clause = (
            f"was located in {res_city + ', ' if res_city else ''}"
            f"{res_county} County, Texas"
        )
    else:
        residence_clause = "was located in Texas"

    elements.append(_para(
        f"{fact_num}. Decedent died on {data['death_date']} {death_loc}. "
        f"At the time of Decedent's death, Decedent's residence "
        f"{residence_clause}.",
        INDENT_STYLE,
    ))
    elements.append(_spacer(8))

    # ---- 2. Marital history --------------------------------------------
    fact_num += 1
    marriages = data.get("marriages", [])
    if data.get("never_married") or not marriages:
        elements.append(_para(
            f"{fact_num}. Decedent was never married.",
            INDENT_STYLE,
        ))
    else:
        n = len(marriages)
        count_word = {
            1: "once", 2: "twice", 3: "three times",
            4: "four times", 5: "five times",
        }.get(n, f"{n} times")
        sentences = [f"Decedent was married {count_word}."]
        for i, m in enumerate(marriages):
            spouse = m.get("spouse_name", "")
            aka = m.get("spouse_aka", "")
            spouse_disp = f"{spouse} a/k/a {aka}" if aka else spouse
            lead = "Decedent married" if i == 0 else "Decedent subsequently married"
            ended_by = m.get("ended_by", "death_decedent")
            if ended_by == "divorce":
                end = (
                    f"until that marriage was terminated pursuant to divorce on "
                    f"{m.get('end_date', '')}"
                )
            elif ended_by == "death_spouse":
                spouse_pronoun = m.get("spouse_pronoun", "his")
                end = (
                    f"until the date of {spouse_pronoun} death on "
                    f"{m.get('end_date', '')}"
                )
            else:  # death_decedent
                end = "until the date of Decedent's death as detailed above"
            sentences.append(
                f"{lead} {spouse_disp} on {m.get('date', '')} and remained "
                f"married to {spouse_disp} {end}."
            )
        elements.append(_para(
            f"{fact_num}. " + " ".join(sentences),
            INDENT_STYLE,
        ))
    elements.append(_spacer(8))

    # ---- Children groups (one fact per group) --------------------------
    child_groups = data.get("child_groups", [])
    # Backward compat: convert old flat children list to one group
    if not child_groups and data.get("children"):
        legacy = data["children"]
        if legacy:
            child_groups = [{
                "other_parent": legacy[0].get("other_parent", ""),
                "other_parent_aka": legacy[0].get("other_parent_aka", ""),
                "relationship_type": "marriage",
                "children": legacy,
            }]

    total_children_count = 0  # used later for "any other / any" wording

    for group in child_groups:
        kids = group.get("children", [])
        if not kids:
            continue
        total_children_count += len(kids)
        fact_num += 1
        op = group.get("other_parent", "")
        op_aka = group.get("other_parent_aka", "")
        op_disp = f"{op} a/k/a {op_aka}" if op_aka else op
        rel_type = group.get("relationship_type", "marriage")
        rel_phrase = {
            "marriage": "marriage to",
            "relationship": "relationship with",
            "relationship_and_marriage": "relationship with and marriage to",
        }.get(rel_type, "marriage to")
        n = len(kids)
        word = _number_word(n)
        child_word = "child" if n == 1 else "children"
        name_phrase = (
            "whose name and birth date are as follows"
            if n == 1 else "whose names and birth dates are as follows"
        )
        elements.append(_para(
            f"{fact_num}. Decedent had {word} {child_word} born of Decedent's "
            f"{rel_phrase} {op_disp}, {name_phrase}:",
            INDENT_STYLE,
        ))
        elements.append(_spacer(4))
        elements.append(_children_table(kids))
        elements.append(_spacer(8))

    # ---- Step children -------------------------------------------------
    for group in data.get("step_child_groups", []):
        kids = group.get("children", [])
        if not kids:
            continue
        total_children_count += len(kids)
        fact_num += 1
        spouse_name = group.get("spouse_name", "")
        prior_type = group.get("prior_relationship_type", "marriage")
        prior_phrase = (
            "marriage to" if prior_type == "marriage" else "relationship with"
        )
        prior_parent = group.get("prior_other_parent", "")
        n = len(kids)
        word = _number_word(n)
        child_word = "child" if n == 1 else "children"
        name_phrase = (
            "whose name and birth date are as follows"
            if n == 1 else "whose names and birth dates are as follows"
        )
        elements.append(_para(
            f"{fact_num}. Decedent took into {home_pronoun} home and helped raise "
            f"{word} {child_word} of {spouse_name}'s prior {prior_phrase} "
            f"{prior_parent}, {name_phrase}:",
            INDENT_STYLE,
        ))
        elements.append(_spacer(4))
        elements.append(_children_table(kids))
        elements.append(_spacer(8))

    # ---- Subsequent deaths of children ---------------------------------
    for d in data.get("subsequent_deaths", []):
        fact_num += 1
        name = d.get("name", "")
        ddate = d.get("death_date", "")
        ac = d.get("aoh_county", res_county)
        rel = d.get("relationship", "").strip()  # e.g. "daughter", "son"
        # "Decedent's daughter NAME" or just "NAME"
        if rel:
            subject = f"Decedent's {rel} {name}"
        else:
            subject = name
        elements.append(_para(
            f"{fact_num}. Subsequent to Decedent's death, {subject} died on "
            f"{ddate}. An Affidavit of Heirship for {name} is filed herewith "
            f"in the Official Public Records of {ac} County, Texas.",
            INDENT_STYLE,
        ))
        elements.append(_spacer(8))

    # ---- Free-form family facts (parents/siblings/survived-by) --------
    # Each entry is a complete sentence (or paragraph) inserted as its own
    # numbered fact. Used for things like "Decedent was survived by his
    # mother..." or "Decedent's father, X, died in 2010."
    for fact_text in data.get("family_facts", []) or []:
        if not fact_text:
            continue
        fact_num += 1
        elements.append(_para(
            f"{fact_num}. {fact_text}",
            INDENT_STYLE,
        ))
        elements.append(_spacer(8))

    # ---- "No (other) children" ----------------------------------------
    # Wording flips based on whether decedent had any children:
    #   no children at all  -> "any children"
    #   had children before -> "any other children"
    fact_num += 1
    if total_children_count == 0:
        elements.append(_para(
            f"{fact_num}. Decedent did not have or adopt any children, nor did "
            f"Decedent raise any children or take any children into "
            f"Decedent's home.",
            INDENT_STYLE,
        ))
    else:
        elements.append(_para(
            f"{fact_num}. Decedent did not have or adopt any other children, nor did "
            f"Decedent raise any other children or take any other children into "
            f"Decedent's home.",
            INDENT_STYLE,
        ))
    elements.append(_spacer(8))

    # ---- Died intestate ------------------------------------------------
    if data.get("died_intestate", True):
        fact_num += 1
        elements.append(_para(
            f"{fact_num}. Decedent died intestate. No application to administer "
            f"the estate of Decedent has been filed to date. No need for the "
            f"administration of Decedent's estate exists, nor do any of the "
            f"heirs of Decedent intend to file any application to administer "
            f"Decedent's estate.",
            INDENT_STYLE,
        ))
        elements.append(_spacer(8))

    # ---- No unpaid debts ----------------------------------------------
    if data.get("no_unpaid_debts", True):
        fact_num += 1
        elements.append(_para(
            f"{fact_num}. Decedent left no unpaid debts that remain unsatisfied. "
            f"There are no unpaid estate or inheritance taxes.",
            INDENT_STYLE,
        ))
        elements.append(_spacer(8))

    # ---- Estate value -------------------------------------------------
    fact_num += 1
    estate_value = data.get("estate_value", "$5,000,000.00")
    estate_value = estate_value.strip()
    if estate_value and not estate_value.startswith("$"):
        estate_value = "$" + estate_value
    elements.append(_para(
        f"{fact_num}. Decedent left an estate with a probable value of less "
        f"than {estate_value} and did not make lifetime gifts that, when coupled "
        f"with the value of the estate for calculation of available unified tax "
        f"credit available to Decedent, would have subjected it to liability for "
        f"federal estate tax purposes.",
        INDENT_STYLE,
    ))
    elements.append(_spacer(8))

    # ---- Property -----------------------------------------------------
    prop_desc = data.get("property_description", "")
    use_exhibit_a = data.get("use_exhibit_a", False)
    prop_county = data.get("property_county", res_county)
    if use_exhibit_a:
        fact_num += 1
        elements.append(_para(
            f"{fact_num}. Decedent owned an interest in that certain real "
            f"property situated in {prop_county} County, Texas, being more "
            f"particularly described on Exhibit \"A\" attached hereto and "
            f"incorporated herein for all purposes.",
            INDENT_STYLE,
        ))
        elements.append(_spacer(14))
    elif prop_desc:
        fact_num += 1
        elements.append(_para(
            f"{fact_num}. Decedent owned an interest in that certain real "
            f"property situated in {prop_county} County, Texas, being more "
            f"particularly described as: {prop_desc}",
            INDENT_STYLE,
        ))
        elements.append(_spacer(14))

    # ---- Perjury + plural notices -------------------------------------
    elements.append(_para(PERJURY_TEXT))
    elements.append(_spacer(8))
    elements.append(_para(PLURAL_TEXT))
    elements.append(_spacer(8))

    # ---- SIGNED to be effective --------------------------------------
    signing_day = data.get("signing_day")
    signing_month = data.get("signing_month", "")
    signing_year = data.get("signing_year", "")

    if signing_day not in (None, "", 0):
        try:
            day_str = _ordinal(int(signing_day))
        except (ValueError, TypeError):
            day_str = str(signing_day)
    else:
        day_str = "_____"

    if signing_month and signing_year:
        month_year = f"{signing_month}, {signing_year}"
    elif signing_month:
        month_year = f"{signing_month}, ______"
    elif signing_year:
        month_year = f"__________, {signing_year}"
    else:
        month_year = "__________, ______"

    elements.append(_para(
        f"SIGNED to be effective the {day_str} day of {month_year}, regardless "
        f"of the date or dates actually executed by the undersigned."
    ))
    elements.append(_spacer(20))

    # ---- "{Signatures appear on the following pages.}" marker --------
    elements.append(Paragraph(
        "{Signatures appear on the following pages.}", SIG_MARKER_STYLE,
    ))

    # ---- Signature pages — one per signer, page-broken --------------
    elements.append(PageBreak())
    elements += _signature_block(affiant_display, affiant_name, month_year)

    elements.append(PageBreak())
    elements += _signature_block(w1, w1, month_year)

    elements.append(PageBreak())
    elements += _signature_block(w2, w2, month_year)

    # ---- After Recording Return To ----------------------------------
    elements.append(_spacer(40))
    elements.append(_para("After Recording Return To:"))
    return_to_name = data.get("return_to_name") or affiant_display
    return_to_addr = data.get("return_to_address") or data.get("affiant_address", "")
    return_to_city = data.get("return_to_city") or data.get("affiant_city", "")
    return_to_state = (
        data.get("return_to_state") or data.get("affiant_state", "Texas")
    )
    return_to_zip = data.get("return_to_zip") or data.get("affiant_zip", "")
    elements.append(_para(return_to_name))
    elements.append(_para(return_to_addr))
    elements.append(_para(
        f"{return_to_city}, {return_to_state} {return_to_zip}"
    ))

    doc.build(elements)
    return buf.getvalue()


# ===========================================================================
# Questionnaire generator (unchanged from prior iteration)
# ===========================================================================

# Form labels / question text
Q_FORM = ParagraphStyle(
    "q_form", fontSize=11, leading=15, fontName="Times-Roman", spaceAfter=4,
)
Q_FORM_BOLD = ParagraphStyle("q_form_bold", parent=Q_FORM, fontName="Times-Bold")
Q_TITLE_FORM = ParagraphStyle(
    "q_title_form", fontSize=14, leading=18, fontName="Times-Bold",
    alignment=1, spaceAfter=14,
)
Q_INDENT = ParagraphStyle(
    "q_indent", parent=Q_FORM, leftIndent=36, spaceAfter=2,
)
Q_TYPED = ParagraphStyle(
    "q_typed", fontSize=11, leading=15, fontName="Courier", spaceAfter=2,
)
Q_TYPED_INDENT = ParagraphStyle("q_typed_indent", parent=Q_TYPED, leftIndent=36)

BLANK_LINE = "_" * 50
BLANK_SHORT = "_" * 22
BLANK_MED = "_" * 35


def _qf(text: str, style=None) -> Paragraph:
    return Paragraph(text, style or Q_FORM)


def _form_field(label: str, value: str, line_len: int = 50) -> Paragraph:
    blank = "_" * line_len
    if value:
        return Paragraph(
            f'{label}: <font face="Courier"><u>&nbsp;{value}&nbsp;</u></font>',
            Q_INDENT,
        )
    return Paragraph(f"{label}: {blank}", Q_INDENT)


def _two_fields(lbl1: str, val1: str, lbl2: str, val2: str) -> Paragraph:
    def _fld(lbl, val):
        if val:
            return f'{lbl}: <font face="Courier"><u>&nbsp;{val}&nbsp;</u></font>'
        return f"{lbl}: {BLANK_SHORT}"
    return Paragraph(
        f"{_fld(lbl1, val1)}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{_fld(lbl2, val2)}",
        Q_INDENT,
    )


def _draw_checkboxes(taxes: bool):
    """Yes/No with white boxes, X inside the checked one."""
    from reportlab.lib.colors import black, white

    class CheckboxRow(Flowable):
        def __init__(self, checked_yes: bool):
            super().__init__()
            self.checked_yes = checked_yes
            self.width = 300
            self.height = 18

        def draw(self):
            c = self.canv
            box_size = 10
            x_start = 120
            c.setFont("Times-Roman", 11)
            c.drawString(x_start, 3, "Yes")
            bx = x_start + 28
            by = 2
            c.setStrokeColor(black)
            c.setFillColor(white)
            c.rect(bx, by, box_size, box_size, fill=1, stroke=1)
            if self.checked_yes:
                c.setFillColor(black)
                c.setFont("Times-Roman", 11)
                c.drawCentredString(bx + box_size / 2, by + 1.5, "x")
            c.setFillColor(black)
            c.setFont("Times-Roman", 11)
            c.drawString(bx + box_size + 14, 3, "No")
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
    return [Paragraph(BLANK_LINE, Q_INDENT) for _ in range(count)]


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

    # Page header
    elements.append(Paragraph(f'DOB: {dob}', Q_FORM))
    elements.append(Paragraph(
        f'Decedent\'s Name: <font face="Courier"><u>&nbsp;{decedent}&nbsp;</u></font>',
        Q_FORM,
    ))
    elements.append(_spacer(10))
    elements.append(_qf("Affidavit of Heirship Questionnaire", Q_TITLE_FORM))
    elements.append(_spacer(4))

    # Q1: Family member (Affiant)
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

    # Q2: Witnesses
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

    # Q3: Relationships
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

    # Q4: Date of death
    death_date = data.get("death_date", "")
    elements.append(Paragraph(
        f'4)&nbsp;&nbsp;&nbsp;Date the Decedent died.'
        f'<font face="Courier"><u>&nbsp;{death_date}&nbsp;</u></font>',
        Q_FORM,
    ))
    elements.append(_spacer(10))

    # Q5: Where died
    elements.append(_qf("5)&nbsp;&nbsp;&nbsp;Where the Decedent died (city and county)."))
    elements.append(_two_fields(
        "City", data.get("death_city", ""),
        "County", data.get("death_county", ""),
    ))
    elements.append(_spacer(10))

    # Q6: Residential address
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

    # Q7: Marital history
    elements.append(_qf("7)&nbsp;&nbsp;&nbsp;Decedent's marital history:"))
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
    for _ in range(2):
        elements.append(Paragraph(f"Date Divorced: {BLANK_SHORT}", Q_INDENT))
    elements.append(_spacer(6))

    elements.append(_qf("C.&nbsp;&nbsp;Dates of any remarriages."))
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

    # Q8, Q9, Q10 — canvas-drawn lined forms
    class LinedFormSection(Flowable):
        def __init__(self, col_headers, col_widths, filled_rows, blank_count,
                     row_height=18):
            super().__init__()
            self.col_headers = col_headers
            self.col_widths = col_widths
            self.filled_rows = filled_rows
            self.blank_count = blank_count
            self.row_height = row_height
            self.width = sum(col_widths)
            self.height = row_height * (1 + len(filled_rows) + blank_count) + 4

        def draw(self):
            c = self.canv
            rh = self.row_height
            y = self.height - rh
            x = 0
            c.setFont("Times-Roman", 10)
            for i, hdr in enumerate(self.col_headers):
                c.drawString(x, y + 4, hdr)
                x += self.col_widths[i]
            y -= rh
            for row_vals in self.filled_rows:
                x = 0
                for i, val in enumerate(row_vals):
                    w = self.col_widths[i]
                    c.setLineWidth(0.5)
                    c.line(x, y, x + w - 8, y)
                    if val:
                        c.setFont("Courier", 10)
                        c.drawString(x + 2, y + 3, val)
                    x += w
                y -= rh
            for _ in range(self.blank_count):
                x = 0
                c.setLineWidth(0.5)
                for i in range(len(self.col_widths)):
                    w = self.col_widths[i]
                    c.line(x, y, x + w - 8, y)
                    x += w
                y -= rh

    # Q8
    elements.append(_qf(
        "8)&nbsp;&nbsp;&nbsp;Decedent's history of children. Names and "
        "birthdates of children and their relationship to decedent."
    ))
    elements.append(_spacer(4))
    children = data.get("children", [])
    child_filled = [
        [c.get("name", ""), c.get("dob", ""), c.get("other_parent", "")]
        for c in children
    ]
    elements.append(LinedFormSection(
        col_headers=["Full name", "Date of Birth",
                     "Biological/Step/Adopted  Name of other Parent"],
        col_widths=[190, 100, 170],
        filled_rows=child_filled,
        blank_count=max(0, 8 - len(children)),
    ))
    elements.append(_spacer(10))

    # Q9
    elements.append(_qf(
        "9)&nbsp;&nbsp;&nbsp;Names of any deceased children of the decedent, "
        "and dates of their death."
    ))
    elements.append(_spacer(4))
    deceased = data.get("deceased_children", [])
    dc_filled = [
        [d.get("name", ""), d.get("death_date", "")] for d in deceased
    ]
    elements.append(LinedFormSection(
        col_headers=["Full name", "Date of death"],
        col_widths=[280, 170],
        filled_rows=dc_filled,
        blank_count=max(0, 3 - len(deceased)),
    ))
    elements.append(_spacer(10))

    # Q10
    elements.append(_qf(
        "10)&nbsp;&nbsp;If any deceased child of the decedent had children, "
        "please provide the full names, and birthdates of said children, "
        "and names of their parents."
    ))
    elements.append(_spacer(4))
    grandchildren = data.get("grandchildren", [])
    if grandchildren:
        gc_filled = [
            [g.get("name", ""), g.get("dob", ""), g.get("parents", "")]
            for g in grandchildren
        ]
    else:
        gc_filled = [["N/A", "", ""]]
    elements.append(LinedFormSection(
        col_headers=["Full name", "Date of birth", "Names of parents"],
        col_widths=[180, 120, 160],
        filled_rows=gc_filled,
        blank_count=7,
    ))
    elements.append(_spacer(10))

    # Q11: Will
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

    # Q12: Debts
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

    # Q13: Taxes
    taxes = data.get("unpaid_taxes", False)
    elements.append(_qf("13)&nbsp;&nbsp;Any unpaid estate or inheritance taxes?"))
    elements.append(_spacer(4))
    elements.append(_draw_checkboxes(taxes))
    elements.append(_spacer(14))

    # Footer
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
