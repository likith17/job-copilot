"""What keeps unattended runs safe: retries, caches, backups, the upload guard, the job's header."""
import copy
import gzip
import json
import sqlite3

import pytest

from copilot import db, inputs, latex, llm, tailor
from copilot.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(resume=tmp_path / "r.tex", companies=tmp_path / "c.yaml", data_dir=tmp_path)


LAYOUT = r"""\documentclass{article}
\newenvironment{rsection}[1]{}{}
\newcommand{\role}[2]{#1 #2}
\newcommand{\gap}{}
\begin{document}
% ---------- header ----------
\begin{center}
{\large\textbf{Pat Example, Machine Learning Engineer}}\\
{\small Albany, NY \;$|$\; +1 (555) 010-0000 \;$|$\;
\href{mailto:pat@example.com}{pat@example.com} \;$|$\; \href{https://github.com/pat}{GitHub}}
\end{center}
\begin{rsection}{Professional Summary}
Machine Learning Engineer with 3 years of production experience.
\end{rsection}
\begin{rsection}{Areas of Expertise}
Python \& C++
\end{rsection}
\begin{rsection}{Technical Skills}
\textbf{Languages:} Python, C++\\[1pt]
\textbf{AI/ML:} PyTorch,
Reinforcement Learning\\[1pt]
\textbf{Databases:} MySQL \quad
\textbf{Systems:} Linux, Docker
\end{rsection}
\begin{rsection}{Employment History}
\role{AI Research Assistant, Example University}{2026 -- Present}
Built \href{https://example.com/x}{Project X}, cutting latency 20\%.
\begin{itemize}
\item Trained models with PyTorch.
\item Evaluated results.
\end{itemize}

\gap
\role{Engineer, Acme}{2021 -- 2023}
\begin{itemize}
\item Fixed production issues.
\end{itemize}
\end{rsection}
\end{document}
"""


def test_environment_sections_and_lead_paragraphs():
    master, _ = latex.parse(LAYOUT)
    assert master["contact"]["name"] == "Pat Example" and master["headline"] == "Machine Learning Engineer"
    assert master["contact"]["location"] == "Albany, NY" and master["contact"]["email"] == "pat@example.com"
    assert master["summary"].startswith("Machine Learning Engineer with 3 years")
    assert {g["category"]: g["items"] for g in master["skills"]} == {
        "Languages": ["Python", "C++"], "AI/ML": ["PyTorch", "Reinforcement Learning"],
        "Databases": ["MySQL"], "Systems": ["Linux", "Docker"]}
    first, second = master["experience"]
    assert first["bullets"][0].startswith("Built [[link:Project X]]") and len(first["bullets"]) == 3
    assert len(second["bullets"]) == 1
    assert latex.render(LAYOUT, master, copy.deepcopy(master)) == LAYOUT     # nothing changed: byte-identical


def test_render_writes_headline_paragraph_and_bullets_in_place():
    master, _ = latex.parse(LAYOUT)
    r = copy.deepcopy(master)
    r["headline"] = "AI Engineer"
    r["experience"][0]["bullets"][0] = r["experience"][0]["bullets"][0].replace("cutting", "reducing")
    r["experience"][0]["bullets"][2] = "Evaluated results rigorously."
    out = latex.render(LAYOUT, master, r)
    assert r"\textbf{Pat Example, AI Engineer}" in out
    assert r"\href{https://example.com/x}{Project X}, reducing latency 20\%." in out      # link kept, paragraph stays one
    assert r"\item Evaluated results rigorously." in out and r"\item Trained models with PyTorch." in out
    assert out.count(r"\item") == LAYOUT.count(r"\item") and r"\gap" in out
    reordered = copy.deepcopy(master)
    reordered["experience"].reverse()
    assert latex.parse(latex.render(LAYOUT, master, reordered))[0]["experience"][0]["title"].startswith("Engineer")


