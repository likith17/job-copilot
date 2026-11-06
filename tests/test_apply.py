"""Autofill support: job lookup by URL, answer bank, and the local API's access rules."""
import json
import urllib.error
import urllib.request

import pytest

from copilot import apply, applyserver, db
from copilot.config import Config


@pytest.fixture
def cfg(tmp_path):
    c = Config(resume=tmp_path / 'r.pdf', companies=tmp_path / 'c.yaml', data_dir=tmp_path)
    conn = db.connect(c.db_path)
    for job_id, url, apply_url in [
        ("greenhouse:nuro:8248317", "https://nuro.ai/careersitem?gh_jid=8248317",
         "https://job-boards.greenhouse.io/embed/job_app?for=nuro&token=8248317"),
        ("lever:zoox:e8a190f0-7a89-4c11-a6a0-452ff6234749", "https://jobs.lever.co/zoox/e8a190f0-7a89-4c11-a6a0-452ff6234749",
         "https://jobs.lever.co/zoox/e8a190f0-7a89-4c11-a6a0-452ff6234749/apply"),
        ("ashby:sierra:ddae4a0c-2f6c-4cc3-80aa-da74a0dd667a", "https://jobs.ashbyhq.com/sierra/ddae4a0c-2f6c-4cc3-80aa-da74a0dd667a",
         "https://jobs.ashbyhq.com/sierra/ddae4a0c-2f6c-4cc3-80aa-da74a0dd667a/application"),
    ]:
        db.upsert_seen(conn, {"id": job_id, "source": job_id.split(":")[0], "company": job_id.split(":")[1].title(),
                              "title": "Engineer", "location": "", "country": "", "department": "", "url": url,
                              "apply_url": apply_url, "description": "x", "posted_at": None})
    conn.commit()
    conn.close()
    return c


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/embed/job_app?for=nuro&token=8248317", "greenhouse:nuro:8248317"),
    ("https://job-boards.greenhouse.io/nuro/jobs/8248317?gh_src=x", "greenhouse:nuro:8248317"),
    ("https://jobs.lever.co/zoox/e8a190f0-7a89-4c11-a6a0-452ff6234749/apply", "lever:zoox:e8a190f0-7a89-4c11-a6a0-452ff6234749"),
    ("https://jobs.ashbyhq.com/sierra/ddae4a0c-2f6c-4cc3-80aa-da74a0dd667a/application",
     "ashby:sierra:ddae4a0c-2f6c-4cc3-80aa-da74a0dd667a"),
    ("https://jobs.lever.co/zoox/00000000-0000-0000-0000-000000000000/apply", None),
])
def test_job_for_url(cfg, url, expected):
    job = apply.job_for_url(db.connect(cfg.db_path), url)
    assert (job["id"] if job else None) == expected


def test_answer_bank_reuses_similar_questions(cfg):
    conn = db.connect(cfg.db_path)
    db.save_answer(conn, "Why do you want to work at Nuro?", "Because of X.", job_id="greenhouse:nuro:8248317")
    assert db.find_answer(conn, "Why do you want to work at Nuro?*")["answer"] == "Because of X."
    assert db.find_answer(conn, "What is your desired salary?") is None
    db.save_answer(conn, "Why do you want to work at Nuro?", "Because of Y.")     # updates, no duplicate
    assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 1


def test_choice_questions_are_never_drafted(cfg, monkeypatch):
    monkeypatch.setattr(apply, "load_profile", lambda c: {"custom_answers": {}})
    out = apply.answer_question(cfg, db.connect(cfg.db_path), "Do you have a security clearance?",
                                kind="select", options=["Yes", "No"])
    assert out["source"] == "" and out["answer"] == ""


def test_profile_custom_answers_win(cfg, monkeypatch):
    monkeypatch.setattr(apply, "load_profile", lambda c: {"custom_answers": {"open to working in-office": "Yes"}})
    out = apply.answer_question(cfg, db.connect(cfg.db_path), "Are you open to working in-office 3 days a week?")
    assert out == {"answer": "Yes", "source": "profile", "note": "custom_answers: open to working in-office"}


@pytest.fixture
def server(cfg):
    srv = applyserver.serve(cfg, port=0, background=True)
    yield cfg, srv.server_address[1], applyserver.get_token(cfg)
    srv.shutdown()


