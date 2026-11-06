"""Tests for the copilot pipeline's deterministic parts (no network, no API)."""
import copy
from datetime import datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook

from copilot import db, export
from copilot.fetch import is_us, min_years_required, prefilter_reason
from copilot.tailor import apply_plan, guard, revert_flagged

RULES = {
    "title_include": ["engineer", "ml", "ai"],
    "title_exclude": ["senior", "sr.", "staff", "iii"],
    "us_only": True,
    "max_years_required": 4,
    "max_age_days": 30,
}


def job(**kw):
    base = {
        "id": "ashby:acme:1", "source": "ashby", "company": "Acme", "title": "ML Engineer",
        "location": "San Francisco, CA", "country": None, "department": "", "url": "https://x/1",
        "apply_url": "https://x/1/application", "description": "Build models with Python. " * 20,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(kw)
    return base


# ---------------- prefilter ----------------

@pytest.mark.parametrize("title,kept", [
    ("ML Engineer", True),
    ("Software Engineer II", True),       # II is deliberately allowed
    ("Senior ML Engineer", False),
    ("Sr. AI Engineer", False),
    ("Software Engineer III", False),
    ("Account Executive", False),          # no include term
    ("Email Marketing Manager", False),    # "ai" must be a whole word, not inside "email"
])
def test_title_rules(title, kept):
    assert (prefilter_reason(job(title=title), RULES) is None) == kept


@pytest.mark.parametrize("location,country,expected", [
    ("San Francisco, CA", None, True),
    ("New York, New York, USA", None, True),
    ("Bengaluru, India; Seattle, WA", None, True),   # multi-location with a US site
    ("Remote", None, True),
    ("Remote in Canada", None, False),                # bare "in" is not Indiana
    ("London, UK", None, False),
    ("Remote", "Canada", False),                      # structured country wins
    ("Somewhere", "United States", True),
])
def test_us_detection(location, country, expected):
    assert is_us({"location": location, "country": country}) == expected


def test_min_years_takes_the_smallest_requirement():
    assert min_years_required("3+ years of experience required; 5+ years preferred") == 3
    assert min_years_required("7+ years of professional software engineering experience") == 7
    assert min_years_required("Founded 10 years ago") is None


def test_experience_and_age_filters():
    assert prefilter_reason(job(description="Need 7+ years of experience. " * 20), RULES).startswith("experience")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    assert prefilter_reason(job(posted_at=old), RULES).startswith("age")


# ---------------- fabrication guard ----------------

MASTER = {
    "contact": {"name": "A B", "location": "NJ", "phone": "1", "email": "a@b.c", "links": []},
    "summary": "AI engineer.",
    "education": [{"school": "BU", "degree": "MS CS", "dates": "2024 - 2026", "details": []}],
    "skills": [{"category": "Languages", "items": ["Python", "C++"]},
               {"category": "AI/ML", "items": ["PyTorch", "RAG"]}],
    "experience": [{"title": "Engineer", "company": "TCS", "dates": "2021 - 2023",
                    "bullets": ["Fixed a defect worth $500K+ in revenue.", "Led incident calls."]}],
    "projects": [{"name": f"P{i}", "dates": "2025", "bullets": [f"Did thing {i}."]} for i in range(4)],
    "publications": [],
}


def test_guard_removes_skills_not_on_master():
    groups, flags = guard(MASTER, "resume", "", [{"category": "AI/ML", "items": ["PyTorch", "Kubernetes"]}])
    assert groups == [{"category": "AI/ML", "items": ["PyTorch"]}]
    assert any("Kubernetes" in f for f in flags)


def test_guard_flags_invented_numbers_and_skills():
    _, flags = guard(MASTER, "Cut latency 40% using Kubernetes.", "", MASTER["skills"])
    assert any("40" in f for f in flags)
    assert any("kubernetes" in f for f in flags)


def test_guard_accepts_master_numbers_and_jd_numbers_in_cover_letter():
    _, flags = guard(MASTER, "Fixed a $500K defect.", "Excited to join your 200-person team.",
                     MASTER["skills"], job_description="We are a 200-person team.")
    assert flags == []


def test_guard_rejects_jd_numbers_in_resume():
    _, flags = guard(MASTER, "Supported 200 engineers.", "", MASTER["skills"], job_description="200-person team")
    assert any("resume" in f and "200" in f for f in flags)


# ---------------- edit-based tailoring ----------------

def plan(**kw):
    base = {"requirements": [{"need": "Python", "keywords": ["python", "incident response"], "evidence": []}],
            "summary": "", "skills": MASTER["skills"], "edits": [], "orders": [],
            "cover_letter": "", "changes": []}
    base.update(kw)
    return base


def edit(bid, text):
    return {"id": bid, "text": text, "reason": "test"}


def test_edit_that_drops_a_metric_is_rejected():
    out, report = apply_plan(MASTER, plan(edits=[edit("E1.1", "Fixed a critical revenue defect quickly.")]))
    assert out["experience"][0]["bullets"][0] == "Fixed a defect worth $500K+ in revenue."
    assert "drops metric" in report["rejected"][0]["why"] and "500k+" in report["rejected"][0]["why"]


def test_edit_that_keeps_metrics_is_accepted_and_reported():
    new = "Fixed a billing-accuracy defect worth $500K+ in revenue."
    p = plan(requirements=[{"need": "x", "keywords": ["billing accuracy"], "evidence": ["E1.1"]}],
             edits=[edit("E1.1", new)])
    out, report = apply_plan(MASTER, p)
    assert out["experience"][0]["bullets"][0] == new
    assert report["accepted"][0]["before"].startswith("Fixed") and report["accepted"][0]["after"] == new


@pytest.mark.parametrize("text,why", [
    ("Fixed a billing-accuracy defect worth $500K+ in revenue for 40 clients.", "adds number"),
    ("Fixed a billing-accuracy defect worth $500K+ with Kubernetes.", "doesn't mention"),
    ("Fixed a billing-accuracy defect worth $500K+, " + "and much more " * 10, "length"),
    ("Corrected a defect worth $500K+ in revenue.", "cosmetic"),      # synonym swap only
])
def test_edit_rules_are_enforced_in_code(text, why):
    p = plan(requirements=[{"need": "x", "keywords": ["billing accuracy"], "evidence": []}],
             edits=[edit("E1.1", text)])
    _, report = apply_plan(MASTER, p)
    assert why in report["rejected"][0]["why"]


def test_posting_keyword_cannot_move_onto_a_role_that_never_mentions_it():
    for kw in ("REST APIs", "rest apis"):   # technical by case, or as a known skill in any case
        p = plan(requirements=[{"need": "APIs", "keywords": [kw], "evidence": ["E1.1"]}],
                 edits=[edit("E1.1", "Fixed a defect in REST APIs worth $500K+ in revenue.")])
        _, report = apply_plan(MASTER, p)
        assert kw in report["rejected"][0]["why"] and "doesn't support" in report["rejected"][0]["why"]


def test_descriptive_framing_and_word_forms_are_allowed():
    # "validated" matches the posting's "validate"; "billing accuracy" is framing, not a tool.
    p = plan(requirements=[{"need": "x", "keywords": ["validate", "billing accuracy"], "evidence": []}],
             edits=[edit("E1.1", "Fixed and validated a billing-accuracy defect worth $500K+.")])
    out, report = apply_plan(MASTER, p)
    assert report["accepted"] and not report["rejected"]


def test_skill_on_resume_elsewhere_still_cannot_move_onto_this_role():
    # PyTorch is in the skills list, but the TCS role never mentions it.
    _, report = apply_plan(MASTER, plan(edits=[edit("E1.1", "Fixed a PyTorch defect worth $500K+ in revenue.")]))
    assert "pytorch" in report["rejected"][0]["why"]


def test_nothing_is_dropped_or_invented_structurally():
    p = plan(orders=[{"section": "PROJECTS", "order": ["P3", "P1"]},       # partial order
                     {"section": "E1", "order": ["E1.2", "E1.1"]}],
             skills=[{"category": "AI/ML", "items": ["RAG", "Kubernetes"]}],  # reorders, invents
             edits=[edit("E9.9", "Ghost bullet.")])
    out, report = apply_plan(MASTER, p)
    assert [x["name"] for x in out["projects"]] == ["P2", "P0", "P1", "P3"]  # listed first, rest kept
    assert out["experience"][0]["bullets"][0] == "Led incident calls."
    assert out["skills"][0] == {"category": "AI/ML", "items": ["RAG", "PyTorch"]}   # no Kubernetes, no loss
    assert out["skills"][1]["category"] == "Languages"
    assert out["experience"][0]["company"] == "TCS" and out["education"] == MASTER["education"]
    assert report["rejected"][0]["why"] == "unknown bullet id"
    assert MASTER["experience"][0]["bullets"][0].startswith("Fixed")       # master untouched


def test_keyword_coverage_is_counted_by_code():
    p = plan(requirements=[{"need": "x", "keywords": ["python", "billing accuracy", "incident response"],
                            "evidence": []}],
             edits=[edit("E1.1", "Fixed a billing-accuracy defect worth $500K+ in revenue.")])
    out, report = apply_plan(MASTER, p)
    assert out["experience"][0]["bullets"][0] == "Fixed a billing-accuracy defect worth $500K+ in revenue."
    # python: already in skills. billing accuracy: surfaced. incident response: never claimed.
    assert (report["coverage"]["before"], report["coverage"]["after"], report["coverage"]["total"]) == (1, 2, 3)
    assert report["coverage"]["missing"] == ["incident response"]


def test_flagged_phrase_reverts_its_bullet():
    # "revenue" is new to this bullet but supported by the role, so code accepts the edit;
    # the audit then catches "fleet monitoring", which code can't judge.
    new = "Led revenue calls on fleet monitoring."
    p = plan(requirements=[{"need": "x", "keywords": ["revenue"], "evidence": []}], edits=[edit("E1.2", new)])
    resume, report = apply_plan(MASTER, p)
    left = revert_flagged(MASTER, resume, report, ['unsupported claim: "fleet monitoring": not in source',
                                                   'unsupported claim: "passion for robots": letter'])
    assert resume["experience"][0]["bullets"][1] == "Led incident calls."
    assert report["accepted"] == [] and "claims audit" in report["rejected"][0]["why"]
    assert left == ['unsupported claim: "passion for robots": letter']


# ---------------- database lifecycle ----------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_upsert_keeps_user_state_on_refetch(conn):
    assert db.upsert_seen(conn, job()) is True
    db.set_status(conn, "ashby:acme:1", "Applied", "sent Monday")
    assert db.upsert_seen(conn, job(title="ML Engineer (updated)")) is False
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert (row["status"], row["notes"], row["title"]) == ("Applied", "sent Monday", "ML Engineer (updated)")
    assert row["applied_at"]


def test_mark_closed_skips_boards_that_failed(conn):
    db.upsert_seen(conn, job(id="a:1", company="Acme"))
    db.upsert_seen(conn, job(id="b:1", company="Beta"))
    # Acme fetched fine without a:1; Beta's board failed, so b:1 must stay open.
    assert db.mark_closed(conn, {"Acme"}, seen_ids=set()) == 1
    active = dict(conn.execute("SELECT id, is_active FROM jobs").fetchall())
    assert active == {"a:1": 0, "b:1": 1}


def test_rescore_keeps_tailored_jobs(conn):
    db.upsert_seen(conn, job(id="a:1"))
    db.upsert_seen(conn, job(id="a:2"))
    db.update(conn, "a:1", stage="tailored")
    db.update(conn, "a:2", stage="graded", fit_score=40)
    assert db.reset_for_rescore(conn) == 1
    stages = dict(conn.execute("SELECT id, stage FROM jobs").fetchall())
    assert stages == {"a:1": "tailored", "a:2": "new"}
    assert conn.execute("SELECT fit_score FROM jobs WHERE id='a:2'").fetchone()[0] == 40


def test_set_status_rejects_unknown(conn):
    db.upsert_seen(conn, job())
    with pytest.raises(ValueError):
        db.set_status(conn, "ashby:acme:1", "Ghosted")


def test_migration_adds_missing_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT)")
    old.commit()
    old.close()
    c = db.connect(path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    assert {"fit_score", "tailored_fit", "country", "status"} <= cols


# ---------------- Excel round trip ----------------

def test_excel_status_edits_flow_back_to_db(conn, tmp_path):
    db.upsert_seen(conn, job())
    db.update(conn, "ashby:acme:1", stage="tailored", fit_score=85, match_score=0.6,
              resume_path=str(tmp_path / "applications" / "r.docx"))
    conn.commit()
    path = tmp_path / "jobs.xlsx"
    assert export.write_workbook(conn, path) == path

    wb = load_workbook(path)
    ws = wb["Jobs"]
    hdr = [c.value for c in ws[1]]
    assert ws.cell(2, hdr.index("Apply") + 1).hyperlink.target == "https://x/1/application"
    assert ws.cell(2, hdr.index("Resume") + 1).hyperlink.target == "applications/r.docx"  # relative
    ws.cell(2, hdr.index("Status") + 1, "Applied")
    ws.cell(2, hdr.index("Notes") + 1, "referral from Sam")
    wb.save(path)

    assert export.sync_status_from_excel(conn, path) == 1
    row = conn.execute("SELECT status, notes, applied_at FROM jobs").fetchone()
    assert (row["status"], row["notes"]) == ("Applied", "referral from Sam") and row["applied_at"]
    assert export.sync_status_from_excel(conn, path) == 0   # idempotent


# ---------------- provider layer ----------------

from types import SimpleNamespace

from pydantic import BaseModel

from copilot import llm


class _Inner(BaseModel):
    x: int


class _Outer(BaseModel):
    items: list[_Inner]


def test_task_settings_merge_provider_and_task():
    cfg = SimpleNamespace(llm={"providers": {"groq": {"type": "openai", "base_url": "u", "api_key_env": "K"}},
                               "tasks": {"grade": {"provider": "groq", "model": "m", "workers": 2}}})
    s = llm.task_settings(cfg, "grade")
    assert (s["type"], s["base_url"], s["model"], s["workers"], s["provider"]) == ("openai", "u", "m", 2, "groq")
    assert llm.task_settings(cfg, "tailor")["type"] == "anthropic"           # unconfigured task default
    bad = SimpleNamespace(llm={"tasks": {"grade": {"provider": "nope"}}})
    with pytest.raises(llm.LLMUnavailable):
        llm.task_settings(bad, "grade")


def test_inline_refs_removes_defs():
    schema = llm._inline_refs(_Outer.model_json_schema())
    assert "$defs" not in schema and "$ref" not in str(schema)
    assert schema["properties"]["items"]["items"]["properties"]["x"]["type"] == "integer"


@pytest.mark.parametrize("raw", [
    '{"x": 1}',
    '<think>let me see {"x": 9}</think>\n{"x": 1}',     # reasoning models
    '```json\n{"x": 1}\n```',                            # fenced output
    'Sure! Here it is: {"x": 1} Hope that helps.',
])
def test_extract_json_handles_local_model_noise(raw):
    assert _Inner.model_validate_json(llm._extract_json(raw)).x == 1


# ---------------- resume change ----------------

def test_new_resume_rescores_everything_except_applied(conn):
    for i in (1, 2, 3):
        db.upsert_seen(conn, job(id=f"a:{i}"))
    db.update(conn, "a:1", stage="graded", fit_score=40, match_score=0.5)
    db.update(conn, "a:2", stage="tailored", fit_score=85, resume_path="r.docx")
    db.update(conn, "a:3", stage="tailored", fit_score=90, resume_path="sent.docx")
    db.set_status(conn, "a:3", "Applied")
    assert db.reset_for_new_resume(conn) == 2
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["a:1"]["stage"] == "new" and rows["a:1"]["fit_score"] is None
    # Unapplied documents get refreshed, but the old file stays linked meanwhile.
    assert rows["a:2"]["stage"] == "new" and rows["a:2"]["resume_path"] == "r.docx"
    # Applied: untouched, because those files are what was actually sent.
    assert rows["a:3"]["stage"] == "tailored" and rows["a:3"]["fit_score"] == 90


def test_resume_change_detection(conn, tmp_path):
    from copilot.pipeline import _check_resume_changed
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"v1")
    cfg = SimpleNamespace(resume=resume)
    db.upsert_seen(conn, job(id="a:1"))
    db.update(conn, "a:1", stage="graded", match_score=0.5, fit_score=60)
    said = []

    _check_resume_changed(cfg, conn, said.append)      # DB scored before tracking existed
    assert conn.execute("SELECT stage FROM jobs").fetchone()[0] == "new" and said

    db.update(conn, "a:1", stage="graded")
    said.clear()
    _check_resume_changed(cfg, conn, said.append)      # same resume: nothing happens
    assert conn.execute("SELECT stage FROM jobs").fetchone()[0] == "graded" and not said

    resume.write_bytes(b"v2")
    _check_resume_changed(cfg, conn, said.append)      # new upload: re-score
    assert conn.execute("SELECT stage FROM jobs").fetchone()[0] == "new" and said


