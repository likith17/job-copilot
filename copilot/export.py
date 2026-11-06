"""Excel view of the database.

The workbook is regenerated on every run, but the Status and Notes columns are
yours: they are read back into the DB before the file is rewritten, so edits
made in Excel survive.
"""
import os
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import ColorScaleRule, FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from copilot import db

H1B_LABEL = {
    "posting says sponsors": "Yes (posting)",
    "employer has sponsored before": "Likely (history)",
    "posting rules it out": "No (posting)",
    "unknown": "Unknown",
}

# (header, width, value getter)
COLUMNS = [
    ("Status", 13, lambda j, _: j["status"]),
    ("Fit", 6, lambda j, _: j["fit_score"]),
    ("Tailored fit", 8, lambda j, _: j["tailored_fit"]),
    ("Final", 7, lambda j, _: _r(j["tailored_final"] or j["final_score"])),
    ("Match", 7, lambda j, _: _r(j["match_score"])),
    ("Tailored", 8, lambda j, _: _r(j["tailored_match"])),
    ("Company", 16, lambda j, _: j["company"]),
    ("Title", 38, lambda j, _: j["title"]),
    ("Location", 22, lambda j, _: j["location"]),
    ("H1B", 15, lambda j, _: H1B_LABEL.get(j["sponsorship_basis"], "Not checked")),
    ("Apply", 9, None),
    ("Resume", 9, None),
    ("Cover letter", 11, None),
    ("Posting", 9, None),
    ("Why", 45, lambda j, _: j["fit_reason"] or ""),
    ("Dealbreakers", 25, lambda j, _: "; ".join(j["dealbreakers"])),
    ("Have", 30, lambda j, _: ", ".join(j["matched_skills"])),
    ("Missing", 30, lambda j, _: ", ".join(j["missing_skills"])),
    ("Review flags", 30, lambda j, _: "; ".join(j["tailor_flags"])),
    ("Sponsorship evidence", 40, lambda j, _: j["sponsorship_evidence"] or ""),
    ("Posted", 11, lambda j, _: (j["posted_at"] or "")[:10]),
    ("Found", 11, lambda j, _: (j["first_seen"] or "")[:10]),
    ("Open", 6, lambda j, _: "yes" if j["is_active"] else "closed"),
    ("Notes", 30, lambda j, _: j["notes"] or ""),
    ("Job ID", 18, lambda j, _: j["id"]),
    ("Description", 60, lambda j, _: (j["description"] or "")[:32000]),
]
HEADERS = [c[0] for c in COLUMNS]
LINK_FONT = Font(color="0563C1", underline="single")


def _r(x):
    return round(x, 3) if x is not None else None


def _rel(target, base_dir):
    """Workbook-relative link, so the data folder can be moved or synced."""
    try:
        return os.path.relpath(target, base_dir).replace("\\", "/")
    except ValueError:
        return str(target)


