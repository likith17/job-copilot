from jobmatcher.models.job import Job, RankedJob
from jobmatcher.filtering.filters import FilterCriteria, filter_jobs, JobFilter


def make_ranked(title, company, location, score, description="Python", source="greenhouse"):
    job = Job(id="1", title=title, company=company, location=location, description=description, description_plain=description, url="https://example.com", source=source)
    return RankedJob(job=job, score=score, explanation="test")


def test_filter_min_score():
    jobs = [make_ranked("SE", "Acme", "Remote", 80), make_ranked("SE", "Acme", "Remote", 30)]
    criteria = FilterCriteria(min_score=50)
    filtered = filter_jobs(jobs, criteria)
    assert len(filtered) == 1
    assert filtered[0].score == 80


def test_filter_company():
    jobs = [
        make_ranked("SE", "Acme", "Remote", 80),
        make_ranked("SE", "OtherCo", "Remote", 80),
    ]
    criteria = FilterCriteria(companies=["Acme"])
    filtered = filter_jobs(jobs, criteria)
    assert len(filtered) == 1
    assert filtered[0].job.company == "Acme"


def test_filter_location():
    jobs = [
        make_ranked("SE", "Acme", "San Francisco", 80),
        make_ranked("SE", "Acme", "Remote", 80),
    ]
    criteria = FilterCriteria(locations=["Remote"])
    filtered = filter_jobs(jobs, criteria)
    assert len(filtered) == 1
    assert "Remote" in filtered[0].job.location


def test_filter_remote_only():
    jobs = [
        make_ranked("SE", "Acme", "San Francisco", 80, description="Onsite in SF"),
        make_ranked("SE", "Acme", "Remote US", 80, description="Remote role"),
    ]
    criteria = FilterCriteria(remote_only=True)
    filtered = filter_jobs(jobs, criteria)
    assert len(filtered) == 1
    assert "Remote" in filtered[0].job.location


def test_filter_keywords():
    jobs = [
        make_ranked("SE", "Acme", "Remote", 80, description="Python and Django"),
        make_ranked("SE", "Acme", "Remote", 80, description="Java and Spring"),
    ]
    criteria = FilterCriteria(keywords=["Python"])
    filtered = filter_jobs(jobs, criteria)
    assert len(filtered) == 1


def test_filter_exclude_keywords():
    jobs = [
        make_ranked("SE", "Acme", "Remote", 80, description="Python"),
        make_ranked("SE", "Acme", "Remote", 80, description="Python Senior Staff Manager director"),
    ]
    criteria = FilterCriteria(exclude_keywords=["Manager", "director"])
    filtered = filter_jobs(jobs, criteria)
    assert len(filtered) == 1


def test_filter_title_contains():
    jobs = [
        make_ranked("Software Engineer", "Acme", "Remote", 80),
        make_ranked("Marketing Manager", "Acme", "Remote", 80),
    ]
    criteria = FilterCriteria(title_contains=["Engineer"])
    filtered = filter_jobs(jobs, criteria)
    assert len(filtered) == 1
    assert "Engineer" in filtered[0].job.title


def test_filter_source():
    jobs = [
        make_ranked("SE", "Acme", "Remote", 80, source="greenhouse"),
        make_ranked("SE", "Acme", "Remote", 80, source="lever"),
    ]
    criteria = FilterCriteria(sources=["lever"])
    filtered = filter_jobs(jobs, criteria)
    assert len(filtered) == 1
    assert filtered[0].job.source == "lever"