def _get(port, path, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_requires_token_and_rejects_web_pages(server):
    cfg, port, token = server
    assert _get(port, "/api/ping", {})[0] == 401
    assert _get(port, "/api/ping", {"X-Copilot-Token": "wrong"})[0] == 401
    # A website that somehow learned the token still can't call it from a page.
    assert _get(port, "/api/ping", {"X-Copilot-Token": token, "Origin": "https://evil.example"})[0] == 401
    assert _get(port, "/api/ping", {"X-Copilot-Token": token, "Origin": "chrome-extension://abc"}) == \
        (200, {"ok": True, "app": "job-copilot"})


def test_api_job_lookup_and_applied(server):
    cfg, port, token = server
    h = {"X-Copilot-Token": token}
    status, body = _get(port, "/api/job?url=https%3A%2F%2Fjobs.lever.co%2Fzoox%2Fe8a190f0-7a89-4c11-a6a0-452ff6234749%2Fapply", h)
    assert status == 200 and body["job"]["company"] == "Zoox" and body["job"]["documents"] == {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/applied", method="POST", headers={**h, "Content-Type": "application/json"},
                                 data=json.dumps({"job_id": "lever:zoox:e8a190f0-7a89-4c11-a6a0-452ff6234749", "ats": "lever",
                                                  "filled": [{"label": "Email", "value": "x", "source": "profile"}],
                                                  "answers": [{"question": "Earliest start date?", "answer": "Immediately"}]}).encode())
    with urllib.request.urlopen(req) as r:
        assert r.status == 200
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT status, applied_at FROM jobs WHERE source='lever'").fetchone()["status"] == "Applied"
    assert conn.execute("SELECT outcome FROM applications").fetchone()["outcome"] == "submitted"
    assert db.find_answer(conn, "Earliest start date?")["answer"] == "Immediately"


def test_flagged_cover_letter_is_never_uploaded(tmp_path):
    resume, letter = tmp_path / "r.pdf", tmp_path / "l.pdf"
    resume.write_bytes(b"r")
    letter.write_bytes(b"l")
    job = {"resume_path": str(resume), "cover_letter_path": str(letter), "tailor_flags": [], "tailor_report": {}}
    assert set(apply.documents(job)) == {"resume", "cover_letter"}
    flagged = {**job, "tailor_flags": ["unsupported claim: x"], "tailor_report": {"letter_flags": ["unsupported claim: x"]}}
    assert set(apply.documents(flagged)) == {"resume"}
    # Editing the letter clears its flags, and then it goes out.
    assert set(apply.documents({**flagged, "tailor_flags": []})) == {"resume", "cover_letter"}


def test_only_a_page_confirmation_unlocks_sending(cfg):
    conn = db.connect(cfg.db_path)
    assert db.confirmed_submissions(conn) == 0
    db.log_application(conn, "j", "u", "lever", "extension", "submitted", note="marked by hand")
    db.log_application(conn, "j", "u", "lever", "prepared", "needs_you", note="field empty")
    assert db.confirmed_submissions(conn) == 0
    db.log_application(conn, "j", "u", "lever", "prepared", "submitted", note="you submitted")
    assert db.confirmed_submissions(conn) == 1


def test_daily_switch_saves_to_config_and_keeps_comments(tmp_path):
    from copilot import autoapply
    path = tmp_path / "config.yaml"
    path.write_text("apply:\n  min_fit: 80\n  # keep me\n  auto_submit: false  # a note\n", encoding="utf-8")
    autoapply.set_auto_submit(True, path)
    assert path.read_text(encoding="utf-8") == "apply:\n  min_fit: 80\n  # keep me\n  auto_submit: true  # a note\n"
    autoapply.set_auto_submit(False, path)
    assert "auto_submit: false  # a note" in path.read_text(encoding="utf-8")
    path.write_text("apply: {}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        autoapply.set_auto_submit(True, path)


def test_drafts_use_the_real_resume_story_and_letter(cfg, monkeypatch):
    """No stand-in for the resume: this runs the same path as a live form."""
    from copilot import llm
    tex = (__import__("pathlib").Path(__file__).parent / "fixtures_resume.tex")
    cfg.resume = tex
    apply.story_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    apply.story_path(cfg).write_text("I want to build AI systems that help people in emergencies.", encoding="utf-8")
    monkeypatch.setattr(apply, "load_profile", lambda c: {"custom_answers": {}})
    seen = {}

    def fake_parse(settings, system, user, schema, max_tokens=0):
        seen.setdefault("calls", []).append((system[0]["text"], user))
        if schema is apply.Answer:
            return apply.Answer(answer="I want to work on safety because I built routing for emergencies.",
                                needs_user=False, reason="r"), None
        return schema(unsupported=[]), None
    monkeypatch.setattr(llm, "parse", fake_parse)
    job = {"company": "Acme", "title": "Engineer", "description": "Acme builds safe AI for hospitals."}
    out = apply.answer_question(cfg, db.connect(cfg.db_path), "Why Acme?", job, "textarea", None, settings={"type": "x"})
    assert out["source"] == "draft" and out["checked"] is True
    system, user = seen["calls"][0]
    assert "motivation question" in system and "help people in emergencies" in user and "Acme builds safe AI" in user


def test_motivation_answers_are_never_reused_for_another_company(cfg):
    assert apply.is_motivation("Why Anthropic?") and apply.is_motivation("What interests you about this role?")
    assert not apply.is_motivation("Are you authorized to work in the US?")
    from copilot import applyserver
    conn = db.connect(cfg.db_path)
    applyserver.r_answers(cfg, conn, {}, {"answers": [{"question": "Why Anthropic?", "answer": "Because of Claude."},
                                                      {"question": "Preferred office?", "answer": "NYC"}]})
    assert db.find_answer(conn, "Why Anthropic?") is None and db.find_answer(conn, "Preferred office?")["answer"] == "NYC"


def test_checked_ai_answers_go_out_only_when_allowed():
    from copilot import autoapply
    job = {"tailor_flags": [], "tailored_fit": 90, "sponsorship_status": "sponsors"}
    state = {"filled": [{"label": "Attach", "value": "r", "source": "documents", "kind": "resume"}],
             "drafts": [{"label": "Why us?", "checked": True}], "needs": [], "errors": [], "captcha": False}
    opts = {"min_fit": 85, "daily_cap": 15}
    assert any("sending them is off" in w for w in autoapply.gates(job, state, opts, 0))
    assert autoapply.gates(job, state, {**opts, "send_ai_answers": True}, 0) == []
    unchecked = {**state, "drafts": [{"label": "Why us?", "checked": False}]}
    assert any("need your review" in w for w in autoapply.gates(job, unchecked, {**opts, "send_ai_answers": True}, 0))


def test_extension_profile_rules():
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which("node"):
        pytest.skip("node not installed")
    script = Path(__file__).parent / "extension_rules.mjs"
    out = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_submit_gates():
    from copilot import autoapply
    opts = {"min_fit": 85, "daily_cap": 15}
    job = {"tailor_flags": [], "tailored_fit": 90, "sponsorship_status": "sponsors"}
    clean = {"filled": [{"label": "Attach", "value": "r.pdf", "source": "documents", "kind": "resume"}], "drafts": [], "needs": [],
             "errors": [], "captcha": False}
    assert autoapply.gates(job, clean, opts, 0) == []
    assert autoapply.gates(None, clean, opts, 0) == ["not in your job DB"]
    for change, why in [({"drafts": [{"label": "Why us?"}]}, "AI-drafted"), ({"needs": ["Start date"]}, "need you"),
                        ({"captcha": True}, "CAPTCHA"), ({"filled": []}, "resume not attached")]:
        assert any(why in w for w in autoapply.gates(job, {**clean, **change}, opts, 0)), why
    assert any("flag" in w for w in autoapply.gates({**job, "tailor_flags": ["x"]}, clean, opts, 0))
    # A flagged cover letter isn't attached, so it doesn't hold the application back.
    held = {**job, "tailor_flags": ["bad claim"], "tailor_report": {"letter_flags": ["bad claim"]}}
    assert autoapply.gates(held, clean, opts, 0) == []
    assert any("fit" in w for w in autoapply.gates({**job, "tailored_fit": 80}, clean, opts, 0))
    assert any("sponsorship" in w for w in autoapply.gates({**job, "sponsorship_status": "no_sponsorship"}, clean, opts, 0))
    assert any("cap" in w for w in autoapply.gates(job, clean, opts, 15))
    # A cover letter alone isn't a resume; old documents and a busy company day hold it back.
    letter = {**clean, "filled": [{"label": "Cover", "value": "c.pdf", "source": "documents", "kind": "cover_letter"}]}
    assert any("resume not attached" in w for w in autoapply.gates(job, letter, opts, 0))
    assert any("older resume" in w for w in autoapply.gates({**job, "docs_resume_sha": "old"}, clean, opts, 0, "new"))
    assert autoapply.gates({**job, "docs_resume_sha": "new"}, clean, opts, 0, "new") == []
    assert any("already sent" in w for w in autoapply.gates(job, clean, {**opts, "per_company_per_day": 2}, 0, None, 2))
    assert any("status is Applied" in w for w in autoapply.gates({**job, "status": "Applied"}, clean, opts, 0))


def test_unanswered_questions_reach_the_inbox_once_answered_leave_it(cfg, monkeypatch):
    monkeypatch.setattr(apply, "load_profile", lambda c: {"custom_answers": {}})
    conn = db.connect(cfg.db_path)
    for _ in range(2):
        out = apply.answer_question(cfg, conn, "Referral name", kind="text", draft=False, required=False)
        assert out["answer"] == ""
    apply.answer_question(cfg, conn, "Security clearance?", kind="select", options=["Yes", "No"], required=True)
    inbox = db.open_questions(conn)
    assert [(q["question"], q["seen"]) for q in inbox] == [("Security clearance?", 1), ("Referral name", 2)]
    assert inbox[0]["options"] == ["Yes", "No"]
    db.save_answer(conn, "Security clearance?", "No")
    db.save_answer(conn, "Referral name", "")          # skip
    assert db.open_questions(conn) == []
    assert apply.answer_question(cfg, conn, "Security clearance?", options=["Yes", "No"])["answer"] == "No"
    assert apply.answer_question(cfg, conn, "Referral name", draft=False)["note"] == "you chose to skip this question"
