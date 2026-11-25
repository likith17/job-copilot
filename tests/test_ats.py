"""The matching report: required vs preferred coverage, gaps, and honest suggestions."""
from copilot import ats

POSTING = """About us
We build routing software. Great benefits and Kubernetes-themed socks.

What you'll do
Ship services.

Requirements
- 5+ years of experience with Python and PostgreSQL
- Experience with Docker and AWS
- Built LLM applications with RAG

Nice to have
- Kafka or Spark
- React

Benefits
Health, dental, Rust club.
"""
RESUME = "Python engineer. Built retrieval-augmented generation apps on Postgres with Docker. Some React."


def test_required_and_preferred_are_read_from_their_sections():
    r = ats.report(RESUME, {"description": POSTING})
    assert set(r["required"]["covered"]) == {"python", "postgresql", "docker", "rag"}
    assert set(r["required"]["missing"]) == {"aws", "llm"}
    assert r["preferred"] == {"covered": ["react"], "missing": ["kafka", "spark"]} or \
        (r["preferred"]["covered"] == ["react"] and set(r["preferred"]["missing"]) == {"kafka", "spark"})
    assert "kubernetes" not in r["missing_keywords"] and "rust" not in r["missing_keywords"]   # not requirements


def test_alignment_is_weighted_coverage_and_says_what_it_is():
    r = ats.report(RESUME, {"description": POSTING})
    assert r["alignment"] == round(100 * (0.75 * 4 / 6 + 0.25 * 1 / 3))
    assert "not a score from a real" in r["note"]
    assert ats.report(RESUME, {"description": "We sell sandwiches."})["alignment"] is None


def test_gaps_years_and_blockers():
    r = ats.report(RESUME, {"description": POSTING + "\nMust hold an active security clearance."}, years=3)
    assert any("5+ years" in g for g in r["gaps"]) and any("clearance" in g for g in r["gaps"])
    assert not ats.report(RESUME, {"description": POSTING}, years=6)["gaps"]


def test_suggestions_never_invent_experience():
    r = ats.report(RESUME, {"description": POSTING}, other_resumes={"Data Engineer resume": "Kafka pipelines on AWS."})
    by = {s["keyword"]: s for s in r["suggestions"]}
    assert by["aws"]["on_file"] and "Data Engineer" in by["aws"]["advice"]
    assert not by["spark"]["on_file"] and "only if" in by["spark"]["advice"]


def test_a_posting_without_sections_counts_everything_but_the_nice_to_haves():
    r = ats.report("Python.", {"description": "You will write Python and Go services. Kubernetes is a plus."})
    assert r["required"]["covered"] == ["python"] and "kubernetes" in r["preferred"]["missing"]


def test_keyword_matching_respects_word_edges():
    assert ats.keywords("JavaScript and Java, restful APIs, k8s, sklearn") == \
        ats.keywords("JavaScript and Java, restful APIs, k8s, sklearn")
    found = set(ats.keywords("JavaScript, k8s, sklearn, PySpark, C++"))
    assert found == {"javascript", "kubernetes", "scikit-learn", "spark", "c++"}


def test_compare_shows_what_tailoring_added():
    c = ats.compare(RESUME, RESUME + " Deployed on AWS.", {"description": POSTING})
    assert c["gained"] == ["aws"] and c["after"]["alignment"] > c["before"]["alignment"]
