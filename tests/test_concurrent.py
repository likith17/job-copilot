import time
from unittest.mock import Mock, patch
import threading

from jobmatcher.models.company import CompanyConfig
from jobmatcher.models.job import Job
from jobmatcher.crawlers.registry import fetch_all_jobs, fetch_all_jobs_concurrent, fetch_all_jobs_sequential
from jobmatcher.crawlers.base import BaseCrawler


def make_company(name, ats, identifier):
    return CompanyConfig(name=name, ats=ats, identifier=identifier)


@patch("jobmatcher.crawlers.registry.get_crawler")
def test_concurrent_fetch_all(mock_get_crawler):
    """Test that concurrent fetching returns jobs from all companies and is faster than sequential."""

    # Create a mock crawler that sleeps a bit to simulate network IO
    def create_mock_crawler(*args, **kwargs):
        mock_crawler = Mock(spec=BaseCrawler)
        # Simulate 0.1s network delay per company
        def fetch_side_effect(company):
            time.sleep(0.1)
            return [
                Job(id=f"{company.identifier}-1", title=f"Engineer at {company.name}", company=company.name, url=f"https://example.com/{company.identifier}")
            ]
        mock_crawler.fetch_jobs.side_effect = fetch_side_effect
        return mock_crawler

    mock_get_crawler.side_effect = create_mock_crawler

    companies = [
        make_company(f"Company{i}", "greenhouse", f"company{i}")
        for i in range(5)
    ]

    # Sequential should take ~0.5s (5 * 0.1)
    start_seq = time.time()
    jobs_seq = fetch_all_jobs(companies, concurrent=False, deduplicate=False, timeout=5)
    elapsed_seq = time.time() - start_seq

    # Concurrent with 5 workers should take ~0.1-0.2s
    start_conc = time.time()
    jobs_conc = fetch_all_jobs(companies, concurrent=True, max_workers=5, deduplicate=False, timeout=5)
    elapsed_conc = time.time() - start_conc

    assert len(jobs_seq) == 5
    assert len(jobs_conc) == 5

    # Concurrent should be faster (allow some overhead)
    # Sequential 0.5s vs concurrent ~0.1s, so concurrent < sequential
    assert elapsed_conc < elapsed_seq, f"Concurrent {elapsed_conc:.2f}s should be faster than sequential {elapsed_seq:.2f}s"


@patch("jobmatcher.crawlers.registry.get_crawler")
def test_concurrent_with_deduplication(mock_get_crawler):
    """Test concurrent fetching with deduplication enabled."""

    def create_mock_crawler(*args, **kwargs):
        mock_crawler = Mock(spec=BaseCrawler)
        def fetch_side_effect(company):
            # Two companies return same job (duplicate across ATS)
            if company.name in ["Acme Greenhouse", "Acme Lever"]:
                return [
                    Job(id="1", title="Software Engineer", company="Acme", location="Remote", url="https://acme.com/jobs/1", source=company.ats)
                ]
            else:
                return [
                    Job(id=f"{company.identifier}-1", title=f"Engineer at {company.name}", company=company.name, url=f"https://example.com/{company.identifier}", source=company.ats)
                ]
        mock_crawler.fetch_jobs.side_effect = fetch_side_effect
        return mock_crawler

    mock_get_crawler.side_effect = create_mock_crawler

    companies = [
        make_company("Acme Greenhouse", "greenhouse", "acme"),
        make_company("Acme Lever", "lever", "acme"),
        make_company("OtherCo", "ashby", "otherco"),
    ]

    jobs_no_dedup = fetch_all_jobs(companies, concurrent=True, max_workers=3, deduplicate=False)
    assert len(jobs_no_dedup) == 3

    jobs_with_dedup = fetch_all_jobs(companies, concurrent=True, max_workers=3, deduplicate=True, dedup_strategy="smart")
    # Acme duplicate should be removed, so 2 remain
    assert len(jobs_with_dedup) == 2