def sync_status_from_excel(conn, path):
    """Pull Status/Notes edits from an existing workbook into the DB."""
    path = Path(path)
    if not path.exists():
        return 0
    try:
        ws = load_workbook(path, read_only=True)["Jobs"]
    except Exception:
        return 0
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if not header or "Job ID" not in header:
        return 0
    i_id, i_status, i_notes = header.index("Job ID"), header.index("Status"), header.index("Notes")

    changed = 0
    for row in rows:
        job_id = row[i_id]
        if not job_id:
            continue
        cur = conn.execute("SELECT status, notes FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not cur:
            continue
        status = (row[i_status] or "").strip() or cur["status"]
        notes = row[i_notes] or ""
        if status not in db.STATUSES:
            continue
        if status != cur["status"] or notes != (cur["notes"] or ""):
            db.set_status(conn, job_id, status, notes)
            changed += 1
    conn.commit()
    return changed


def write_workbook(conn, path):
    path = Path(path)
    base = path.parent
    jobs = db.jobs_for_export(conn)
    resume_sha = db.get_meta(conn, "resume_sha")

    wb = Workbook()
    ws = wb.active
    ws.title = "Jobs"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
        cell.alignment = Alignment(vertical="center")

    col = {h: i + 1 for i, h in enumerate(HEADERS)}
    for r, job in enumerate(jobs, start=2):
        for c, (header, _, get) in enumerate(COLUMNS, start=1):
            if get:
                ws.cell(row=r, column=c, value=get(job, base))
        links = {
            "Apply": job["apply_url"],
            "Posting": job["url"],
            "Resume": job["resume_path"] and _rel(job["resume_path"], base),
            "Cover letter": job["cover_letter_path"] and _rel(job["cover_letter_path"], base),
        }
        stale = job.get("docs_resume_sha") and resume_sha and job["docs_resume_sha"] != resume_sha
        for header, target in links.items():
            if target:
                label = "old resume" if stale and header in ("Resume", "Cover letter") else "open"
                cell = ws.cell(row=r, column=col[header], value=label)
                cell.hyperlink = target
                cell.font = LINK_FONT

    last = max(ws.max_row, 2)
    for i, (_, width, _) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "H2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{last}"

    status_dv = DataValidation(type="list", formula1='"' + ",".join(db.STATUSES) + '"', allow_blank=True)
    ws.add_data_validation(status_dv)
    status_dv.add(f"A2:A{last}")

    for header in ("Final", "Match", "Tailored"):
        letter = get_column_letter(col[header])
        ws.conditional_formatting.add(
            f"{letter}2:{letter}{last}",
            ColorScaleRule(start_type="num", start_value=0.3, start_color="F8696B",
                           mid_type="num", mid_value=0.45, mid_color="FFEB84",
                           end_type="num", end_value=0.6, end_color="63BE7B"),
        )
    for header in ("Fit", "Tailored fit"):
        letter = get_column_letter(col[header])
        ws.conditional_formatting.add(
            f"{letter}2:{letter}{last}",
            ColorScaleRule(start_type="num", start_value=40, start_color="F8696B",
                           mid_type="num", mid_value=70, mid_color="FFEB84",
                           end_type="num", end_value=90, end_color="63BE7B"),
        )
    h1b = get_column_letter(col["H1B"])
    ws.conditional_formatting.add(f"{h1b}2:{h1b}{last}", FormulaRule(
        formula=[f'LEFT({h1b}2,2)="No"'], fill=PatternFill("solid", fgColor="F4CCCC")))
    ws.conditional_formatting.add(f"{h1b}2:{h1b}{last}", FormulaRule(
        formula=[f'OR(LEFT({h1b}2,3)="Yes",LEFT({h1b}2,6)="Likely")'],
        fill=PatternFill("solid", fgColor="D9EAD3")))
    for row in ws.iter_rows(min_row=2, max_row=last):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=False)

    _runs_sheet(wb, conn)

    try:
        wb.save(path)
        return path
    except PermissionError:
        # Usually the workbook is open in Excel. Never lose a run's output over it.
        alt = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
        wb.save(alt)
        return alt


def _runs_sheet(wb, conn):
    import json
    ws = wb.create_sheet("Runs")
    rows = conn.execute("SELECT started_at, finished_at, stats FROM runs ORDER BY id DESC LIMIT 200").fetchall()
    keys = ["fetched", "new", "filtered_out", "scored", "graded", "shortlisted", "tailored", "errors", "closed",
            "boards_failed"]
    ws.append(["Started", "Finished", *keys])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        stats = json.loads(r["stats"] or "{}")
        ws.append([r["started_at"], r["finished_at"], *[_cell(stats.get(k)) for k in keys]])
    for i in range(1, len(keys) + 3):
        ws.column_dimensions[get_column_letter(i)].width = 22 if i <= 2 else 12


def _cell(v):
    if isinstance(v, (list, dict)):
        return ", ".join(v) if isinstance(v, list) else ", ".join(v.keys())
    return v
