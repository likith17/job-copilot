"""Document generation is only 'ready' when the files are really there, and a failure says why."""
import pytest

from copilot import db, llm, pipeline, tailor
from copilot.config import Config
from copilot.documents import render_cover_letter, render_resume

RESUME = {
    "contact": {"name": "Pat Example", "location": "Albany, NY", "phone": "", "email": "pat@example.com", "links": []},
    "headline": "", "summary": "Engineer who builds routing systems for emergency response teams. " * 3,
    "education": [{"school": "Example University", "degree": "M.S.", "dates": "2026", "details": []}],
    "skills": [{"category": "Languages", "items": ["Python", "C++"]}],
    "experience": [{"title": "Engineer", "company": "Acme", "dates": "2021-2023",
                    "bullets": ["Built emergency routing with YOLOv12 across 371 cameras, cutting response time 20%."] * 4}],
    "projects": [], "publications": [],
}


def test_real_documents_pass_validation(tmp_path):
    resume, letter = tmp_path / "r.docx", tmp_path / "l.docx"
    pipeline._atomic(render_resume, resume, RESUME)
    pipeline._atomic(render_cover_letter, letter, "Dear team,\n\n" + "I build reliable systems. " * 30,
                     RESUME["contact"], "Acme")
    pipeline.validate_documents(resume, letter, "Pat Example")
    assert not list(tmp_path.glob("*.part*"))          # nothing half-written left behind


@pytest.mark.parametrize("content,why", [(None, "not written"), (b"x" * 5000, "can't be opened")])
def test_missing_or_corrupt_files_fail_with_a_reason(tmp_path, content, why):
    path = tmp_path / "resume.pdf"
    if content:
        path.write_bytes(content)
    with pytest.raises(pipeline.DocumentError, match=why):
        pipeline.validate_documents(path, None, "Pat Example")


def test_a_resume_without_your_name_is_rejected(tmp_path):
    path = tmp_path / "r.docx"
    render_resume(RESUME, path)
    with pytest.raises(pipeline.DocumentError, match="name"):
        pipeline.validate_documents(path, None, "Someone Else")


def test_audit_drops_flags_for_things_the_resume_already_says(monkeypatch):
    claims = ["YOLOv12", "371 cameras", '"cutting response time 20%"', "managed a team of forty engineers"]
    monkeypatch.setattr(llm, "parse", lambda *a, **k: (tailor.ClaimAudit(unsupported=claims), None))
    flags = tailor.audit_claims(RESUME, "edited text", "", {"type": "x"})
    assert flags == ["unsupported claim: managed a team of forty engineers"]


def _job(conn, job_id, **fields):
    db.upsert_seen(conn, {"id": job_id, "source": "lever", "company": "Acme", "title": "Engineer", "location": "",
                          "country": "", "department": "", "url": "u", "apply_url": "u",
                          "description": "x", "posted_at": None})
    db.update(conn, job_id, stage="shortlisted", **fields)
    conn.commit()
    return db.decode(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


@pytest.fixture
def setup(tmp_path, monkeypatch):
    resume = tmp_path / "r.tex"
    resume.write_text("x", encoding="utf-8")
    cfg = Config(resume=resume, companies=tmp_path / "c.yaml", data_dir=tmp_path)
    cfg.llm = {"tasks": {"tailor": {"provider": "local", "model": "m"}}}
    conn = db.connect(cfg.db_path)
    monkeypatch.setattr(tailor, "load_or_build_master", lambda *a, **k: None)
    monkeypatch.setattr(llm, "preflight", lambda s, say=None: s)
    return cfg, conn


def test_a_failed_generation_records_the_error_and_keeps_old_documents(setup, monkeypatch):
    cfg, conn = setup
    old = _job(conn, "has-docs", resume_path="old.pdf", doc_status="ready")
    new = _job(conn, "no-docs")

    def boom(ctx, job):
        raise pipeline.DocumentError("the resume file was not written (x.pdf)")
    monkeypatch.setattr(pipeline, "_application", boom)
    stats = {"tailored": 0, "errors": 0}
    pipeline._make_applications(cfg, conn, "resume", [old, new], 2, force=True, stats=stats, say=lambda *_: None)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert stats["errors"] == 2
    assert rows["has-docs"]["doc_status"] == "failed" and "not written" in rows["has-docs"]["doc_error"]
    assert rows["has-docs"]["resume_path"] == "old.pdf" and rows["has-docs"]["stage"] == "shortlisted"
    assert rows["no-docs"]["stage"] == "error"


def test_jobs_left_generating_are_marked_failed_when_the_model_stops(setup, monkeypatch):
    cfg, conn = setup
    job = _job(conn, "j1")

    def unavailable(ctx, job):
        raise llm.LLMUnavailable("model is gone")
    monkeypatch.setattr(pipeline, "_application", unavailable)
    with pytest.raises(llm.LLMUnavailable):
        pipeline._make_applications(cfg, conn, "resume", [job], 1, force=True,
                                    stats={"tailored": 0, "errors": 0}, say=lambda *_: None)
    row = conn.execute("SELECT doc_status, doc_error FROM jobs").fetchone()
    assert row["doc_status"] == "failed" and "unavailable" in row["doc_error"]
