"""Today's shortlist: the few jobs worth your time today (11 by default).

Chosen in code from what the pipeline already knows, with no model call: the AI
fit grade, how fresh the posting is, what the posting says about sponsorship,
and whether you have already been shown the job on earlier days. One posting per
company and a few per job family keep the list varied; the same role posted in
several cities counts once.

Each day's list is stored, so yesterday's list stays readable and a job you keep
ignoring sinks instead of sitting at the top forever.
"""
import re
from datetime import date, datetime, timezone

from copilot import db, library

SIZE = 11
DEFAULTS = {"size": SIZE, "min_fit": 60, "per_company": 1, "per_family": 3}
DONE = ("Applied", "Interviewing", "Offer", "Rejected", "Skipped")
SCHEMA = """CREATE TABLE IF NOT EXISTS shortlist (
    day TEXT NOT NULL, job_id TEXT NOT NULL, rank INTEGER NOT NULL, score REAL, why TEXT, region TEXT,
    PRIMARY KEY (day, job_id))"""
REGIONS = {"us": "United States", "world": "Rest of the world"}
# Titles that say the role is for you rank a little higher.
LEVEL_POINTS = {"intern": (3, "internship"), "new grad": (3, "early-career title")}
# (posted within this many days, points, what to tell you)
FRESH = [(1, 10, "posted in the last 24 hours"), (3, 7, "posted in the last 3 days"), (7, 3, "posted this week"),
         (14, 0, ""), (30, -4, "posted over two weeks ago")]
STALE = (-8, "posted over a month ago")
SPONSOR = {"posting says sponsors": (4, "posting says it sponsors visas"),
           "employer has sponsored before": (2, "employer has sponsored before")}
RULED_OUT = "posting rules it out"


def options(cfg):
    return {**DEFAULTS, **(getattr(cfg, "shortlist", None) or {})}


