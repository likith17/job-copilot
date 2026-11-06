"""
Integration test: full pipeline resume -> crawl (mocked) -> rank -> filter -> export
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from jobmatcher.parsers.resume_parser import ResumeParser
from jobmatcher.models.company import CompanyConfig
from jobmatcher.crawlers.registry import CrawlerRegistry
from jobmatcher.ranking.engine import RankingEngine
from jobmatcher.filtering.filters import FilterCriteria, filter_jobs
from jobmatcher.exporter.csv_export import export_to_csv


SAMPLE_RESUME = """
Jane Smith
jane@example.com

Senior Python Developer with 4 years experience
Skills: Python, Django, AWS, Docker, PostgreSQL, React

Experience building scalable web apps.
"""


@patch("jobmatcher.crawlers.base.requests.Session.get")
def test_full_pipeline(mock_get):
    # Mock greenhouse response
    def mock_get_side_effect(url, params=None, timeout=None):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = Mock()
        if "greenhouse" in url:
            mock_resp.json.return_value = {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Senior Python Engineer",
                        "location": {"name": "Remote"},
                        "content": "<p>We need Python, Django, AWS, Docker</p>",
                        "absolute_url": "https://boards.greenhouse.io/test/jobs/1",
                        "departments": [{"name": "Engineering"}],
                    },
                    {
                        "id": 2,
                        "title": "Marketing Manager",
                        "location": {"name": "New York"},
                        "content": "<p>Need Marketo</p>",
                        "absolute_url": "https://boards.greenhouse.io/test/jobs/2",
                        "departments": [{"name": "Marketing"}],
                    },
                ]
            }
        elif "lever" in url:
            mock_resp.json.return_value = [
                {
                    "id": "a",
                    "text": "Backend Engineer",
                    "categories": {"location": "Remote", "team": "Eng"},
                    "description": "Python, PostgreSQL",
                    "descriptionPlain": "Python, PostgreSQL",
                    "hostedUrl": "https://jobs.lever.co/test/a",
                }
            ]
        else:
            mock_resp.json.return_value = {"jobs": []}
        return mock_resp

    mock_get.side_effect = mock_get_side_effect

    # Parse resume
    parser = ResumeParser()
    resume = parser.parse_text(SAMPLE_RESUME)
    assert "Python" in resume.skills

    # Load companies
    companies = [
        CompanyConfig(name="Test Greenhouse", ats="greenhouse", identifier="test"),
        CompanyConfig(name="Test Lever", ats="lever", identifier="test"),
    ]

    # Crawl
    all_jobs = []
    for comp in companies:
        crawler = CrawlerRegistry.create(comp.ats, delay=0)
        jobs = crawler.fetch_jobs(comp)
        all_jobs.extend(jobs)

    assert len(all_jobs) == 3

    # Rank
    engine = RankingEngine()
    ranked = engine.rank_all(resume, all_jobs)
    assert len(ranked) == 3
    # Python job should be highest
    assert "Python" in ranked[0].job.title or ranked[0].score > ranked[-1].score

    # Filter
    criteria = FilterCriteria(min_score=40, keywords=["Python"])
    filtered = filter_jobs(ranked, criteria)
    assert len(filtered) >= 1
    assert all(r.score >= 40 for r in filtered)

    # Export
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        csv_path = f.name

    try:
        export_to_csv(filtered, csv_path)
        assert Path(csv_path).exists()
        content = Path(csv_path).read_text()
        assert "Python" in content or "Engineer" in content
    finally:
        Path(csv_path).unlink(missing_ok=True)
