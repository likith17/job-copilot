from __future__ import annotations

from typing import List
import html
import logging

from jobmatcher.crawlers.base import BaseCrawler, CrawlerError
from jobmatcher.models.company import CompanyConfig
from jobmatcher.models.job import Job

logger = logging.getLogger(__name__)


class GreenhouseCrawler(BaseCrawler):
    """Crawler for Greenhouse ATS.

    Public API: https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    Docs: https://developers.greenhouse.io/board#list-jobs
    """

    ats_name = "greenhouse"
    BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"

    def fetch_jobs(self, company: CompanyConfig) -> List[Job]:
        if not company.identifier:
            raise CrawlerError("Greenhouse requires identifier/board token")

        url = self.BASE_URL.format(board=company.identifier)
        params = {"content": "true"}

        logger.info(f"Fetching Greenhouse jobs for {company.name} ({company.identifier})")

        try:
            resp = self._get(url, params=params)
            data = resp.json()
        except Exception as e:
            raise CrawlerError(f"Greenhouse fetch failed for {company.name}: {e}") from e

        # Greenhouse returns {"jobs": [...]} or list? According to docs, it's {"jobs": [...]}
        jobs_raw = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(jobs_raw, list):
            # Sometimes API returns directly list
            jobs_raw = data if isinstance(data, list) else []

        jobs: List[Job] = []
        for raw in jobs_raw:
            try:
                job = self._parse_job(raw, company)
                jobs.append(job)
            except Exception as e:
                logger.warning(f"Failed to parse Greenhouse job {raw.get('id')}: {e}")
                continue

        logger.info(f"Greenhouse: {company.name} -> {len(jobs)} jobs")
        return jobs

    def _parse_job(self, raw: dict, company: CompanyConfig) -> Job:
        job_id = str(raw.get("id", ""))
        title = raw.get("title", "").strip()
        absolute_url = raw.get("absolute_url", "")
        location = ""
        if isinstance(raw.get("location"), dict):
            location = raw["location"].get("name", "")
        else:
            location = str(raw.get("location", ""))

        # The boards API returns content HTML-escaped (&lt;p&gt;...), so unescape
        # before stripping tags or the plain text keeps literal markup.
        content = html.unescape(raw.get("content", "") or "")
        description_plain = self.clean_html(content)

        # Department / team might be in metadata
        departments = ""
        teams = ""
        metadata = raw.get("departments") or raw.get("metadata") or []
        if isinstance(metadata, list) and metadata:
            # metadata is list of dicts like {"id":..., "name":..., "value":...}
            # Actually greenhouse: "departments": [{"id":..., "name":...}]
            if isinstance(raw.get("departments"), list):
                departments = ", ".join([d.get("name", "") for d in raw["departments"] if d.get("name")])
            # Check metadata for team?
        # For simplicity, also parse from 'offices'
        if isinstance(raw.get("offices"), list) and raw["offices"]:
            # append office to location already captured
            pass

        return Job(
            id=job_id,
            title=title,
            company=company.name,
            location=location,
            department=departments,
            description=content,
            description_plain=description_plain,
            url=absolute_url,
            # The embed URL opens the application form directly, even when
            # absolute_url points at a company-hosted careers page.
            apply_url=(
                f"https://job-boards.greenhouse.io/embed/job_app?for={company.identifier}&token={job_id}"
                if job_id else absolute_url
            ),
            source="greenhouse",
            posted_at=raw.get("first_published") or raw.get("updated_at"),
            raw_data=raw,
        )
