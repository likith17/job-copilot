from __future__ import annotations

from typing import List
import logging

from jobmatcher.crawlers.base import BaseCrawler, CrawlerError
from jobmatcher.models.company import CompanyConfig
from jobmatcher.models.job import Job

logger = logging.getLogger(__name__)


class LeverCrawler(BaseCrawler):
    """Crawler for Lever ATS.

    Public API: https://api.lever.co/v0/postings/{company}?mode=json
    Also supports: ?group=team etc but we use basic.
    """

    ats_name = "lever"
    BASE_URL = "https://api.lever.co/v0/postings/{company}"

    def fetch_jobs(self, company: CompanyConfig) -> List[Job]:
        if not company.identifier:
            raise CrawlerError("Lever requires identifier/company token")

        url = self.BASE_URL.format(company=company.identifier)
        params = {"mode": "json"}

        logger.info(f"Fetching Lever jobs for {company.name} ({company.identifier})")

        try:
            resp = self._get(url, params=params)
            data = resp.json()
        except Exception as e:
            raise CrawlerError(f"Lever fetch failed for {company.name}: {e}") from e

        if not isinstance(data, list):
            # Lever might return error object
            if isinstance(data, dict) and "message" in data:
                raise CrawlerError(f"Lever API error for {company.name}: {data}")
            logger.warning(f"Unexpected Lever response for {company.name}: {type(data)}")
            return []

        jobs: List[Job] = []
        for raw in data:
            try:
                job = self._parse_job(raw, company)
                jobs.append(job)
            except Exception as e:
                logger.warning(f"Failed to parse Lever job {raw.get('id')}: {e}")
                continue

        logger.info(f"Lever: {company.name} -> {len(jobs)} jobs")
        return jobs

    def _parse_job(self, raw: dict, company: CompanyConfig) -> Job:
        job_id = str(raw.get("id", ""))
        title = raw.get("text", "") or raw.get("title", "")

        categories = raw.get("categories", {})
        location = categories.get("location", "") if isinstance(categories, dict) else ""
        team = categories.get("team", "") if isinstance(categories, dict) else ""
        commitment = categories.get("commitment", "") or categories.get("department", "") if isinstance(categories, dict) else ""

        description = raw.get("description", "") or ""
        description_plain = raw.get("descriptionPlain", "") or self.clean_html(description)

        # Lever keeps responsibilities and requirements in `lists` and closing
        # text in `additional`; descriptionPlain is only the intro.
        sections = [description_plain]
        for item in raw.get("lists") or []:
            if isinstance(item, dict) and item.get("content"):
                sections.append(f"{item.get('text', '')}\n{self.clean_html(item['content'])}")
        additional = raw.get("additionalPlain") or self.clean_html(raw.get("additional", "") or "")
        if additional:
            sections.append(additional)
        description_plain = "\n\n".join(s.strip() for s in sections if s and s.strip())

        posted_at = None
        if raw.get("createdAt"):
            from datetime import datetime, timezone
            posted_at = datetime.fromtimestamp(raw["createdAt"] / 1000, tz=timezone.utc).isoformat()

        hosted_url = raw.get("hostedUrl", "") or ""
        apply_url = raw.get("applyUrl", "") or hosted_url

        return Job(
            id=job_id,
            title=title,
            company=company.name,
            location=location,
            team=team,
            department=commitment,
            employment_type=commitment,
            description=description,
            description_plain=description_plain,
            url=hosted_url,
            apply_url=apply_url,
            source="lever",
            posted_at=posted_at,
            raw_data=raw,
        )