@pytest.mark.parametrize("title,role", [
    ("Software Engineer II, AI Agents (NYC)", "Software Engineer"),
    ("Senior Machine Learning Engineer", "Machine Learning Engineer"),
    ("AI/ML Engineer - Platform", "AI/ML Engineer"),
    ("Full-Stack Engineer, Growth", "Full-Stack Engineer"),
    ("Research Scientist", "Research Scientist"),
    ("Head of AI", "ML Engineer"),                  # not a plain technical role: keep the resume's own
    ("Staff Engineer", "ML Engineer"),              # nothing left but a level
    ("Forward Deployed Engineer", "ML Engineer"),
    ("University Grad Software Engineer 2027", "Software Engineer"),
    ("Demo Experience Engineer", "ML Engineer"),
    ("Machine Learning Engineer, Infrastructure", "Machine Learning Engineer"),
])
def test_role_headline_never_claims_a_level(title, role):
    assert tailor.role_headline(title, "ML Engineer") == role


def test_summary_must_state_the_role_and_use_posting_terms():
    terms = ["PyTorch", "RAG", "Kubernetes"]
    assert tailor.summary_problem("Machine Learning Engineer using PyTorch and RAG.", "Machine Learning Engineer", terms) is None
    assert "role" in tailor.summary_problem("Engineer using PyTorch and RAG.", "Data Scientist", terms)
    assert "terms" in tailor.summary_problem("Data Scientist with experience.", "Data Scientist", terms)


def test_header_falls_back_to_the_candidates_own_words(monkeypatch):
    master, _ = latex.parse(LAYOUT)
    resume = copy.deepcopy(master)
    report = {"requirements": [{"need": "x", "keywords": ["PyTorch"], "evidence": []}]}
    monkeypatch.setattr(llm, "parse", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("bad output")))
    tailor.tailor_header(master, resume, report, {"title": "Senior Applied Scientist, Ranking"}, {})
    assert resume["headline"] == "Applied Scientist"
    assert resume["summary"].startswith("Applied Scientist with 3 years")
    assert resume["summary"].endswith("Hands-on experience with PyTorch.")    # on the resume, so safe to name


def test_fallback_names_only_what_the_resume_supports():
    master, _ = latex.parse(LAYOUT)
    reqs = [{"need": "x", "keywords": ["PyTorch", "Reinforcement Learning", "Kubernetes"], "evidence": []}]
    terms = tailor.summary_terms(reqs, master)
    assert "Kubernetes" not in terms
    resume = copy.deepcopy(master)
    tailor.role_first(master, resume, "AI Engineer", terms)
    assert resume["summary"].startswith("AI Engineer with 3 years")
    assert "PyTorch" in resume["summary"] and "Reinforcement Learning" in resume["summary"]
    assert "Kubernetes" not in resume["summary"]


LETTER = ("Dear team,\n\nI built a routing system with YOLOv12. I also wrapped the inference service in a FastAPI "
          "endpoint for low-latency access. I leveraged Python daily.\n\nI would welcome a conversation.")


def test_flagged_sentences_are_cut_and_banned_words_swapped():
    flag = 'unsupported claim: "wrapped the inference service in a FastAPI endpoint": not in the source'
    out, dropped = tailor.drop_flagged(LETTER, [flag, "cover letter uses banned phrase: leveraged"])
    assert dropped == 1 and "FastAPI" not in out and "I used Python daily." in out
    assert out.startswith("Dear team,\n\nI built") and out.endswith("I would welcome a conversation.")


def test_a_flag_that_matches_no_sentence_cuts_nothing():
    out, dropped = tailor.drop_flagged(LETTER, ["unsupported claim: managed a team of forty engineers across Europe"])
    assert dropped == 0 and "FastAPI" in out


def test_too_many_flags_leave_the_letter_for_a_person():
    flags = [f'unsupported claim: "routing system with YOLOv12 number {i}"' for i in range(6)]
    assert tailor.drop_flagged(LETTER, flags)[1] == 0


