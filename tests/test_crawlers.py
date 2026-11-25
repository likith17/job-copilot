import json
from unittest.mock import Mock, patch

from jobmatcher.models.company import CompanyConfig
from jobmatcher.crawlers.greenhouse import GreenhouseCrawler
from jobmatcher.crawlers.lever import LeverCrawler
from jobmatcher.crawlers.ashby import AshbyCrawler
from jobmatcher.crawlers.registry import CrawlerRegistry


def test_registry_lists():
    ats_list = CrawlerRegistry.list_ats()
    assert "greenhouse" in ats_list
    assert "lever" in ats_list
    assert "ashby" in ats_list


def test_greenhouse_parsing():
    crawler = GreenhouseCrawler()

    raw = {
        "id": 123,
        "title": "Software Engineer",
        "location": {"name": "San Francisco"},
        "content": "<p>Python and AWS</p>",
        "absolute_url": "https://boards.greenhouse.io/example/jobs/123",
        "departments": [{"name": "Engineering"}],
    }
    company = CompanyConfig(name="Example", ats="greenhouse", identifier="example")
    job = crawler._parse_job(raw, company)

    assert job.title == "Software Engineer"
    assert job.company == "Example"
    assert job.location == "San Francisco"
    assert "Python" in job.description_plain
    assert job.source == "greenhouse"


def test_lever_parsing():
    crawler = LeverCrawler()
    raw = {
        "id": "abc-123",
        "text": "Backend Engineer",
        "categories": {"location": "Remote", "team": "Engineering", "commitment": "Full-time"},
        "description": "<div>We need Python</div>",
        "descriptionPlain": "We need Python",
        "hostedUrl": "https://jobs.lever.co/example/abc-123",
        "applyUrl": "https://jobs.lever.co/example/abc-123/apply",
    }
    company = CompanyConfig(name="Example", ats="lever", identifier="example")
    job = crawler._parse_job(raw, company)
    assert job.title == "Backend Engineer"
    assert job.location == "Remote"
    assert job.source == "lever"


def test_ashby_parsing():
    crawler = AshbyCrawler()
    raw = {
        "id": "job-id-123",
        "title": "Senior Python Engineer",
        "location": "Remote US",
        "department": "Engineering",
        "employmentType": "Full-time",
        "descriptionHtml": "<p>Python, Docker</p>",
        "descriptionPlain": "Python, Docker",
        "jobUrl": "https://jobs.ashbyhq.com/example/job-id-123",
    }
    company = CompanyConfig(name="Example", ats="ashby", identifier="example")
    job = crawler._parse_job(raw, company)
    assert job.title == "Senior Python Engineer"
    assert job.company == "Example"
    assert job.source == "ashby"


@patch("jobmatcher.crawlers.base.requests.Session.get")
def test_greenhouse_fetch_mock(mock_get):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "jobs": [
            {
                "id": 1,
                "title": "Engineer",
                "location": {"name": "Remote"},
                "content": "Python",
                "absolute_url": "https://example.com",
            }
        ]
    }
    mock_response.raise_for_status = Mock()
    mock_get.return_value = mock_response

    crawler = GreenhouseCrawler(delay=0)
    company = CompanyConfig(name="Test", ats="greenhouse", identifier="test")
    jobs = crawler.fetch_jobs(company)
    assert len(jobs) == 1
    assert jobs[0].title == "Engineer"


@patch("jobmatcher.crawlers.base.requests.Session.get")
def test_lever_fetch_mock(mock_get):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {
            "id": "1",
            "text": "Engineer",
            "categories": {"location": "Remote"},
            "description": "Python",
            "hostedUrl": "https://jobs.lever.co/test/1",
        }
    ]
    mock_response.raise_for_status = Mock()
    mock_get.return_value = mock_response

    crawler = LeverCrawler(delay=0)
    company = CompanyConfig(name="Test", ats="lever", identifier="test")
    jobs = crawler.fetch_jobs(company)
    assert len(jobs) == 1


@patch("jobmatcher.crawlers.base.requests.Session.get")
def test_ashby_fetch_mock(mock_get):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "jobs": [
            {
                "id": "1",
                "title": "Engineer",
                "location": "Remote",
                "descriptionPlain": "Python",
                "jobUrl": "https://jobs.ashbyhq.com/test/1",
            }
        ]
    }
    mock_response.raise_for_status = Mock()
    mock_get.return_value = mock_response

    crawler = AshbyCrawler(delay=0)
    company = CompanyConfig(name="Test", ats="ashby", identifier="test")
    jobs = crawler.fetch_jobs(company)
    assert len(jobs) == 1
