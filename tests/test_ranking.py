from jobmatcher.models.job import Job
from jobmatcher.models.resume import ResumeProfile
from jobmatcher.ranking.engine import RankingEngine, RankingWeights


def make_resume():
    return ResumeProfile(
        raw_text="Python AWS Docker Kubernetes Senior Software Engineer",
        name="John",
        skills={"Python", "AWS", "Docker", "Kubernetes", "PostgreSQL"},
        titles=["Software Engineer", "Senior Software Engineer"],
        experience_years=5,
    )


def make_job(title, description, company="Acme"):
    return Job(
        id="1",
        title=title,
        company=company,
        location="Remote",
        description=description,
        description_plain=description,
        url="https://example.com",
    )


def test_ranking_high_match():
    resume = make_resume()
    job = make_job("Senior Software Engineer", "We need Python, AWS, Docker, Kubernetes, PostgreSQL 3 years experience")

    engine = RankingEngine()
    ranked = engine.rank(resume, job)

    assert ranked.score > 60  # should be strong match
    assert "Python" in ranked.matched_skills
    assert len(ranked.explanation) > 0


def test_ranking_low_match():
    resume = make_resume()
    job = make_job("Marketing Manager", "Need Salesforce, Marketo, SEO, content marketing")

    engine = RankingEngine()
    ranked = engine.rank(resume, job)

    assert ranked.score < 50  # weak match


def test_ranking_sort():
    resume = make_resume()
    jobs = [
        make_job("Marketing", "Salesforce marketing"),
        make_job("Python Engineer", "Python AWS Docker"),
        make_job("Senior Software Engineer", "Python AWS Docker Kubernetes"),
    ]

    engine = RankingEngine()
    ranked = engine.rank_all(resume, jobs)

    # Should be sorted descending
    assert ranked[0].score >= ranked[1].score >= ranked[2].score
    assert "Python" in ranked[0].job.title or "Senior" in ranked[0].job.title


def test_weights_normalization():
    w = RankingWeights(skills=5, title=2, experience=1, keywords=2)
    w.normalize()
    total = w.skills + w.title + w.experience + w.keywords
    assert abs(total - 1.0) < 1e-6


def test_explanation_contains_score():
    resume = make_resume()
    job = make_job("Software Engineer", "Python")
    engine = RankingEngine()
    ranked = engine.rank(resume, job)
    assert str(int(ranked.score)) in ranked.explanation or f"{ranked.score:.0f}" in ranked.explanation
