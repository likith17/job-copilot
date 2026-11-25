from __future__ import annotations

import re
import logging
from typing import List, Tuple, Set, Dict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from jobmatcher.models.job import Job, RankedJob

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication: lower, strip, remove fragment, remove utm params."""
    if not url:
        return ""
    url = url.strip().lower()
    url = url.rstrip("/")
    # Remove fragment
    url = url.split("#")[0]

    try:
        parsed = urlparse(url)
        # Keep path normalized: lower already, remove duplicate slashes
        # Filter query params: drop utm_* and other tracking params, keep important ones like gh_jid, id
        qs = parse_qs(parsed.query)
        # Remove tracking params
        filtered_qs = {k: v for k, v in qs.items() if not k.lower().startswith("utm_")}
        # Sort query params for consistency
        new_query = urlencode(filtered_qs, doseq=True)
        normalized = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path.rstrip("/"),
                parsed.params,
                new_query,
                "",  # fragment removed
            )
        )
        return normalized.rstrip("/").lower()
    except Exception:
        # Fallback
        return url.lower()


def normalize_text(text: str) -> str:
    """Normalize text for deduplication: lower, collapse whitespace, strip."""
    if not text:
        return ""
    text = text.strip().lower()
    # Collapse multiple whitespace
    text = re.sub(r"\s+", " ", text)
    # Remove punctuation that might vary? Keep alphanumeric and spaces
    # Don't strip too aggressively - keep original but lower
    return text


def is_location_similar(loc1: str, loc2: str) -> bool:
    """
    Check if two locations are similar enough to be considered same.
    Used for fuzzy matching: "Remote - US" vs "Remote", "San Francisco, CA" vs "San Francisco"
    """
    if not loc1 or not loc2:
        return False
    n1 = normalize_text(loc1)
    n2 = normalize_text(loc2)
    if n1 == n2:
        return True
    # One contains the other
    if n1 in n2 or n2 in n1:
        return True
    # Both contain remote
    remote_terms = {"remote", "wfh", "work from home"}
    if any(term in n1 for term in remote_terms) and any(term in n2 for term in remote_terms):
        return True
    return False


def deduplicate_jobs(
    jobs: List[Job],
    strategy: str = "smart",
    keep_first: bool = True,
) -> Tuple[List[Job], int]:
    """
    Remove duplicate job postings.

    Strategies:
    - "url": only deduplicate by exact normalized URL
    - "strict": url + exact company+title+location
    - "smart": (default) url + company+title+location + cross-ATS company+title (different sources)
              This covers duplicates appearing across Greenhouse, Lever, Ashby.
    - "aggressive": company+title only, regardless of location/source

    Args:
        jobs: List of Job objects
        strategy: deduplication strategy
        keep_first: if True, keep first occurrence; otherwise keep last

    Returns:
        (deduped_jobs, removed_count)
    """
    if not jobs:
        return [], 0

    seen_urls: Set[str] = set()
    seen_ctl: Set[Tuple[str, str, str]] = set()  # company, title, location
    seen_ct: Set[Tuple[str, str]] = set()  # company, title
    # For smart cross-ATS detection: map ct -> set of sources seen
    ct_to_sources: Dict[Tuple[str, str], Set[str]] = {}
    ct_to_locations: Dict[Tuple[str, str], Set[str]] = {}

    deduped: List[Job] = []
    removed = 0

    # If not keep_first, iterate reversed and then reverse result
    iterable = jobs if keep_first else list(reversed(jobs))

    for job in iterable:
        url_key = normalize_url(job.url or job.apply_url or "")
        company_key = normalize_text(job.company)
        title_key = normalize_text(job.title)
        location_key = normalize_text(job.location)

        ctl_key = (company_key, title_key, location_key)
        ct_key = (company_key, title_key)

        is_duplicate = False
        reason = ""

        # URL dedup (strongest signal)
        if url_key and url_key in seen_urls:
            is_duplicate = True
            reason = f"duplicate URL: {url_key}"

        # Exact company+title+location
        elif strategy in ("strict", "smart", "aggressive") and ctl_key in seen_ctl:
            is_duplicate = True
            reason = f"duplicate company+title+location: {ctl_key}"

        # Smart cross-ATS: same company+title but different source
        elif strategy == "smart" and ct_key in seen_ct:
            # Check if this ct_key was seen from different source, or location similar
            prev_sources = ct_to_sources.get(ct_key, set())
            prev_locations = ct_to_locations.get(ct_key, set())

            # If source differs and location is similar or not specified, consider duplicate
            # This handles case where same job appears on Greenhouse and Lever with slightly different URLs
            if job.source.lower() not in prev_sources:
                # Different ATS source
                # Check location similarity if we have locations
                if not location_key or any(is_location_similar(location_key, prev_loc) for prev_loc in prev_locations) or not prev_locations:
                    # Also check if title is exactly same (not just substring)
                    # For safety, require title exact match (normalized)
                    is_duplicate = True
                    reason = f"cross-ATS duplicate: {ct_key} from {job.source} vs {prev_sources}"
            # Also aggressive sub-check: if location is similar and company+title same, even same source might be duplicate posting
            elif any(is_location_similar(location_key, prev_loc) for prev_loc in prev_locations):
                # Same source, same company+title, similar location -> likely same job posted twice in same ATS with different ID?
                # Keep conservative: only duplicate if URL empty or location exact
                # We'll not mark as duplicate here unless URL check already caught it, to avoid over-dedup of multi-location postings
                pass

        # Aggressive: company+title only
        elif strategy == "aggressive" and ct_key in seen_ct:
            is_duplicate = True
            reason = f"aggressive duplicate company+title: {ct_key}"

        if is_duplicate:
            removed += 1
            logger.debug(f"Removed duplicate job: {job.company} - {job.title} ({job.location}) - reason: {reason}")
            continue

        # Not duplicate, add to seen
        if url_key:
            seen_urls.add(url_key)
        if ctl_key != ("", "", ""):
            seen_ctl.add(ctl_key)
        if ct_key != ("", ""):
            seen_ct.add(ct_key)
            ct_to_sources.setdefault(ct_key, set()).add(job.source.lower())
            ct_to_locations.setdefault(ct_key, set()).add(location_key)

        deduped.append(job)

    if not keep_first:
        deduped.reverse()

    logger.info(f"Deduplication ({strategy}): {len(jobs)} -> {len(deduped)} (removed {removed})")
    return deduped, removed


def deduplicate_ranked_jobs(
    ranked_jobs: List[RankedJob],
    strategy: str = "smart",
) -> Tuple[List[RankedJob], int]:
    """
    Deduplicate RankedJobs based on their underlying Job.
    Keeps the highest scored version when duplicates found (unless keep_first logic).
    """
    if not ranked_jobs:
        return [], 0

    # Group by dedup keys, keep highest score
    url_to_best: Dict[str, RankedJob] = {}
    ctl_to_best: Dict[Tuple[str, str, str], RankedJob] = {}
    ct_to_best: Dict[Tuple[str, str], RankedJob] = {}

    # For simplicity, reuse deduplicate_jobs logic but with scoring awareness
    # We'll walk through ranked_jobs (which should already be sorted by score desc)
    # and keep first occurrence (highest score)
    seen_urls: Set[str] = set()
    seen_ctl: Set[Tuple[str, str, str]] = set()
    seen_ct: Set[Tuple[str, str]] = set()
    ct_to_sources: Dict[Tuple[str, str], Set[str]] = {}
    ct_to_locations: Dict[Tuple[str, str], Set[str]] = {}

    deduped: List[RankedJob] = []
    removed = 0

    for rj in ranked_jobs:
        job = rj.job
        url_key = normalize_url(job.url or job.apply_url or "")
        company_key = normalize_text(job.company)
        title_key = normalize_text(job.title)
        location_key = normalize_text(job.location)

        ctl_key = (company_key, title_key, location_key)
        ct_key = (company_key, title_key)

        is_duplicate = False

        if url_key and url_key in seen_urls:
            is_duplicate = True
        elif strategy in ("strict", "smart", "aggressive") and ctl_key in seen_ctl:
            is_duplicate = True
        elif strategy == "smart" and ct_key in seen_ct:
            prev_sources = ct_to_sources.get(ct_key, set())
            prev_locations = ct_to_locations.get(ct_key, set())
            if job.source.lower() not in prev_sources:
                if not location_key or any(is_location_similar(location_key, prev_loc) for prev_loc in prev_locations) or not prev_locations:
                    is_duplicate = True
        elif strategy == "aggressive" and ct_key in seen_ct:
            is_duplicate = True

        if is_duplicate:
            removed += 1
            continue

        if url_key:
            seen_urls.add(url_key)
        seen_ctl.add(ctl_key)
        if ct_key != ("", ""):
            seen_ct.add(ct_key)
            ct_to_sources.setdefault(ct_key, set()).add(job.source.lower())
            ct_to_locations.setdefault(ct_key, set()).add(location_key)

        deduped.append(rj)

    logger.info(f"Deduplication (ranked, {strategy}): {len(ranked_jobs)} -> {len(deduped)} (removed {removed})")
    return deduped, removed
