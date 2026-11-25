"""Company facts carry sources; unsourced company claims are dropped; proposals stay proposals."""
from copilot import db, intel, llm
from copilot.config import Config

POSTING = """About Acme
Acme builds routing software for ambulances in 40 cities.

Requirements
- Python, Kafka and Docker
"""
JOB = {"id": "j1", "company": "Acme", "title": "Backend Engineer", "url": "https://jobs.example/acme/1",
       "description": POSTING, "sponsorship_basis": "posting says sponsors"}
SETTINGS = {"model": "m", "type": "openai"}


def wiki_get(url):
    if "list=search" in url:
        return {"query": {"search": [{"title": "Acme (band)"}, {"title": "Acme Corporation"}]}}
    return {"type": "standard", "extract": "Acme Corporation is a software company founded in 2015.",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Acme_Corporation"}}}


def conn_for(tmp_path):
    return db.connect(Config(resume=tmp_path / "r", companies=tmp_path / "c", data_dir=tmp_path).db_path)


def test_facts_come_with_links_and_wikipedia_is_fetched_once(tmp_path):
    conn, calls = conn_for(tmp_path), []

    def get(url):
        calls.append(url)
        return wiki_get(url)
    facts = intel.company_facts(conn, JOB, get=get)
    assert [f["source"] for f in facts] == ["The posting (the company's own words)", "Wikipedia", "The posting"]
    assert "ambulances" in facts[0]["text"] and facts[1]["url"].endswith("Acme_Corporation")
    assert all(f["url"] for f in facts)
    n = len(calls)
    intel.company_facts(conn, JOB, get=get)
    assert len(calls) == n                                    # cached per company


def test_a_wrong_or_unreachable_wikipedia_page_is_ignored():
    def band(url):
        return {"query": {"search": [{"title": "Acme (band)"}]}} if "list=search" in url else \
            {"type": "standard", "extract": "Acme are a rock band from Leeds."}

    def offline(url):
        raise OSError("no network")
    assert intel.wikipedia("Acme", band) is None and intel.wikipedia("Acme", offline) is None


def test_company_points_without_a_real_source_are_dropped_and_the_result_is_cached(tmp_path, monkeypatch):
    conn, calls = conn_for(tmp_path), []
    idea = intel.Idea(title="t", what="w", shows="s")

    def parse(settings, system, user, schema, max_tokens=0):
        calls.append(user)
        return intel.Strategy(why_fit=["Built Kafka pipelines"], why_company=[
            intel.CompanyPoint(point="Routes ambulances", source=1),
            intel.CompanyPoint(point="Raised a billion dollars", source=9)],
            quick_win=idea, short_project=idea, advanced_project=idea), None
    monkeypatch.setattr(llm, "parse", parse)
    out = intel.strategy(None, conn, JOB, "resume", "k", SETTINGS, online=False)
    assert [p["point"] for p in out["why_company"]] == ["Routes ambulances"]
    assert "[1] (The posting" in calls[0] and len(out["ideas"]) == 3 and not out["cached"]
    again = intel.strategy(None, conn, JOB, "resume", "k", SETTINGS, online=False)
    assert again["cached"] and len(calls) == 1
    assert intel.cached_strategy(conn, JOB, "k", SETTINGS)["why_fit"] == ["Built Kafka pipelines"]
    assert intel.cached_strategy(conn, JOB, "other-resume", SETTINGS) is None


def test_best_existing_project_is_the_one_naming_most_requirements():
    master = {"projects": [{"name": "Traffic RL", "bullets": ["PyTorch agents in simulation"]},
                           {"name": "Event pipeline", "bullets": ["Kafka consumers in Python, shipped with Docker"]}],
              "experience": [{"title": "Engineer", "company": "X", "bullets": ["Wrote Python"]}]}
    best = intel.best_existing(master, JOB)
    assert best["name"] == "Event pipeline" and set(best["keywords"]) == {"python", "kafka", "docker"}
    assert intel.best_existing({"projects": [], "experience": []}, JOB) is None
