from __future__ import annotations

from typing import List
import logging
import re

from jobmatcher.crawlers.base import BaseCrawler, CrawlerError
from jobmatcher.models.company import CompanyConfig
from jobmatcher.models.job import Job

logger = logging.getLogger(__name__)


class GenericCrawler(BaseCrawler):
    """
    Generic HTML crawler fallback for custom career pages.
    Attempts to extract job listings from a company's career page URL.

    This is intentionally simple and best-effort. Users are encouraged to
    implement custom crawlers by extending BaseCrawler.

    Strategies:
    - Looks for links containing job titles or greenhouse/lever/ashby embedded
    - Parses <a> tags and JSON-LD JobPosting schema
    """

    ats_name = "generic"

    def fetch_jobs(self, company: CompanyConfig) -> List[Job]:
        if not company.url:
            raise CrawlerError("Generic crawler requires company.url to be set")

        logger.info(f"Fetching generic career page for {company.name} at {company.url}")

        try:
            resp = self._get(company.url)
            html = resp.text
        except Exception as e:
            raise CrawlerError(f"Generic fetch failed for {company.name}: {e}") from e

        jobs: List[Job] = []

        # Try JSON-LD JobPosting
        jobs.extend(self._parse_json_ld(html, company))

        # Fallback: try to find anchor tags with typical job patterns
        if not jobs:
            jobs.extend(self._parse_anchor_tags(html, company))

        logger.info(f"Generic: {company.name} -> {len(jobs)} jobs")
        return jobs

    def _parse_json_ld(self, html: str, company: CompanyConfig) -> List[Job]:
        from bs4 import BeautifulSoup
        import json

        soup = BeautifulSoup(html, "lxml")
        jobs: List[Job] = []

        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string or "")
                # Could be list or dict
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "JobPosting":
                        title = item.get("title", "")
                        desc = item.get("description", "")
                        url = item.get("url", company.url)
                        location = ""
                        job_loc = item.get("jobLocation")
                        if isinstance(job_loc, dict):
                            addr = job_loc.get("address", {})
                            location = addr.get("addressLocality", "") or str(addr)
                        elif isinstance(job_loc, list) and job_loc:
                            addr = job_loc[0].get("address", {})
                            location = addr.get("addressLocality", "")

                        jobs.append(
                            Job(
                                id=f"{company.identifier}-{title}",
                                title=title,
                                company=company.name,
                                location=location,
                                description=desc,
                                description_plain=self.clean_html(desc),
                                url=url,
                                apply_url=url,
                                source="generic",
                                raw_data=item,
                            )
                        )
            except Exception:
                continue

        return jobs

    def _parse_anchor_tags(self, html: str, company: CompanyConfig) -> List[Job]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        jobs: List[Job] = []
        # Find all <a> with href containing job-like URLs
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if not text or len(text) < 3 or len(text) > 120:
                continue
            # Heuristics: href contains /job, /career, /opening, /position
            if re.search(r"/(job|career|opening|position|role)s?/", href, re.I) or re.search(
                r"(greenhouse|lever|ashby)", href, re.I
            ):
                # Expand relative URL
                from urllib.parse import urljoin

                full_url = urljoin(company.url or "", href)
                jobs.append(
                    Job(
                        id=f"{company.identifier}-{text}",
                        title=text,
                        company=company.name,
                        description="",
                        description_plain="",
                        url=full_url,
                        apply_url=full_url,
                        source="generic",
                        raw_data={"href": href, "text": text},
                    )
                )

        # Deduplicate by URL
        seen = set()
        deduped = []
        for j in jobs:
            if j.url not in seen:
                seen.add(j.url)
                deduped.append(j)
        return deduped[:100]  # limit