# ---------------- house style, enforced in code ----------------

from copilot.tailor import banned_phrases, plain


def test_plain_turns_machine_typography_into_ascii():
    text = "Cut image 55% (3.27 GB → 1.5 GB) — kept “parity”; real‑time …"
    assert plain(text) == 'Cut image 55% (3.27 GB to 1.5 GB), kept "parity"; real-time ...'
    assert plain(text).isascii()


def test_banned_words_reject_an_edit():
    p = plan(requirements=[{"need": "x", "keywords": ["billing accuracy"], "evidence": []}],
             edits=[edit("E1.1", "Leveraged billing-accuracy checks on a defect worth $500K+.")])
    _, report = apply_plan(MASTER, p)
    assert "banned phrase" in report["rejected"][0]["why"] and "leveraged" in report["rejected"][0]["why"]
    assert banned_phrases("A results-driven, highly motivated engineer") == ["highly motivated", "results-driven"]


def test_tighter_rewrite_is_accepted_without_a_new_keyword():
    # Your prompt asks for concise bullets: a clearly shorter rewrite earns its place.
    original = "Led daily incident management calls as the primary point of contact for offshore teams."
    master = {**MASTER, "experience": [{**MASTER["experience"][0], "bullets": [original]}]}
    p = plan(requirements=[{"need": "x", "keywords": ["kubernetes"], "evidence": []}],
             edits=[edit("E1.1", "Led daily incident calls as primary contact for offshore teams.")])
    out, report = apply_plan(master, p)
    assert report["accepted"] and out["experience"][0]["bullets"][0].startswith("Led daily incident calls")