@patch("jobmatcher.crawlers.registry.get_crawler")
def test_concurrent_error_handling(mock_get_crawler):
    """Test that errors in one company don't fail entire concurrent fetch, and retry behavior intact."""

    def create_mock_crawler(*args, **kwargs):
        # For retries, we test that BaseCrawler's retry logic is preserved
        # Here we simulate a crawler that fails for one company
        mock_crawler = Mock(spec=BaseCrawler)
        def fetch_side_effect(company):
            if company.identifier == "fail":
                raise Exception("Simulated failure")
            return [Job(id="1", title="Engineer", company=company.name, url="https://example.com")]
        mock_crawler.fetch_jobs.side_effect = fetch_side_effect
        return mock_crawler

    mock_get_crawler.side_effect = create_mock_crawler

    companies = [
        make_company("Good1", "greenhouse", "good1"),
        make_company("FailCo", "greenhouse", "fail"),
        make_company("Good2", "greenhouse", "good2"),
    ]

    jobs = fetch_all_jobs(companies, concurrent=True, max_workers=3, deduplicate=False)
    # Should have 2 jobs from good companies, fail is ignored
    assert len(jobs) == 2
    assert all(j.company != "FailCo" for j in jobs)


@patch("jobmatcher.crawlers.base.requests.Session.get")
def test_retry_behavior_intact_concurrent(mock_get):
    """Verify retry logic still works when fetching concurrently - BaseCrawler._get should retry."""

    from jobmatcher.crawlers.greenhouse import GreenhouseCrawler
    import requests

    call_counts = {}

    def mock_get_side_effect(url, params=None, timeout=None):
        cnt = call_counts.get(url, 0)
        call_counts[url] = cnt + 1

        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = Mock()

        if cnt == 0 and "company_retry" in url:
            # Simulate transient failure on first attempt - must be RequestException to trigger retry
            raise requests.RequestException("Transient failure - should retry")

        mock_resp.json.return_value = {
            "jobs": [{"id": 1, "title": "Engineer", "location": {"name": "Remote"}, "content": "Python", "absolute_url": "https://example.com"}]
        }
        return mock_resp

    mock_get.side_effect = mock_get_side_effect

    companies = [
        make_company("RetryCo", "greenhouse", "company_retry"),
        make_company("NormalCo", "greenhouse", "company_normal"),
    ]

    with patch("jobmatcher.crawlers.registry.get_crawler") as mock_get_crawler:
        def create_real_crawler(*args, **kwargs):
            return GreenhouseCrawler(retries=2, delay=0)

        mock_get_crawler.side_effect = create_real_crawler

        jobs = fetch_all_jobs(companies, concurrent=True, max_workers=2, deduplicate=False, retries=2, delay=0)

        # Both should succeed because retry should handle transient failure
        assert len(jobs) == 2
        retry_calls = [k for k in call_counts if "company_retry" in k]
        assert len(retry_calls) == 1
        assert call_counts[retry_calls[0]] == 2, "Retry should have happened for transient failure"


def test_fetch_all_jobs_empty_list():
    jobs = fetch_all_jobs([], concurrent=True)
    assert jobs == []


def test_concurrent_vs_sequential_same_result():
    """Concurrent and sequential should return same results (minus order, but after dedup order may differ, check counts)."""

    with patch("jobmatcher.crawlers.registry.get_crawler") as mock_get_crawler:
        def create_mock_crawler(*args, **kwargs):
            mock_crawler = Mock(spec=BaseCrawler)
            def fetch_side_effect(company):
                return [
                    Job(id=f"{company.identifier}-1", title="Engineer", company=company.name, url=f"https://example.com/{company.identifier}")
                ]
            mock_crawler.fetch_jobs.side_effect = fetch_side_effect
            return mock_crawler

        mock_get_crawler.side_effect = create_mock_crawler

        companies = [make_company(f"C{i}", "greenhouse", f"c{i}") for i in range(3)]

        jobs_seq = fetch_all_jobs_sequential(companies, deduplicate=False)
        jobs_conc = fetch_all_jobs_concurrent(companies, deduplicate=False, max_workers=3)

        assert len(jobs_seq) == len(jobs_conc) == 3
        # Same companies present
        assert set(j.company for j in jobs_seq) == set(j.company for j in jobs_conc)
