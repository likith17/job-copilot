"""What the autofill extension and the auto-apply runner need from the copilot:
your profile, the job behind an application URL, its tailored documents, and
answers to the form's questions.

Every answer says where it came from, because that decides what can be
submitted without you:
    profile   your profile.yaml (you wrote it)
    bank      an answer you approved before, reused for the same question
    draft     written now by the AI from your resume; you review it first
"""
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

from copilot import db, inputs, latex, llm, tailor
from copilot.config import ROOT

ATS_ID = [
    ("greenhouse", re.compile(r"(?:[?&](?:gh_jid|token)=|/jobs/)(\d{5,})")),
    ("lever", re.compile(r"jobs\.lever\.co/[^/]+/([0-9a-f-]{36})")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{36})")),
]


def load_profile(cfg):
    """profile.yaml, with blanks filled from your resume's contact line."""
    path = ROOT / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    profile = profile or {}
    contact = _resume_contact(cfg)
    if contact:
        first, _, last = contact.get("name", "").title().partition(" ")
        links = {l["label"].lower(): l["url"] for l in contact.get("links", [])}
        defaults = {"first_name": first, "last_name": last, "email": contact.get("email", ""),
                    "phone": contact.get("phone", ""), "location": contact.get("location", ""),
                    "linkedin": links.get("linkedin", ""), "github": links.get("github", ""),
                    "portfolio": links.get("portfolio") or links.get("website", "")}
        for k, v in defaults.items():
            if not profile.get(k):
                profile[k] = v
    profile.setdefault("name", f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip())
    profile["custom_answers"] = profile.get("custom_answers") or {}
    return profile


def _resume_contact(cfg):
    for upload in [cfg.resume] + [p for p in sorted((cfg.data_dir / "inputs" / "resume").glob("*.tex"))[::-1]]:
        p = Path(upload)
        if p.suffix == ".tex" and p.exists():
            return latex.parse(p.read_text(encoding="utf-8"))[0]["contact"]
    master = cfg.data_dir / "master_resume.json"
    if master.exists():
        import json
        return json.loads(master.read_text(encoding="utf-8")).get("contact")
    return None


def job_for_url(conn, url):
    """The job in the DB this application page belongs to, or None."""
    for source, pattern in ATS_ID:
        m = pattern.search(url or "")
        if m:
            row = conn.execute("SELECT * FROM jobs WHERE id LIKE ?", (f"{source}:%:{m.group(1)}",)).fetchone()
            if row:
                return db.decode(row)
    base = (url or "").split("?")[0].rstrip("/")
    for suffix in ("/apply", "/application"):
        base = base.removesuffix(suffix)
    row = conn.execute("SELECT * FROM jobs WHERE url LIKE ? OR apply_url LIKE ?", (base + "%", base + "%")).fetchone()
    return db.decode(row) if row else None


def letter_flags(job):
    """The review flags that belong to the cover letter and are still open."""
    from_letter = (job.get("tailor_report") or {}).get("letter_flags") or []
    return [f for f in job.get("tailor_flags") or [] if f in from_letter]


def documents(job):
    """{kind: Path} of the tailored files to upload; PDF preferred over DOCX.
    A cover letter with open review flags stays home: unverified claims are never sent."""
    out = {}
    for kind, key in (("resume", "resume_path"), ("cover_letter", "cover_letter_path")):
        p = Path(job.get(key) or "")
        if not job.get(key) or not p.exists() or (kind == "cover_letter" and letter_flags(job)):
            continue
        pdf = p.with_suffix(".pdf")
        out[kind] = pdf if pdf.exists() else p
    return out


def job_summary(job):
    docs = documents(job)
    return {"id": job["id"], "company": job["company"], "title": job["title"], "status": job.get("status"),
            "fit": job.get("tailored_fit") or job.get("fit_score"),
            "flags": [f for f in job.get("tailor_flags") or [] if f not in letter_flags(job)],
            "letter_withheld": bool(letter_flags(job)), "sponsorship": job.get("sponsorship_status"),
            "documents": {k: p.name for k, p in docs.items()}}


# ---------------- answering questions ----------------

class Answer(BaseModel):
    answer: str
    needs_user: bool     # true when the question asks for a fact the resume and profile don't give
    reason: str


ANSWER_SYSTEM = """You answer one question on a job application form, as the candidate.

Use ONLY facts from the candidate's resume, profile, and the job posting below.
- Never invent experience, numbers, tools, dates, people, or opinions about the
  company that the posting doesn't support.
- If the question needs a fact you don't have (a salary number, a date, a legal
  or personal fact, a yes/no you can't verify, a referral name), set
  needs_user=true and answer "".
- Short-answer questions: 2-5 sentences, under 120 words unless the question
  asks for more. One-line fields: just the value.
- Plain, direct English in the first person. No cliches, no "I am thrilled",
  "passionate", "leverage", "utilize", "cutting-edge". ASCII punctuation only.
"""


def answer_question(cfg, conn, question, job=None, kind="text", options=None, settings=None, draft=True,
                    required=None):
    """{answer, source, note} for one form question. Order: your profile's custom
    answers, then your answer bank, then an AI draft. source="" means: leave it
    for you, and the question goes to your to-answer inbox in the app."""
    required = draft if required is None else required

    def unanswered(note):
        db.note_question(conn, question, kind, options, required, job["company"] if job else "")
        return {"answer": "", "source": "", "note": note}

    profile = load_profile(cfg)
    q = question.lower()
    for key, value in profile["custom_answers"].items():
        if key.lower() in q and value not in (None, ""):
            return {"answer": str(value), "source": "profile", "note": f"custom_answers: {key}"}
    saved = db.find_answer(conn, question)
    if saved:
        if not saved["answer"].strip():
            return {"answer": "", "source": "", "note": "you chose to skip this question"}
        return {"answer": saved["answer"], "source": "bank", "note": "your saved answer"}
    if options is not None:     # fixed-choice questions are never drafted: answered from the profile or by you
        return unanswered("choice question: answer it once in the app (Autofill info)")
    if not draft:   # optional questions are only answered in your own words
        return unanswered("not in your answers yet: add it in the app (Autofill info)")
    if settings is None:
        settings = llm.preflight(llm.task_settings(cfg, "answer" if "answer" in (cfg.llm.get("tasks") or {})
                                                   else "tailor"), say=lambda *_: None)
    resume = tailor.resume_to_text(tailor.load_or_build_master(cfg, inputs.read_text(cfg.resume), log=lambda *_: None))
    facts = {k: profile.get(k) for k in ("location", "current_company", "current_title", "years_experience",
                                          "work_authorization", "start_date", "willing_to_relocate",
                                          "open_to_remote", "open_to_hybrid", "open_to_onsite", "education",
                                          "certifications", "languages")}
    user = (f"QUESTION ({kind} field): {question}\n\nCANDIDATE PROFILE:\n{yaml.safe_dump(facts, sort_keys=False)}"
            f"\nRESUME:\n{resume}")
    if job:
        user += f"\n\nJOB: {job['company']} | {job['title']}\nPOSTING:\n{(job.get('description') or '')[:6000]}"
    out, _ = llm.parse(settings, [{"type": "text", "text": ANSWER_SYSTEM}], user, Answer, max_tokens=4000)
    if out.needs_user or not out.answer.strip():
        return unanswered(out.reason)
    text = tailor.plain(out.answer.strip())
    banned = tailor.banned_phrases(text)
    if banned:
        return {"answer": text, "source": "draft", "note": f"review: uses {', '.join(banned)}"}
    return {"answer": text, "source": "draft", "note": out.reason}
