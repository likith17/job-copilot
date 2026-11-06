import pytest
from jobmatcher.models.job import Job, RankedJob
from jobmatcher.crawlers.deduplication import (
    normalize_url,
    normalize_text,
    deduplicate_jobs,
    deduplicate_ranked_jobs,
    is_location_similar,
)


def make_job(title, company, location="Remote", url="", source="greenhouse", job_id="1"):
    return Job(
        id=job_id,
        title=title,
        company=company,
        location=location,
        url=url or f"https://{company.lower()}.com/jobs/{title.lower().replace(' ', '-')}",
        source=source,
        description="Python AWS",
        description_plain="Python AWS",
    )


def test_normalize_url():
    assert normalize_url("https://EXAMPLE.com/Jobs/123/") == "https://example.com/jobs/123"
    assert normalize_url("https://example.com/jobs/123?utm_source=linkedin&utm_medium=test") == "https://example.com/jobs/123"
    assert normalize_url("https://example.com/jobs/123#section") == "https://example.com/jobs/123"
    assert normalize_url("") == ""


def test_normalize_text():
    assert normalize_text("  Software Engineer ") == "software engineer"
    assert normalize_text("Senior   Software  Engineer") == "senior software engineer"
    assert normalize_text("") == ""


def test_is_location_similar():
    assert is_location_similar("Remote", "Remote - US")
    assert is_location_similar("Remote", "remote us")
    assert is_location_similar("San Francisco, CA", "San Francisco")
    assert not is_location_similar("Berlin", "London")
    assert not is_location_similar("", "Remote")


def test_dedup_same_url():
    jobs = [
        make_job("Software Engineer", "Acme", url="https://acme.com/jobs/1", job_id="1"),
        make_job("Software Engineer", "Acme", url="https://acme.com/jobs/1", job_id="2"),  # same URL diff ID
        make_job("Software Engineer", "Acme", url="https://acme.com/jobs/2", job_id="3"),
    ]
    deduped, removed = deduplicate_jobs(jobs, strategy="url")
    assert len(deduped) == 2
    assert removed == 1
    # First occurrence kept
    assert deduped[0].id == "1"


def test_dedup_strict_same_company_title_location():
    jobs = [
        make_job("Software Engineer", "Acme", location="Remote", url="https://acme.com/1", job_id="1"),
        make_job("Software Engineer", "Acme", location="Remote", url="https://acme.com/2", job_id="2"),
        make_job("Software Engineer", "Acme", location="San Francisco", url="https://acme.com/3", job_id="3"),
    ]
    deduped, removed = deduplicate_jobs(jobs, strategy="strict")
    # First two have same company+title+location -> one removed
    # Third has different location -> kept
    assert len(deduped) == 2
    assert removed == 1


def test_dedup_smart_cross_ats():
    # Same job appears on greenhouse and lever with different URLs but same company+title
    jobs = [
        make_job("Senior Python Engineer", "Acme", location="Remote", url="https://boards.greenhouse.io/acme/jobs/1", source="greenhouse", job_id="1"),
        make_job("Senior Python Engineer", "Acme", location="Remote - US", url="https://jobs.lever.co/acme/123", source="lever", job_id="2"),
        make_job("Senior Python Engineer", "OtherCo", location="Remote", url="https://boards.greenhouse.io/otherco/jobs/1", source="greenhouse", job_id="3"),
    ]
    deduped, removed = deduplicate_jobs(jobs, strategy="smart")
    # First two are cross-ATS duplicates -> one removed
    # Third is different company -> kept
    assert len(deduped) == 2
    assert removed == 1
    companies = [j.company for j in deduped]
    assert "OtherCo" in companies


def test_dedup_smart_same_source_different_location_kept():
    # Same company, same title, but different locations with same source -> should be kept (different office)
    # Our smart strategy keeps them unless location similar and source different?
    # But we implement conservative: same source, different location not considered duplicate if not exact match
    jobs = [
        make_job("Software Engineer", "Acme", location="Berlin", url="https://acme.com/berlin", source="greenhouse", job_id="1"),
        make_job("Software Engineer", "Acme", location="London", url="https://acme.com/london", source="greenhouse", job_id="2"),
    ]
    deduped, removed = deduplicate_jobs(jobs, strategy="smart")
    # These have same company+title but different location, same source -> should be kept (not duplicate)
    # Our current logic: strict would keep because ctl differs, smart would see ct_key exists but same source and location not similar -> keep
    assert len(deduped) == 2
    assert removed == 0


def test_dedup_aggressive():
    jobs = [
        make_job("Software Engineer", "Acme", location="Berlin", job_id="1"),
        make_job("Software Engineer", "Acme", location="London", job_id="2"),
        make_job("Software Engineer", "Acme", location="Remote", job_id="3"),
    ]
    deduped, removed = deduplicate_jobs(jobs, strategy="aggressive")
    assert len(deduped) == 1
    assert removed == 2


def test_dedup_empty():
    deduped, removed = deduplicate_jobs([], strategy="smart")
    assert deduped == []
    assert removed == 0


def test_dedup_url_case_insensitive():
    jobs = [
        make_job("Engineer", "Acme", url="https://Acme.com/JOBS/1", job_id="1"),
        make_job("Engineer", "Acme", url="https://acme.com/jobs/1", job_id="2"),
    ]
    deduped, removed = deduplicate_jobs(jobs, strategy="url")
    assert len(deduped) == 1
    assert removed == 1


def test_dedup_ranked_jobs():
    job1 = make_job("Software Engineer", "Acme", location="Remote", url="https://acme.com/1", job_id="1")
    job2 = make_job("Software Engineer", "Acme", location="Remote", url="https://acme.com/1", job_id="2")
    job3 = make_job("Engineer", "Other", location="Remote", url="https://other.com/1", job_id="3")

    ranked = [
        RankedJob(job=job1, score=90, explanation="high"),
        RankedJob(job=job2, score=80, explanation="high but dup"),
        RankedJob(job=job3, score=70, explanation="medium"),
    ]

    deduped, removed = deduplicate_ranked_jobs(ranked, strategy="smart")
    assert len(deduped) == 2
    assert removed == 1
    # Highest score kept
    assert deduped[0].score == 90
    assert deduped[0].job.company == "Acme"


def test_dedup_ranked_jobs_cross_ats():
    job1 = make_job("Python Engineer", "Acme", location="Remote", url="https://gh.com/1", source="greenhouse", job_id="1")
    job2 = make_job("Python Engineer", "Acme", location="Remote", url="https://lever.com/1", source="lever", job_id="2")
    ranked = [
        RankedJob(job=job1, score=85, explanation=""),
        RankedJob(job=job2, score=80, explanation=""),
    ]
    deduped, removed = deduplicate_ranked_jobs(ranked, strategy="smart")
    assert len(deduped) == 1
    assert removed == 1
