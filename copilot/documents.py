"""Render tailored resumes and cover letters as ATS-friendly DOCX.

Single column, real text (no tables or text boxes), standard section names:
the layout applicant-tracking parsers read most reliably.
"""
import re
from datetime import date

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

FONT = "Calibri"
ACCENT = RGBColor(0x1F, 0x3A, 0x5F)


def slug(text, limit=40):
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return s[:limit].rstrip("_") or "job"


def _base_document():
    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = sec.bottom_margin = Inches(0.5)
    sec.left_margin = sec.right_margin = Inches(0.6)
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    style.font.size = Pt(10)
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.space_before = Pt(0)
    return doc


def _para(doc, text="", size=None, bold=False, italic=False, align=None, after=0, color=None):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(after)
    if align:
        p.alignment = align
    if text:
        run = p.add_run(text)
        run.bold, run.italic = bold, italic
        if size:
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = color
    return p


def _hyperlink(paragraph, text, url):
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                          is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rpr.append(u)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _heading(doc, text):
    p = _para(doc, text.upper(), size=10.5, bold=True, color=ACCENT)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(2)
    # bottom border as a section rule
    ppr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in {"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": "1F3A5F"}.items():
        bottom.set(qn(k), v)
    bdr.append(bottom)
    ppr.append(bdr)


def _left_right(doc, left, right, bold_left=True, italic=False):
    """Title on the left, dates flush right via a right tab stop."""
    p = doc.add_paragraph()
    sec = doc.sections[0]
    width = sec.page_width - sec.left_margin - sec.right_margin
    p.paragraph_format.tab_stops.add_tab_stop(width, alignment=2)  # 2 = right
    r = p.add_run(left)
    r.bold, r.italic = bold_left, italic
    if right:
        p.add_run("\t" + right)
    return p


def _mark(run):
    """Review-copy styling for changed text: bold on a yellow highlight."""
    run.bold = True
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _bullet(doc, text, changed=False):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.left_indent = Inches(0.2)
    p.paragraph_format.space_after = Pt(1)
    run = p.add_run(text)
    if changed:
        _mark(run)


def render_resume(r, path, changed=frozenset()):
    """`changed` holds texts that differ from the master resume. Pass it only for
    the review copy: changes come out bold and highlighted so they're easy to check.
    The copy you send is rendered without it."""
    doc = _base_document()
    c = r["contact"]
    _para(doc, c["name"], size=16, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, color=ACCENT)
    line = _para(doc, " | ".join(x for x in [c["location"], c["phone"], c["email"]] if x),
                 align=WD_ALIGN_PARAGRAPH.CENTER)
    for link in c["links"]:
        line.add_run(" | ")
        _hyperlink(line, link["label"], link["url"])

    _heading(doc, "Summary")
    summary = _para(doc, r["summary"])
    if r["summary"] in changed:
        for run in summary.runs:
            _mark(run)

    _heading(doc, "Technical Skills")
    for g in r["skills"]:
        p = doc.add_paragraph()
        p.add_run(f"{g['category']}: ").bold = True
        p.add_run(", ".join(g["items"]))

    _heading(doc, "Professional Experience")
    for role in r["experience"]:
        _left_right(doc, f"{role['title']}, {role['company']}", role["dates"])
        for b in role["bullets"]:
            _bullet(doc, b, b in changed)

    _heading(doc, "Projects")
    for proj in r["projects"]:
        _left_right(doc, proj["name"], proj["dates"])
        for b in proj["bullets"]:
            _bullet(doc, b, b in changed)

    _heading(doc, "Education")
    for e in r["education"]:
        _left_right(doc, e["school"], e["dates"])
        _para(doc, e["degree"], italic=True)
        for d in e["details"]:
            _para(doc, d)

    if r["publications"]:
        _heading(doc, "Publications")
        for pub in r["publications"]:
            _bullet(doc, pub)

    doc.save(path)


def render_cover_letter(text, contact, company, path):
    doc = _base_document()
    doc.sections[0].left_margin = doc.sections[0].right_margin = Inches(1)
    doc.sections[0].top_margin = Inches(0.8)
    doc.styles["Normal"].font.size = Pt(11)

    _para(doc, contact["name"], size=14, bold=True, color=ACCENT)
    _para(doc, " | ".join(x for x in [contact["location"], contact["phone"], contact["email"]] if x), after=14)
    _para(doc, date.today().strftime("%B %d, %Y"), after=10)
    _para(doc, company, after=12)

    for block in re.split(r"\n\s*\n", text.strip()):
        p = _para(doc, after=10)
        for i, line in enumerate(block.strip().split("\n")):
            if i:
                p.add_run().add_break()  # keeps "Sincerely,\nName" on two lines
            p.add_run(line.strip())
    doc.save(path)