def test_scrub_keeps_the_letter_flagged_if_the_audit_still_objects(monkeypatch):
    flag = 'unsupported claim: "wrapped the inference service in a FastAPI endpoint"'
    monkeypatch.setattr(tailor, "letter_problems", lambda *a, **k: [])
    monkeypatch.setattr(tailor, "audit_claims", lambda *a, **k: [])
    clean, left = tailor.scrub_letter({}, LETTER, [flag], "", {})
    assert "FastAPI" not in clean and left == []
    monkeypatch.setattr(tailor, "audit_claims", lambda *a, **k: ["still wrong", "and this"])
    same, left = tailor.scrub_letter({}, LETTER, [flag], "", {})
    assert same == LETTER and left == [flag]


def test_audit_flags_revert_edits_even_when_paraphrased():
    master, _ = latex.parse(LAYOUT)
    resume = copy.deepcopy(master)
    resume["summary"] = "Software Engineer who built observability dashboards for latency."
    edited = "Trained models with PyTorch on a large GPU cluster."
    resume["experience"][0]["bullets"][1] = edited
    report = {"accepted": [{"id": "E1.2", "before": "Trained models with PyTorch.", "after": edited}], "rejected": []}
    left = tailor.revert_flagged(master, resume, report, [
        "unsupported claim: built observability dashboards",         # the summary
        'unsupported claim: "observability dashboards for latency"',  # the same summary, counted once
        "unsupported claim: PyTorch training on large GPU cluster",   # a paraphrase of the bullet
        "unsupported claim: managed a team of forty engineers",       # points at nothing: for a person
    ])
    assert resume["summary"] == master["summary"] and resume["experience"][0]["bullets"][1] == "Trained models with PyTorch."
    assert report["accepted"] == [] and left == ["unsupported claim: managed a team of forty engineers"]


def test_rate_limits_are_waited_out(monkeypatch):
    calls, waits = [], []
    def flaky(*a):
        calls.append(1)
        if len(calls) < 3:
            raise llm.RateLimited("slow down")
        return "ok", None
    monkeypatch.setattr(llm, "_parse_openai", flaky)
    monkeypatch.setattr("time.sleep", waits.append)
    assert llm.parse({"type": "openai"}, [], "", None) == ("ok", None)
    assert waits == list(llm.RATE_LIMIT_WAITS[:2])


def test_a_limit_that_never_clears_stops_cleanly(monkeypatch):
    def always(*a):
        raise llm.RateLimited("quota")
    monkeypatch.setattr(llm, "_parse_openai", always)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(llm.LLMUnavailable):
        llm.parse({"type": "openai"}, [], "", None)


@pytest.mark.parametrize("name,flag", [
    ("PAT_EXAMPLE_Senior_Software_Engineer_20261019.pdf", True),
    ("PAT_EXAMPLE_Resume_Acme.pdf", True),
    ("REVIEW_changes_in_bold_Asana.pdf", True),
    ("resume.tex", False),
    ("my_CV_23_.pdf", False),
])
def test_upload_guard_names(cfg, name, flag):
    assert inputs.looks_generated(cfg, name, b"data") is flag


def test_upload_guard_blocks_unless_forced_and_spots_our_files(cfg):
    folder = cfg.data_dir / "applications" / "job"
    folder.mkdir(parents=True)
    (folder / "any.pdf").write_bytes(b"same bytes")
    with pytest.raises(inputs.LooksGenerated):
        inputs.save_upload(cfg, "resume", "renamed.pdf", b"same bytes")
    assert inputs.save_upload(cfg, "resume", "renamed.pdf", b"same bytes", force=True).exists()
    assert inputs.save_upload(cfg, "cover_letter", "Letter_Resume_x.pdf", b"x").exists()   # letters aren't guarded


def test_backup_is_daily_compressed_and_pruned(cfg):
    conn = db.connect(cfg.db_path)
    db.set_meta(conn, "x", "1")
    conn.close()
    folder = cfg.data_dir / "backups"
    folder.mkdir()
    for d in range(1, 9):
        (folder / f"copilot-2020-01-0{d}.db.gz").write_bytes(b"old")
    out = db.daily_backup(cfg.db_path)
    assert db.daily_backup(cfg.db_path) is None                     # once a day
    assert len(list(folder.glob("*.gz"))) == 7 and out.exists()
    raw = cfg.data_dir / "restored.db"
    raw.write_bytes(gzip.decompress(out.read_bytes()))
    assert sqlite3.connect(raw).execute("SELECT value FROM meta WHERE key = 'x'").fetchone()[0] == "1"


