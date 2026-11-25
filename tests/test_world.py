"""Worldwide search, early-career filtering, faster crawling, and cover letters from your story."""
from datetime import datetime, timezone

import pytest

from copilot import db, fetch, grade, inputs, letter, llm, pipeline, shortlist, tailor
from copilot.config import Config

ENGLISH = "You will build backend services with our team and ship them to users every week. " * 5


def job(**kw):
    return {"id": "j", "title": "Software Engineer", "location": "London, UK", "country": None,
            "description": ENGLISH, "posted_at": None, **kw}


@pytest.mark.parametrize("location,country,region", [
    ("London, UK", None, "world"), ("Bengaluru, India", None, "world"), ("Austin, TX", None, "us"),
    ("Remote - US", None, "us"), ("Berlin", "Germany", "world"), ("", "United States", "us")])
def test_region(location, country, region):
    assert fetch.region(job(location=location, country=country)) == region


def test_regions_rule_decides_what_is_kept():
    assert fetch.prefilter_reason(job(), {"regions": ["us"]}).startswith("location")
    assert fetch.prefilter_reason(job(), {"regions": ["us", "world"]}) is None
    assert fetch.prefilter_reason(job(), {"us_only": True}).startswith("location")      # old configs still work


def test_postings_not_in_english_are_skipped():
    german = "Wir suchen eine Softwareentwicklerin für unser Team in Berlin. Du arbeitest mit Python und Go. " * 6
    assert fetch.prefilter_reason(job(description=german), {"regions": ["world"]}).startswith("language")


@pytest.mark.parametrize("title,description,level", [
    ("Software Engineering Intern, Summer 2027", "", "intern"),
    ("Machine Learning Engineer - New Grad", "", "new grad"),
    ("Software Engineer I", "", "new grad"),
    ("Software Engineer II", "Requires 2+ years of experience.", "2+ yrs"),
    ("Junior Data Engineer", "", "new grad"),
    ("Backend Engineer", "", "not stated"),
])
def test_level(title, description, level):
    assert fetch.level({"title": title, "description": description}) == level


def test_years_written_as_words_count():
    assert fetch.min_years_required("At least five years of professional experience required.") == 5
    rules = {"regions": ["world"], "max_years_required": 4}
    assert fetch.prefilter_reason(job(description=ENGLISH + " Five or more years of experience."), rules)


def test_internships_are_no_longer_excluded_by_the_config():
    from copilot.config import load_config
    rules = load_config().prefilter
    assert fetch.prefilter_reason(job(title="Software Engineer Intern", location="New York, NY"), rules) is None
    assert fetch.prefilter_reason(job(title="Senior Software Engineer", location="New York, NY"), rules)


def conn_for(tmp_path, **kw):
    cfg = Config(resume=tmp_path / "r.tex", companies=tmp_path / "c", data_dir=tmp_path, **kw)
    return cfg, db.connect(cfg.db_path)


def posting(i, **kw):
    return {"id": f"p{i}", "source": "lever", "company": "Acme", "title": "Engineer", "location": "London, UK",
            "country": None, "department": "", "url": "u", "apply_url": "u", "description": ENGLISH,
            "posted_at": None, **kw}


def test_bulk_upsert_writes_only_what_changed(tmp_path):
    _, conn = conn_for(tmp_path)
    assert db.upsert_many(conn, [posting(1), posting(2), posting(3)]) == 3
    conn.execute("UPDATE jobs SET stage = 'filtered_out', description = 'old' WHERE id = 'p1'")
    conn.execute("UPDATE jobs SET is_active = 0 WHERE id = 'p3'")
    before = conn.total_changes
    assert db.upsert_many(conn, [posting(1), posting(2), posting(3), posting(4)]) == 1
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["p1"]["description"] == "old"            # filtered out: not rewritten
    assert rows["p3"]["is_active"] == 1                   # back on the board
    assert conn.total_changes - before == 2               # the new posting and the reopened one only


def test_changed_filters_recheck_stored_postings(tmp_path):
    cfg, conn = conn_for(tmp_path, prefilter={"regions": ["us"]})
    db.upsert_many(conn, [posting(1), posting(2, location="Boston, MA")])
    stats = {"filtered_out": 0}
    pipeline._prefilter(cfg, conn, stats, lambda *_: None)
    stage = {r["id"]: r["stage"] for r in conn.execute("SELECT id, stage FROM jobs")}
    assert stage == {"p1": "filtered_out", "p2": "pending"}
    cfg.prefilter = {"regions": ["us", "world"]}
    pipeline._prefilter(cfg, conn, {"filtered_out": 0}, lambda *_: None)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert rows["p1"]["stage"] == "pending" and rows["p1"]["region"] == "world"
    assert rows["p2"]["region"] == "us"


def test_grading_takes_turns_between_regions():
    jobs = [{"id": f"u{i}", "region": "us"} for i in range(5)] + [{"id": "w0", "region": "world"}]
    assert [j["id"] for j in pipeline._balanced(jobs, 3)] == ["u0", "w0", "u1"]


