from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional
import time
import logging

import requests

from jobmatcher.models.job import Job
from jobmatcher.models.company import CompanyConfig

logger = logging.getLogger(__name__)


class CrawlerError(Exception):
    """Custom exception for crawler failures."""

    pass


class BaseCrawler(ABC):
    """Abstract base for all ATS crawlers."""

    # Each subclass should define ats_name
    ats_name: str = "base"

    def __init__(self, timeout: int = 15, retries: int = 2, delay: float = 0.5, user_agent: Optional[str] = None):
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or "Mozilla/5.0 (compatible; JobMatcher/0.1; +https://github.com/jobmatcher)",
                "Accept": "application/json, text/html",
            }
        )

    @abstractmethod
    def fetch_jobs(self, company: CompanyConfig) -> List[Job]:
        """Fetch jobs for given company. Must be implemented by subclasses."""
        pass

    def _get(self, url: str, params: Optional[dict] = None) -> requests.Response:
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "2"))
                    logger.warning(f"Rate limited (429) for {url}, sleeping {retry_after}s")
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                time.sleep(self.delay)
                return resp
            except requests.RequestException as e:
                last_exc = e
                logger.warning(f"Attempt {attempt+1}/{self.retries+1} failed for {url}: {e}")
                if attempt < self.retries:
                    time.sleep(self.delay * (attempt + 1))
                else:
                    break
        raise CrawlerError(f"Failed to GET {url} after {self.retries+1} attempts: {last_exc}") from last_exc

    def _post(self, url: str, json_data: Optional[dict] = None) -> requests.Response:
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.post(url, json=json_data, timeout=self.timeout)
                resp.raise_for_status()
                time.sleep(self.delay)
                return resp
            except requests.RequestException as e:
                last_exc = e
                logger.warning(f"POST attempt {attempt+1} failed for {url}: {e}")
                if attempt < self.retries:
                    time.sleep(self.delay * (attempt + 1))
        raise CrawlerError(f"Failed to POST {url}: {last_exc}") from last_exc

    @staticmethod
    def clean_html(html: str) -> str:
        """Strip HTML tags to plain text roughly."""
        if not html:
            return ""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
            return soup.get_text(separator="\n", strip=True)
        except Exception:
            # Fallback regex
            import re

            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text)
            return text.strip()
