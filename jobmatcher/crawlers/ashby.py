from __future__ import annotations

from typing import List
import logging

from jobmatcher.crawlers.base import BaseCrawler, CrawlerError
from jobmatcher.models.company import CompanyConfig
from jobmatcher.models.job import Job

logger = logging.getLogger(__name__)


class AshbyCrawler(BaseCrawler):
    """Crawler for Ashby ATS.

    Public posting API: https://api.ashbyhq.com/posting-api/job-board/{boardName}
    This endpoint is used by Ashby job boards (jobs.ashbyhq.com/{company}).

    It returns JSON like: {"jobs": [{...}, ...]}

    Reference observed in network traffic of Ashby boards.
    """

    ats_name = "ashby"
    BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"

    def fetch_jobs(self, company: CompanyConfig) -> List[Job]:
        if not company.identifier:
            raise CrawlerError("Ashby requires identifier/board name")

        url = self.BASE_URL.format(board=company.identifier)

        logger.info(f"Fetching Ashby jobs for {company.name} ({company.identifier})")

        try:
            resp = self._get(url)
            data = resp.json()
        except Exception as e:
            raise CrawlerError(f"Ashby fetch failed for {company.name}: {e}") from e

        jobs_raw = data.get("jobs") if isinstance(data, dict) else []
        if not isinstance(jobs_raw, list):
            logger.warning(f"Ashby unexpected response for {company.name}: {data}")
            return []

        jobs: List[Job] = []
        for raw in jobs_raw:
            try:
                job = self._parse_job(raw, company)
                jobs.append(job)
            except Exception as e:
                logger.warning(f"Failed to parse Ashby job {raw.get('id')}: {e}")
                continue

        logger.info(f"Ashby: {company.name} -> {len(jobs)} jobs")
        return jobs

    def _parse_job(self, raw: dict, company: CompanyConfig) -> Job:
        job_id = str(raw.get("id", ""))
        title = raw.get("title", "")

        location = raw.get("location", "") or ""
        # Ashby location can be object or string
        if isinstance(location, dict):
            location = location.get("name") or location.get("locationStr") or str(location)

        department = raw.get("department", "") or raw.get("team", "") or ""
        employment_type = raw.get("employmentType", "") or ""

        # Ashby provides descriptionHtml and descriptionPlain
        description_html = raw.get("descriptionHtml", "") or raw.get("description", "") or ""
        description_plain = raw.get("descriptionPlain", "") or self.clean_html(description_html)

        # URL construction
        job_url = raw.get("jobUrl", "") or raw.get("canonicalJobUrl", "") or ""
        if not job_url and raw.get("id"):
            # fallback: https://jobs.ashbyhq.com/{board}/{id}
            job_url = f"https://jobs.ashbyhq.com/{company.identifier}/{raw.get('id')}"

        # posted date
        posted = raw.get("publishedAt") or raw.get("createdAt") or ""

        return Job(
            id=job_id,
            title=title,
            company=company.name,
            location=location,
            department=department,
            employment_type=employment_type,
            description=description_html,
            description_plain=description_plain,
            url=job_url,
            apply_url=raw.get("applyUrl") or job_url,
            source="ashby",
            posted_at=str(posted) if posted else None,
            raw_data=raw,
        )
