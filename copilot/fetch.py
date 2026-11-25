"""Stage 1: pull postings from ATS boards and drop the obviously irrelevant ones.

Crawling is delegated to the vendored `jobmatcher` package (Greenhouse, Lever,
Ashby). Everything here is cheap: no embeddings, no API calls.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from jobmatcher.crawlers.registry import get_crawler
from jobmatcher.models.company import CompanyConfig

log = logging.getLogger(__name__)


def load_companies(path):
    return CompanyConfig.load_list_from_yaml(str(path))


def _country(raw):
    return ((((raw or {}).get("address") or {}).get("postalAddress") or {}).get("addressCountry")  # Ashby
            or (raw or {}).get("country"))                                                         # Lever


def _normalize(job, company):
    return {
        "id": f"{job.source}:{company.identifier}:{job.id}",
        "source": job.source,
        "company": company.name,
        "title": (job.title or "").strip(),
        "location": job.location or "",
        "country": _country(job.raw_data),
        "department": job.department or "",
        "url": job.url,
        "apply_url": job.apply_url or job.url,
        "description": job.description_plain or "",
        "posted_at": job.posted_at,
    }


def fetch_all(companies, max_workers=24):
    """Fetch every board concurrently (network-bound, so many threads at once).

    Returns (jobs, ok_companies, failures) so the caller can tell a board that
    has no openings apart from a board that failed to load.
    """
    jobs, ok, failures = [], set(), {}

    def one(company):
        crawler = get_crawler(company.ats, timeout=20, retries=2, delay=0.2)
        return company, crawler.fetch_jobs(company)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(one, c): c for c in companies}
        for fut in as_completed(futures):
            company = futures[fut]
            try:
                _, fetched = fut.result()
            except Exception as exc:
                failures[company.name] = str(exc)[:200]
                log.warning("fetch failed for %s: %s", company.name, exc)
                continue
            ok.add(company.name)
            jobs.extend(_normalize(j, company) for j in fetched)

    # Same posting can appear twice on one board (multi-location listings).
    unique = {j["id"]: j for j in jobs}
    return list(unique.values()), ok, failures


# ---------------- prefilter ----------------

def _term_pattern(terms):
    if not terms:
        return None
    alts = "|".join(re.escape(t.strip().lower()) for t in terms if t.strip())
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)")


US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_MARKERS = re.compile(
    r"\b(united states|usa|u\.s\.|us|america|"
    r"san francisco|new york|nyc|seattle|boston|austin|chicago|los angeles|mountain view|"
    r"palo alto|menlo park|sunnyvale|san jose|san mateo|redwood city|"
    r"denver|atlanta|miami|washington|pittsburgh|philadelphia|san diego|salt lake|portland|"
    r"bay area|silicon valley|brooklyn|jersey city)\b"
)
# State codes only count in "City, ST" form; bare "IN"/"OR"/"ME" are ordinary words.
US_STATE_SUFFIX = re.compile(r",\s*(?:" + "|".join(US_STATES) + r")\b")
NON_US = re.compile(
    r"\b(london|uk|united kingdom|england|dublin|ireland|paris|france|berlin|munich|germany|"
    r"amsterdam|netherlands|zurich|switzerland|madrid|spain|lisbon|stockholm|warsaw|poland|"
    r"toronto|vancouver|montreal|canada|mexico|são paulo|sao paulo|brazil|bangalore|bengaluru|"
    r"hyderabad|india|singapore|tokyo|japan|seoul|korea|sydney|australia|tel aviv|israel|"
    r"emea|apac|latam|europe|hong kong|shanghai|beijing|china|taipei|manila|dubai)\b"
)


def is_us(job):
    """Best-effort US check. Prefers structured country data when the ATS gives it."""
    country = job.get("country")
    if country:
        return country in ("United States", "US", "USA")

    loc = job["location"].lower()
    if not loc.strip():
        return True  # unknown location: keep it, let scoring decide
    if US_MARKERS.search(loc) or US_STATE_SUFFIX.search(job["location"]):
        return True
    # "Remote" with no region is ambiguous; keep it.
    if loc.strip() in ("remote", "hybrid", "anywhere"):
        return True
    return not NON_US.search(loc)


# "Software Engineer 3", "Engineer IV", "SDE L5": levels above early career.
SENIOR_LEVEL = re.compile(r"\b(?:engineer|developer|scientist|sde|swe|analyst)\s*(?:[3-9]|iii|iv|v|vi)\b|\bl[5-9]\b")


def region(job):
    """"us" or "world": the two categories you choose between."""
    return "us" if is_us(job) else "world"


# Titles that say the role is for early-career people, in the order checked.
LEVELS = [
    ("intern", re.compile(r"\bintern(?:ship)?s?\b|\bco-?op\b|apprentice|working student|werkstudent|"
                          r"\bstudent (?:worker|researcher|engineer)", re.I)),
    ("new grad", re.compile(r"new[- ]grad|\bgraduate\b|\bgrad\b|university|campus|early[- ]career|entry[- ]level|"
                            r"\bjunior\b|\bjr\.?(?!\w)|\bassociate\b|residen(?:cy|t)\b|"
                            r"\b(?:engineer|developer|scientist|analyst)\s*(?:i|1)\b(?!\s*i)|level (?:i|1)\b", re.I)),
]


def level(job):
    """intern | new grad | "N+ yrs" (what the posting asks) | "not stated"."""
    for name, rx in LEVELS:
        if rx.search(job.get("title") or ""):
            return name
    years = min_years_required(job.get("description"))
    return f"{years}+ yrs" if years else "not stated"


ENGLISH = re.compile(r"\b(?:the|and|to|of|you|with|for|our|we|in|is|are|will|your)\b", re.I)
WORD = re.compile(r"\w+")


def looks_english(text):
    """A posting in German, French, Japanese... is skipped: the matcher and the
    grader read English, and you apply in English."""
    sample = (text or "")[:3000]
    words = len(WORD.findall(sample))
    return words < 60 or len(ENGLISH.findall(sample)) / words > 0.08


NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
NUMBER_WORD = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b(?=\s*(?:\(\d+\)\s*)?\+?\s*(?:or more\s+)?years?)", re.I)


OR_MORE = re.compile(r"(\d{1,2})\s+or more(?=\s+years?)", re.I)


YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*years?(?:'|’)?\s+(?:of\s+)?"
    r"(?:\w+\s+){0,4}?(?:experience|exp\b)",
    re.IGNORECASE,
)


def min_years_required(text):
    """Smallest 'N+ years of experience' figure in a posting, or None.

    Taking the minimum is deliberate: "3+ years required, 5+ preferred" should
    count as 3, not 5.
    """
    text = NUMBER_WORD.sub(lambda m: str(NUMBER_WORDS[m.group(1).lower()]), text or "")
    text = OR_MORE.sub(lambda m: m.group(1) + "+", text)
    nums = [int(m.group(1)) for m in YEARS.finditer(text)]
    nums = [n for n in nums if 0 < n < 30]
    return min(nums) if nums else None


def _parse_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def prefilter_reason(job, rules):
    """Return why a job is filtered out, or None if it should be scored."""
    title = job["title"].lower()

    include = _term_pattern(rules.get("title_include"))
    if include and not include.search(title):
        return "title: not a target role"
    exclude = _term_pattern(rules.get("title_exclude"))
    if exclude:
        m = exclude.search(title)
        if m:
            return f"title: excluded term '{m.group(0)}'"

    if SENIOR_LEVEL.search(title):
        return "title: senior level number"
    regions = rules.get("regions") or (["us"] if rules.get("us_only", True) else ["us", "world"])
    if region(job) not in regions:
        return f"location: {job['location'][:60]}"

    max_age = rules.get("max_age_days")
    posted = _parse_date(job.get("posted_at"))
    if max_age and posted and posted < datetime.now(timezone.utc) - timedelta(days=max_age):
        return f"age: posted {posted.date()}"

    max_years = rules.get("max_years_required")
    if max_years is not None:
        years = min_years_required(job["description"])
        if years is not None and years > max_years:
            return f"experience: {years}+ years"

    if len(job["description"]) < 200:
        return "description: too short to score"
    if not looks_english(job["description"]):
        return "language: posting is not in English"
    return None