def test_edit_output_is_cleaned_to_ascii_before_checks():
    p = plan(requirements=[{"need": "x", "keywords": ["billing accuracy"], "evidence": []}],
             edits=[edit("E1.1", "Fixed a billing‑accuracy defect worth $500K+ in revenue — fast.")])
    out, _ = apply_plan(MASTER, p)
    assert out["experience"][0]["bullets"][0].isascii()


# ---------------- cover letter rules ----------------

from copilot.tailor import letter_problems

WRITING = {"letter_words": [5, 60], "letter_points": ["open to relocating anywhere in the US"]}


def test_honest_gap_sentence_is_allowed_but_a_false_claim_is_flagged():
    honest = "Dear Hiring Team, I fixed a $500K+ defect. I haven't used Kubernetes yet and would ramp up quickly. " \
             "I am open to relocating anywhere in the US."
    claim = "Dear Hiring Team, I fixed a $500K+ defect and ran Kubernetes clusters. " \
            "I am open to relocating anywhere in the US."
    assert not any("kubernetes" in f for f in letter_problems(MASTER, honest, "", "", WRITING))
    assert any("kubernetes" in f for f in letter_problems(MASTER, claim, "", "", WRITING))


def test_letter_word_count_banned_phrases_and_points_are_checked():
    short = "I am thrilled."
    probs = letter_problems(MASTER, short, "", "", WRITING)
    assert any("words; aim for" in f for f in probs)
    assert any("banned phrase: i am thrilled" in f for f in probs)
    assert any("missing your point" in f for f in probs)