def test_each_region_gets_its_own_list(tmp_path):
    cfg, conn = conn_for(tmp_path, prefilter={"regions": ["us", "world"]})
    for i, (loc, region) in enumerate([("Boston, MA", "us"), ("London, UK", "world"), ("Paris", "world")]):
        db.upsert_many(conn, [posting(i, location=loc, company=f"C{i}")])
        db.update(conn, f"p{i}", stage="graded", fit_score=80 + i, region=region, match_score=0.5)
    conn.commit()
    shortlist.refresh(conn, cfg)
    assert [r["job_id"] for r in shortlist.today(conn, cfg, "us")] == ["p0"]
    assert [r["job_id"] for r in shortlist.today(conn, cfg, "world")] == ["p2", "p1"]
    assert len(shortlist.today(conn, cfg)) == 3


def test_grading_reads_the_job_not_the_boilerplate():
    text = ("About the role\nBuild APIs in Go.\n\nRequirements\n- Go and Postgres\n\nBenefits\nFree lunch and 401k.\n\n"
            "We are an equal opportunity employer and value diversity.\n") + "Ship features with the team. " * 20
    trimmed = grade.trim_posting(text)
    assert "Go and Postgres" in trimmed and "Free lunch" not in trimmed and "equal opportunity" not in trimmed


TEMPLATE = ("The thread is learning. It is what I would bring to the [Role] position at [Company].\n\n"
            "At TCS I learned a lot.\n\n"
            "[Company-specific sentence explaining what interests you about the product.]\n\n"
            "I would welcome the opportunity to talk.")
MASTER = {"contact": {"name": "Pat Example"}, "summary": "", "experience": [], "projects": []}
JOB = {"title": "Software Engineer, New Grad (2027) - Remote", "company": "Acme", "description": ENGLISH}


def test_the_letter_keeps_your_words_and_fills_the_blanks(monkeypatch):
    monkeypatch.setattr(llm, "parse", lambda *a, **k: (letter.CompanyLine(
        sentence="Acme's work on backend services for its users connects to the production support I did at TCS.",
        connection="the production support I did at TCS"), None))
    monkeypatch.setattr(tailor, "audit_claims", lambda *a, **k: [])
    text, notes = letter.from_template(TEMPLATE, JOB, MASTER, {}, {}, "")
    assert "the Software Engineer, New Grad position at Acme" in text
    assert "At TCS I learned a lot." in text and "Acme's work on backend services" in text
    assert text.startswith("Dear Acme Hiring Team,") and text.endswith("Sincerely,\nPat Example")
    assert "[" not in text and not notes


def test_a_flagged_company_sentence_is_left_out(monkeypatch):
    monkeypatch.setattr(llm, "parse", lambda *a, **k: (letter.CompanyLine(
        sentence="Acme ships backend services, and I led a Kubernetes migration for ten thousand users last year.",
        connection="I led a Kubernetes migration for ten thousand users last year"), None))
    monkeypatch.setattr(tailor, "audit_claims", lambda *a, **k: ["unsupported claim: Kubernetes migration"])
    text, notes = letter.from_template(TEMPLATE, JOB, MASTER, {}, {}, "")
    assert "Kubernetes" not in text and "company-specific" not in text and "left out" in notes[0]
    assert "At TCS I learned a lot.\n\nI would welcome" in text


def test_story_placeholders_never_reach_an_answer():
    assert "[" not in letter.for_answers(TEMPLATE) and "[" not in letter.for_answers(TEMPLATE, JOB)
    assert letter.for_answers("Plain story.") == "Plain story."


def test_projects_file(tmp_path):
    cfg = Config(resume=tmp_path / "r", companies=tmp_path / "c", data_dir=tmp_path)
    inputs.projects_path(cfg).parent.mkdir(parents=True)
    inputs.projects_path(cfg).write_text("# notes\n## FireSense: NASA\n- Node.js atlas\n\n## Empty\n", encoding="utf-8")
    assert inputs.projects(cfg) == {"FireSense: NASA": "- Node.js atlas"}


def test_company_facts_must_come_from_the_posting(monkeypatch):
    monkeypatch.setattr(llm, "parse", lambda *a, **k: (letter.CompanyLine(
        sentence="Acme's billion-dollar quantum satellites inspire me, which connects to my production support at TCS.",
        connection="my production support at TCS"), None))
    monkeypatch.setattr(tailor, "audit_claims", lambda *a, **k: [])
    text, notes = letter.from_template(TEMPLATE, JOB, MASTER, {}, {}, "")
    assert "quantum" not in text and "doesn't" in notes[0]


@pytest.mark.parametrize("title", ["Software Engineer 3", "Software Engineer III", "Engineer IV, Platform", "SWE L5"])
def test_senior_level_numbers_are_dropped(title):
    assert fetch.prefilter_reason(job(title=title), {"regions": ["world"]}).startswith("title")