def _job(job_id, **fields):
    return {"id": job_id, "source": "lever", "company": "Acme", "title": "Engineer", "location": "", "country": "",
            "department": "", "url": "u", "apply_url": "u", "description": "text", "posted_at": None, **fields}


def test_new_resume_leaves_filtered_jobs_and_prune_keeps_the_row(cfg):
    conn = db.connect(cfg.db_path)
    for j in ("a", "b", "c"):
        db.upsert_seen(conn, _job(j))
    db.update(conn, "a", stage="filtered_out")
    db.update(conn, "b", stage="filtered_out", is_active=0)
    db.update(conn, "c", stage="graded")
    conn.commit()
    assert db.reset_for_new_resume(conn) == 1
    assert db.prune_closed(conn) == 1
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["a"]["description"] == "text" and rows["b"]["description"] == "" and rows["c"]["stage"] == "new"
    db.reset_for_rescore(conn)                      # a job with no text can't be re-filtered
    assert conn.execute("SELECT stage FROM jobs WHERE id = 'b'").fetchone()[0] == "filtered_out"
    assert conn.execute("SELECT stage FROM jobs WHERE id = 'a'").fetchone()[0] == "new"


def test_closed_postings_are_not_graded(cfg, monkeypatch):
    from copilot import grade, pipeline
    conn = db.connect(cfg.db_path)
    db.upsert_seen(conn, _job("closed"))
    db.update(conn, "closed", stage="candidate", match_score=0.9, is_active=0)
    conn.commit()
    cfg.llm = {"tasks": {"grade": {"provider": "local", "model": "m"}}}
    monkeypatch.setattr(grade, "grade", lambda *a: pytest.fail("graded a closed posting"))
    pipeline._grade(cfg, conn, "resume", {"graded": 0, "errors": 0}, lambda *_: None)


def test_grades_are_reused_for_the_same_resume_and_posting(cfg, monkeypatch):
    from copilot import grade, pipeline
    conn = db.connect(cfg.db_path)
    db.upsert_seen(conn, _job("a"))
    db.update(conn, "a", stage="candidate", match_score=0.6)
    conn.commit()
    cfg.llm = {"tasks": {"grade": {"provider": "local", "model": "m", "min_fit": 70}}}
    calls = []
    def fake(resume_text, job, settings):
        calls.append(job["id"])
        return grade.FitGrade(fit=90, reason="good", dealbreakers=[]), None
    monkeypatch.setattr(grade, "grade", fake)
    stats = {"graded": 0, "errors": 0}
    pipeline._grade(cfg, conn, "resume one", stats, lambda *_: None)
    db.update(conn, "a", stage="candidate", fit_score=None)
    conn.commit()
    pipeline._grade(cfg, conn, "resume one", stats, lambda *_: None)
    assert calls == ["a"] and stats["graded"] == 2
    assert conn.execute("SELECT stage, fit_score FROM jobs").fetchone()["fit_score"] == 90
    db.update(conn, "a", stage="candidate")
    conn.commit()
    pipeline._grade(cfg, conn, "another resume", stats, lambda *_: None)
    assert calls == ["a", "a"]                      # a different resume is a different grade


def test_sponsorship_is_asked_once_per_posting(cfg, monkeypatch):
    from copilot import scoring
    calls = []
    def fake(text, settings):
        calls.append(text)
        return {"status": "sponsors", "evidence": "", "reasoning": "r", "verified": False}
    monkeypatch.setattr(scoring, "sponsorship", fake)
    a = scoring.sponsorship_cached(cfg, "posting", {"model": "m"})
    assert scoring.sponsorship_cached(cfg, "posting", {"model": "m"}) == a and calls == ["posting"]
