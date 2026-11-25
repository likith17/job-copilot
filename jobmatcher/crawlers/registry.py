from __future__ import annotations

from typing import Dict, Type, List, Tuple, Optional
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time

from jobmatcher.crawlers.base import BaseCrawler
from jobmatcher.crawlers.greenhouse import GreenhouseCrawler
from jobmatcher.crawlers.lever import LeverCrawler
from jobmatcher.crawlers.ashby import AshbyCrawler
from jobmatcher.crawlers.generic import GenericCrawler
from jobmatcher.models.company import CompanyConfig
from jobmatcher.crawlers.deduplication import deduplicate_jobs

logger = logging.getLogger(__name__)


class CrawlerRegistry:
    """Registry for ATS crawlers - enables modular extensibility."""

    _registry: Dict[str, Type[BaseCrawler]] = {}

    @classmethod
    def register(cls, ats_name: str, crawler_cls: Type[BaseCrawler]) -> None:
        cls._registry[ats_name.lower()] = crawler_cls
        logger.debug(f"Registered crawler for ATS: {ats_name}")

    @classmethod
    def get(cls, ats_name: str) -> Type[BaseCrawler]:
        ats_name = ats_name.lower()
        if ats_name not in cls._registry:
            raise ValueError(f"No crawler registered for ATS '{ats_name}'. Available: {list(cls._registry.keys())}")
        return cls._registry[ats_name]

    @classmethod
    def list_ats(cls) -> List[str]:
        return sorted(cls._registry.keys())

    @classmethod
    def create(cls, ats_name: str, **kwargs) -> BaseCrawler:
        crawler_cls = cls.get(ats_name)
        return crawler_cls(**kwargs)


# Register built-ins
CrawlerRegistry.register("greenhouse", GreenhouseCrawler)
CrawlerRegistry.register("lever", LeverCrawler)
CrawlerRegistry.register("ashby", AshbyCrawler)
CrawlerRegistry.register("generic", GenericCrawler)
CrawlerRegistry.register("custom", GenericCrawler)


def get_crawler(ats_name: str, **kwargs) -> BaseCrawler:
    """Convenience function to instantiate a crawler."""
    return CrawlerRegistry.create(ats_name, **kwargs)


def list_supported_ats() -> List[str]:
    return CrawlerRegistry.list_ats()


def _fetch_single_company(
    company: CompanyConfig, crawler_kwargs: Dict
) -> Tuple[CompanyConfig, List, Optional[Exception]]:
    """Fetch jobs for a single company, returning (company, jobs, error)."""
    from jobmatcher.models.job import Job

    start = time.time()
    try:
        crawler = get_crawler(company.ats, **crawler_kwargs)
        jobs = crawler.fetch_jobs(company)
        elapsed = time.time() - start
        logger.info(f"{company.name}: {len(jobs)} jobs in {elapsed:.2f}s (thread={threading.current_thread().name})")
        return company, jobs, None
    except Exception as e:
        elapsed = time.time() - start
        logger.error(f"Failed to fetch jobs for {company.name} ({company.ats}) after {elapsed:.2f}s: {e}")
        return company, [], e


def fetch_all_jobs(
    companies: List[CompanyConfig],
    max_workers: int = 8,
    deduplicate: bool = True,
    dedup_strategy: str = "smart",
    concurrent: bool = True,
    **crawler_kwargs,
) -> List:
    """
    Fetch jobs from all companies.

    Args:
        companies: List of CompanyConfig
        max_workers: Max concurrent threads (when concurrent=True)
        deduplicate: Whether to automatically remove duplicate postings
        dedup_strategy: Deduplication strategy - "url", "strict", "smart", "aggressive"
        concurrent: If True, use ThreadPoolExecutor for parallel fetching.
                    If False, fallback to sequential (useful for debugging).
        **crawler_kwargs: Passed to crawler constructors (timeout, retries, delay, etc.)

    Returns:
        List of Job objects (deduped if enabled)
    """
    from jobmatcher.models.job import Job

    if not companies:
        return []

    all_jobs: List[Job] = []

    if concurrent and len(companies) > 1:
        # Concurrent fetching
        logger.info(f"Fetching {len(companies)} companies concurrently with {max_workers} workers")
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="crawler") as executor:
            future_to_company = {
                executor.submit(_fetch_single_company, company, crawler_kwargs): company for company in companies
            }

            for future in as_completed(future_to_company):
                company = future_to_company[future]
                try:
                    comp, jobs, error = future.result()
                    if error is None:
                        all_jobs.extend(jobs)
                except Exception as e:
                    logger.error(f"Unexpected error for {company.name}: {e}")
                    continue
    else:
        # Sequential fallback (preserves original behavior)
        logger.info(f"Fetching {len(companies)} companies sequentially")
        for company in companies:
            _, jobs, error = _fetch_single_company(company, crawler_kwargs)
            if error is None:
                all_jobs.extend(jobs)

    logger.info(f"Total jobs before deduplication: {len(all_jobs)}")

    if deduplicate:
        deduped, removed = deduplicate_jobs(all_jobs, strategy=dedup_strategy)
        logger.info(f"Deduplication ({dedup_strategy}): removed {removed} duplicates, {len(deduped)} remain")
        return deduped
    else:
        return all_jobs


def fetch_all_jobs_concurrent(
    companies: List[CompanyConfig],
    max_workers: int = 8,
    deduplicate: bool = True,
    dedup_strategy: str = "smart",
    **crawler_kwargs,
) -> List:
    """Convenience wrapper that always uses concurrent fetching."""
    return fetch_all_jobs(
        companies,
        max_workers=max_workers,
        deduplicate=deduplicate,
        dedup_strategy=dedup_strategy,
        concurrent=True,
        **crawler_kwargs,
    )


def fetch_all_jobs_sequential(
    companies: List[CompanyConfig],
    deduplicate: bool = True,
    dedup_strategy: str = "smart",
    **crawler_kwargs,
) -> List:
    """Convenience wrapper that always uses sequential fetching."""
    return fetch_all_jobs(
        companies,
        deduplicate=deduplicate,
        dedup_strategy=dedup_strategy,
        concurrent=False,
        **crawler_kwargs,
    )