def age_days(job, now=None):
    """Days since the company posted it, or since we first saw it when the board gives no date."""
    now = now or datetime.now(timezone.utc)
    for field in ("posted_at", "first_seen"):
        try:
            t = datetime.fromisoformat(str(job.get(field) or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max((now - t).total_seconds() / 86400, 0.0)
    return None


def freshness(job, now=None):
    age = age_days(job, now)
    if age is None:
        return 0, ""
    for days, points, text in FRESH:
        if age <= days:
            return points, text
    return STALE


def grade_priority(job, now=None):
    """Order for AI grading: the best local matches, fresh postings first among near-equals."""
    return (job.get("match_score") or 0) + freshness(job, now)[0] / 100


def same_role_key(job):
    """One key for the same role posted in several places ("SWE (NYC)", "SWE - Remote")."""
    title = re.sub(r"\(.*?\)|\[.*?\]", " ", (job.get("title") or "").lower())
    title = re.split(r"\s[-–—|]\s|,", title)[0]
    return (job.get("company") or "").lower().strip(), re.sub(r"[^a-z0-9+#]+", " ", title).strip()


def score(job, days_shown=0, now=None):
    """(points, [reasons]). The fit grade, adjusted for freshness, sponsorship and repeats."""
    points, why = float(job.get("fit_score") or 0), [f"fit {int(job.get('fit_score') or 0)}"]
    for extra, text in (freshness(job, now), SPONSOR.get(job.get("sponsorship_basis"), (0, "")),
                        LEVEL_POINTS.get(job.get("level"), (0, ""))):
        points += extra
        if text:
            why.append(text)
    if days_shown:
        points -= min(4 * days_shown, 16)
        why.append(f"shown on {days_shown} earlier day{'s' if days_shown > 1 else ''}")
    return points, why


def build(jobs, shown=None, opts=None, now=None):
    """The shortlist from graded jobs: [{"job", "score", "why"}], best first.

    jobs: rows with a fit grade. shown: {job id: earlier days it was listed}."""
    opts, shown = {**DEFAULTS, **(opts or {})}, shown or {}
    best = {}
    for job in jobs:
        if (not job.get("is_active") or job.get("status") in DONE or job.get("fit_score") is None
                or job["fit_score"] < opts["min_fit"] or job.get("sponsorship_basis") == RULED_OUT):
            continue
        points, why = score(job, shown.get(job["id"], 0), now)
        entry = {"job": job, "score": points, "why": why,
                 "family": job.get("role_family") or library.classify(job.get("title"), job.get("description"))}
        key = same_role_key(job)
        if key not in best or points > best[key]["score"]:
            best[key] = entry
    ranked = sorted(best.values(), key=lambda e: (e["score"], e["job"].get("match_score") or 0), reverse=True)

    picked, companies, families = [], {}, {}
    # Varied first; then, only if the list is still short, more of one family, then of one company.
    for company_cap, family_cap in ((opts["per_company"], opts["per_family"]), (opts["per_company"], 10 ** 6),
                                    (opts["per_company"] + 1, 10 ** 6)):
        for e in ranked:
            company = same_role_key(e["job"])[0]
            if (len(picked) >= opts["size"] or e in picked or companies.get(company, 0) >= company_cap
                    or families.get(e["family"], 0) >= family_cap):
                continue
            picked.append(e)
            companies[company] = companies.get(company, 0) + 1
            families[e["family"]] = families.get(e["family"], 0) + 1
    return sorted(picked, key=lambda e: e["score"], reverse=True)


def _today():
    return date.today().isoformat()


def _schema(conn):
    conn.execute(SCHEMA)
    if "region" not in {r[1] for r in conn.execute("PRAGMA table_info(shortlist)")}:
        conn.execute("ALTER TABLE shortlist ADD COLUMN region TEXT")


def regions(cfg):
    """The regions you search: config prefilter.regions, US only by default."""
    rules = getattr(cfg, "prefilter", None) or {}
    return [r for r in (rules.get("regions") or ["us"]) if r in REGIONS]


def refresh(conn, cfg, day=None, now=None):
    """Choose today's lists again (one per region) from the current grades and store them.
    Returns every picked entry, US first."""
    _schema(conn)
    day = day or _today()
    shown = {r["job_id"]: r["n"] for r in conn.execute(
        "SELECT job_id, COUNT(*) AS n FROM shortlist WHERE day < ? GROUP BY job_id", (day,))}
    jobs = db.jobs_in_stage(conn, "graded", "shortlisted", "tailored")
    conn.execute("DELETE FROM shortlist WHERE day = ?", (day,))
    picked = []
    for region in regions(cfg):
        mine = build([j for j in jobs if (j.get("region") or "us") == region], shown, options(cfg), now)
        conn.executemany("INSERT INTO shortlist (day, job_id, rank, score, why, region) VALUES (?, ?, ?, ?, ?, ?)",
                         [(day, e["job"]["id"], i, e["score"], "; ".join(e["why"]), region)
                          for i, e in enumerate(mine, 1)])
        picked += [{**e, "region": region} for e in mine]
    conn.commit()
    return picked


def stored(conn, day=None, region=None):
    """A day's list as stored: [{"job_id", "rank", "score", "why", "region"}] (empty if none was made)."""
    _schema(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT job_id, rank, score, why, COALESCE(region, 'us') AS region FROM shortlist WHERE day = ? "
        "ORDER BY COALESCE(region, 'us') DESC, rank", (day or _today(),))]
    return [r for r in rows if region in (None, r["region"])]


def today(conn, cfg, region=None):
    """Today's list for a region (or both): the stored one, or new ones the first time today."""
    if not stored(conn):
        refresh(conn, cfg)
    return stored(conn, region=region)


def days(conn, limit=14):
    """Days that have a stored list, newest first."""
    _schema(conn)
    return [r["day"] for r in conn.execute("SELECT DISTINCT day FROM shortlist ORDER BY day DESC LIMIT ?", (limit,))]
