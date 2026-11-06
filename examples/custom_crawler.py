"""
Example of how to extend JobMatcher with a custom crawler.

To add support for a new ATS or custom career page, simply:
1. Subclass BaseCrawler
2. Implement fetch_jobs()
3. Register it in CrawlerRegistry
"""

from typing import List
from jobmatcher.crawlers.base import BaseCrawler
from jobmatcher.crawlers.registry import CrawlerRegistry
from jobmatcher.models.company import CompanyConfig
from jobmatcher.models.job import Job


class WorkdayCrawler(BaseCrawler):
    """Example custom crawler for Workday-based career pages."""

    ats_name = "workday"

    def fetch_jobs(self, company: CompanyConfig) -> List[Job]:
        # Example implementation - you would implement real Workday API logic here
        # Workday often has endpoint like https://company.wd1.myworkdayjobs.com/wday/cxs/company/jobs

        if not company.url:
            # Construct typical Workday URL pattern if identifier provided
            url = f"https://{company.identifier}.wd1.myworkdayjobs.com/wday/cxs/{company.identifier}/jobs"
        else:
            url = company.url

        # This is simplified - real Workday requires POST with JSON payload
        # For demo, we fetch and parse naively
        try:
            resp = self._get(url)
            data = resp.json()
            # Workday response typically: {"jobPostings": [...]}
            jobs_raw = data.get("jobPostings", []) if isinstance(data, dict) else []
        except Exception as e:
            print(f"Workday fetch not implemented fully: {e}")
            return []

        jobs: List[Job] = []
        for raw in jobs_raw:
            jobs.append(
                Job(
                    id=raw.get("bulletFields", [""])[0] if raw.get("bulletFields") else str(raw.get("title")),
                    title=raw.get("title", ""),
                    company=company.name,
                    location=raw.get("locationsText", ""),
                    description=raw.get("bulletFields", [""])[0] if raw.get("bulletFields") else "",
                    description_plain=raw.get("bulletFields", [""])[0] if raw.get("bulletFields") else "",
                    url=company.url or "",
                    source="workday",
                    raw_data=raw,
                )
            )
        return jobs


# Register the custom crawler so it can be used via config
CrawlerRegistry.register("workday", WorkdayCrawler)


# Now you can use it in companies.json:
# {"name": "My Workday Company", "ats": "workday", "identifier": "mycompany", "url": "https://..."}

if __name__ == "__main__":
    print(f"Supported ATS after registration: {CrawlerRegistry.list_ats()}")
    # Example usage
    config = CompanyConfig(name="Example Workday Co", ats="workday", identifier="exampleco")
    crawler = CrawlerRegistry.create("workday")
    print(f"Crawler created: {crawler.ats_name}")
    # jobs = crawler.fetch_jobs(config)
