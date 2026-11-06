from jobmatcher.models.job import Job, RankedJob
from jobmatcher.models.company import CompanyConfig


def test_job_creation():
    job = Job(id="1", title="Software Engineer", company="Acme", location="Remote", url="https://example.com")
    assert job.title == "Software Engineer"
    assert job.company == "Acme"
    assert "Software Engineer" in job.searchable_text


def test_job_auto_id():
    job = Job(id="", title="Test", company="Acme", url="https://example.com")
    assert job.id != ""


def test_ranked_job_to_dict():
    job = Job(id="1", title="SE", company="Acme", url="https://example.com")
    ranked = RankedJob(job=job, score=85.5, explanation="Great", matched_skills=["Python"], missing_skills=["Go"])
    d = ranked.to_dict()
    assert d["match_score"] == 85.5
    assert "Acme" in d["company"]


def test_company_config():
    cfg = CompanyConfig(name="TestCo", ats="greenhouse", identifier="testco")
    assert cfg.ats == "greenhouse"
    assert cfg.name == "TestCo"


def test_company_config_invalid_ats():
    try:
        CompanyConfig(name="X", ats="invalid", identifier="x")
        assert False, "Should have raised"
    except ValueError:
        pass


def test_company_from_dict():
    data = {"name": "Foo", "ats": "Lever", "identifier": "foo"}
    cfg = CompanyConfig.from_dict(data)
    assert cfg.ats == "lever"
    assert cfg.identifier == "foo"