def test_coursework_list_allows_coursework_mentions_only():
    from copilot.tailor import coursework_note, letter_block
    w = {"letter_words": [5, 80], "letter_points": [], "coursework": ["Kubernetes"]}
    letter = "Dear Hiring Team, I studied Kubernetes in coursework and have not used it professionally."
    assert not any("kubernetes" in f for f in letter_problems(MASTER, letter, "", coursework_note(w), w))
    assert "never present it as professional" in letter_block(w).lower()


# ---------------- manual jobs ----------------

def test_manual_job_is_never_marked_closed_by_a_crawl(conn):
    db.upsert_seen(conn, job(id="manual:abc", source="manual", company="Acme"))
    db.upsert_seen(conn, job(id="ashby:acme:1", company="Acme"))
    assert db.mark_closed(conn, {"Acme"}, seen_ids=set()) == 1     # only the crawled one
    active = dict(conn.execute("SELECT id, is_active FROM jobs").fetchall())
    assert active == {"manual:abc": 1, "ashby:acme:1": 0}


def test_add_manual_job_scores_it_and_skips_the_filters(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from copilot import grade, pipeline, scoring
    from copilot.config import load_config

    cfg = load_config()
    cfg.data_dir = tmp_path
    resume = tmp_path / "r.txt"
    resume.write_text("resume", encoding="utf-8")
    monkeypatch.setattr(pipeline.inputs, "apply", lambda c: c)
    monkeypatch.setattr(pipeline.inputs, "read_text", lambda p: "Python engineer resume")
    monkeypatch.setattr(scoring, "match", lambda r, d: (0.12, ["python"], ["go"]))   # below the floor
    monkeypatch.setattr(grade, "grade", lambda r, j, s: (SimpleNamespace(fit=91, reason="strong", dealbreakers=[]), None))
    monkeypatch.setattr(scoring, "sponsorship",
                        lambda d, s: {"status": "sponsors", "evidence": "We sponsor visas.", "reasoning": "", "verified": True})
    monkeypatch.setattr(scoring, "final", lambda r, d, c, jd: {"basis": "posting says sponsors", "multiplier": 1.0, "final": 0.12})

    # A "Senior" title would fail the prefilter, and the match is below the floor:
    # neither applies to a job you added yourself.
    jid = pipeline.add_manual_job(cfg, company="Acme", title="Senior Staff Engineer", url="https://x/job",
                                  description="We build things. " * 20, say=lambda m: None)
    c = db.connect(cfg.db_path)
    row = db.decode(c.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone())
    assert jid.startswith("manual:") and row["source"] == "manual"
    assert (row["stage"], row["fit_score"], row["sponsorship_basis"]) == ("shortlisted", 91, "posting says sponsors")
    assert (cfg.data_dir / "jobs.xlsx").exists()
    # Re-adding the same link updates the same job instead of duplicating it.
    assert pipeline.add_manual_job(cfg, company="Acme", title="Senior Staff Engineer", url="https://x/job",
                                   description="Updated text. " * 20, say=lambda m: None) == jid
    assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


# ---------------- feedback loop ----------------

def _pack(tmp_path, monkeypatch):
    """A tailored job in a temp DB, rendered as Word, ready for feedback."""
    import json as _json
    from copilot import pipeline
    from copilot.config import load_config
    cfg = load_config()
    cfg.data_dir = tmp_path
    cfg.resume = tmp_path / "resume.pdf"
    monkeypatch.setattr(pipeline.inputs, "apply", lambda c: c)
    (tmp_path / "master_resume.json").write_text(_json.dumps(MASTER), encoding="utf-8")
    resume = copy.deepcopy(MASTER)
    new = "Fixed a billing-accuracy defect worth $500K+ in revenue."
    resume["experience"][0]["bullets"][0] = new
    report = {"requirements": [], "accepted": [{"id": "E1.1", "before": MASTER["experience"][0]["bullets"][0],
                                                "after": new, "reason": "test"}],
              "rejected": [], "coverage": {"before": 0, "after": 0, "total": 0, "missing": []}, "writer": "m"}
    c = db.connect(cfg.db_path)
    db.upsert_seen(c, job(id="j1"))
    db.update(c, "j1", stage="tailored", tailored_resume=resume, tailor_report=report,
              resume_path=str(tmp_path / "r.docx"), review_resume_path=str(tmp_path / "rev.docx"))
    c.commit()
    return cfg, c, new


def test_reject_restores_your_original_and_logs_it(tmp_path, monkeypatch):
    from copilot import pipeline
    cfg, c, new = _pack(tmp_path, monkeypatch)
    pipeline.apply_feedback(cfg, "j1", "E1.1", "reject", source="test")
    row = db.decode(c.execute("SELECT * FROM jobs WHERE id='j1'").fetchone())
    assert row["tailored_resume"]["experience"][0]["bullets"][0] == MASTER["experience"][0]["bullets"][0]
    assert row["tailor_report"]["accepted"] == [] and (tmp_path / "r.docx").exists()
    assert db.feedback_stats(c) == {"total": 1, "keep": 0, "rewrite": 0, "reject": 1, "keep_rate": 0.0}
    assert db.feedback_examples(c)["rejected"][0]["proposed"] == new


def test_rewrite_uses_your_words_and_becomes_a_style_example(tmp_path, monkeypatch):
    from copilot import pipeline
    from copilot.tailor import examples_block
    cfg, c, _ = _pack(tmp_path, monkeypatch)
    mine = "Caught a billing defect worth $500K+ before release."
    pipeline.apply_feedback(cfg, "j1", "E1.1", "rewrite", mine, source="test")
    row = db.decode(c.execute("SELECT * FROM jobs WHERE id='j1'").fetchone())
    assert row["tailored_resume"]["experience"][0]["bullets"][0] == mine
    assert row["tailor_report"]["accepted"][0]["verdict"] == "rewrite"
    assert mine in examples_block(db.feedback_examples(c))


def test_keep_changes_nothing_but_counts(tmp_path, monkeypatch):
    from copilot import pipeline
    cfg, c, new = _pack(tmp_path, monkeypatch)
    pipeline.apply_feedback(cfg, "j1", "E1.1", "keep", source="test")
    row = db.decode(c.execute("SELECT * FROM jobs WHERE id='j1'").fetchone())
    assert row["tailored_resume"]["experience"][0]["bullets"][0] == new
    assert db.feedback_stats(c)["keep_rate"] == 1.0
