"""Today's shortlist: fresh, varied, deduplicated, and not the same jobs every day."""
from datetime import datetime, timedelta, timezone

from copilot import db, shortlist
from copilot.config import Config

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def job(i, fit=80, company=None, title="Software Engineer", age=10, **extra):
    return {"id": f"j{i}", "company": company or f"Co{i}", "title": title, "description": "", "fit_score": fit,
            "match_score": 0.6, "is_active": 1, "status": "New", "sponsorship_basis": "unknown",
            "posted_at": (NOW - timedelta(days=age)).isoformat(), "first_seen": NOW.isoformat(), **extra}


def ids(picked):
    return [e["job"]["id"] for e in picked]


def test_eleven_jobs_best_first():
    picked = shortlist.build([job(i, fit=60 + i, title=t) for i, t in enumerate(
        ["Software Engineer", "Backend Engineer", "AI Engineer", "Data Engineer", "ML Engineer"] * 4)], now=NOW)
    assert len(picked) == 11 and picked[0]["job"]["fit_score"] == 79
    assert [e["score"] for e in picked] == sorted((e["score"] for e in picked), reverse=True)


def test_fresh_postings_beat_slightly_better_stale_ones():
    picked = shortlist.build([job(1, fit=84, age=40), job(2, fit=80, age=0.5)], now=NOW)
    assert ids(picked) == ["j2", "j1"] and "posted in the last 24 hours" in picked[0]["why"]


def test_applied_closed_low_and_no_sponsorship_jobs_are_left_out():
    jobs = [job(1, status="Applied"), job(2, is_active=0), job(3, fit=40), job(4, status="Skipped"),
            job(5, sponsorship_basis="posting rules it out"), job(6, fit=None), job(7)]
    assert ids(shortlist.build(jobs, now=NOW)) == ["j7"]


def test_unknown_sponsorship_stays_and_known_sponsorship_helps():
    picked = shortlist.build([job(1), job(2, sponsorship_basis="posting says sponsors")], now=NOW)
    assert ids(picked) == ["j2", "j1"]


def test_same_role_in_several_cities_counts_once_and_one_per_company():
    jobs = [job(1, company="Acme", title="Software Engineer (NYC)", fit=90),
            job(2, company="Acme", title="Software Engineer - Remote", fit=88),
            job(3, company="Acme", title="Backend Engineer", fit=87)] + [job(i, fit=70) for i in range(4, 16)]
    picked = ids(shortlist.build(jobs, now=NOW))
    assert "j1" in picked and "j2" not in picked and "j3" not in picked


def test_a_second_job_per_company_only_fills_a_short_list():
    jobs = [job(1, company="Acme", fit=90), job(2, company="Acme", title="Backend Engineer", fit=85)]
    assert ids(shortlist.build(jobs, now=NOW)) == ["j1", "j2"]


def test_no_job_family_takes_over_the_list():
    jobs = [job(i, fit=95, title="AI Engineer") for i in range(11)] + \
           [job(20 + i, fit=70, title="Backend Engineer") for i in range(11)]
    families = [e["family"] for e in shortlist.build(jobs, now=NOW)]
    assert len(families) == 11 and families.count("backend") == 3      # the rest filled with the best


def test_jobs_shown_before_and_ignored_sink(tmp_path):
    cfg = Config(resume=tmp_path / "r.tex", companies=tmp_path / "c.yaml", data_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    for j in (job(1, fit=82), job(2, fit=80)):
        db.upsert_seen(conn, {"id": j["id"], "source": "lever", "company": j["company"], "title": j["title"],
                              "location": "", "country": "", "department": "", "url": "u", "apply_url": "u",
                              "description": "x", "posted_at": j["posted_at"]})
        db.update(conn, j["id"], stage="graded", fit_score=j["fit_score"], match_score=0.6)
    conn.commit()
    first = shortlist.refresh(conn, cfg, day="2026-11-13", now=NOW)
    assert ids(first) == ["j1", "j2"]
    conn.execute("DELETE FROM shortlist WHERE job_id = 'j2'")       # only j1 was shown before
    assert ids(shortlist.refresh(conn, cfg, day="2026-11-14", now=NOW)) == ["j2", "j1"]
    assert shortlist.days(conn) == ["2026-11-14", "2026-11-13"]
    assert [r["job_id"] for r in shortlist.stored(conn, "2026-11-13")] == ["j1"]    # history is kept
