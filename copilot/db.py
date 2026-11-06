"""SQLite store: one row per posting, carrying it through every pipeline stage.

The DB is the source of truth. The Excel sheet is a regenerated view of it,
except for the Status and Notes columns, which flow back in (see export.py).
"""
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# New:    no documents yet
# Review: documents written, but something was flagged for you to check first
# Ready:  documents written, nothing flagged (or you reviewed them)
STATUSES = ["New", "Review", "Ready", "Applied", "Interviewing", "Offer", "Rejected", "Skipped"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                    TEXT PRIMARY KEY,   -- source:board:ats_id, stable across runs
    source                TEXT,
    company               TEXT,
    title                 TEXT,
    location              TEXT,
    country               TEXT,               -- when the ATS reports it
    department            TEXT,
    url                   TEXT,
    apply_url             TEXT,
    description           TEXT,
    posted_at             TEXT,
    first_seen            TEXT,
    last_seen             TEXT,
    is_active             INTEGER DEFAULT 1,

    -- pipeline progress: new -> filtered_out | pending -> scored | candidate
    --   -> graded | shortlisted -> tailored | error
    stage                 TEXT,
    filter_reason         TEXT,

    match_score           REAL,
    matched_skills        TEXT,               -- JSON list
    missing_skills        TEXT,               -- JSON list

    fit_score             INTEGER,            -- LLM grade, 0-100
    fit_reason            TEXT,
    dealbreakers          TEXT,               -- JSON list

    sponsorship_status    TEXT,               -- sponsors | no_sponsorship | not_mentioned
    sponsorship_evidence  TEXT,
    sponsorship_verified  INTEGER,
    sponsorship_basis     TEXT,
    multiplier            REAL,
    final_score           REAL,

    tailored_match        REAL,
    tailored_fit          INTEGER,            -- fit grade re-run on the tailored resume
    tailored_final        REAL,
    resume_path           TEXT,
    review_resume_path    TEXT,
    resume_tex_path       TEXT,               -- tailored .tex (open it in Overleaf)               -- same resume, changes in bold: for the user, never sent
    cover_letter_path     TEXT,
    tailor_flags          TEXT,               -- JSON list of guard warnings
    tailor_changes        TEXT,               -- JSON list of what was changed and why
    tailored_at           TEXT,
    docs_resume_sha       TEXT,               -- which resume upload the documents came from
    tailored_resume       TEXT,               -- JSON: the tailored resume itself (DOCX is a render of it)
    cover_letter_text     TEXT,               -- the cover letter itself (DOCX is a render of it)
    revision              INTEGER DEFAULT 0,  -- bumps on every regenerate / revise / edit
    tailor_report         TEXT,               -- JSON: requirements, bullet before/after, keyword coverage

    status                TEXT DEFAULT 'New', -- user-facing application status
    notes                 TEXT DEFAULT '',
    applied_at            TEXT,
    error                 TEXT,
    updated_at            TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT,
    finished_at  TEXT,
    stats        TEXT                         -- JSON counters for the run
);

CREATE TABLE IF NOT EXISTS edit_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT,
    edit_id     TEXT,               -- bullet id, e.g. E1.2, or "summary"
    before      TEXT,               -- the candidate's original wording
    proposed    TEXT,               -- what the AI wrote
    final       TEXT,               -- what the candidate ended up with
    verdict     TEXT,               -- keep | reject | rewrite
    company     TEXT,
    title       TEXT,
    writer      TEXT,               -- model that proposed it
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Results that depend only on their inputs, so they are never paid for twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    created_at  TEXT
);

-- Answers to application questions you approved; reused for similar questions.
CREATE TABLE IF NOT EXISTS answers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT,
    qkey        TEXT UNIQUE,        -- normalized question, for exact reuse
    answer      TEXT,
    source      TEXT,               -- you | ai_approved
    job_id      TEXT,               -- where it was first answered
    uses        INTEGER DEFAULT 0,
    updated_at  TEXT
);

-- Questions forms asked that nothing could answer: your to-answer inbox in the app.
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qkey        TEXT UNIQUE,
    question    TEXT,
    kind        TEXT,               -- text | textarea | select | radio | checkbox | combobox | buttons | consent
    options     TEXT,               -- JSON list (choice questions)
    required    INTEGER,
    company     TEXT,               -- where it was last seen
    seen        INTEGER DEFAULT 1,
    first_seen  TEXT,
    last_seen   TEXT
);

-- What was filled into each application, by the extension or the auto-apply runner.
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT,
    url           TEXT,
    ats           TEXT,
    mode          TEXT,             -- extension | prepared | auto
    outcome       TEXT,             -- submitted | prepared | needs_you | failed
    filled        TEXT,             -- JSON: [{label, value, source}]
    unfilled      TEXT,             -- JSON: [label]
    note          TEXT,
    created_at    TEXT
);
"""

# Run after _migrate, so an older DB has the columns before they are indexed.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jobs_stage ON jobs(stage);
CREATE INDEX IF NOT EXISTS idx_jobs_final ON jobs(final_score);
"""

JSON_FIELDS = {"matched_skills", "missing_skills", "dealbreakers", "tailor_flags", "tailor_changes",
               "tailored_resume", "tailor_report"}


_ready = set()      # DB paths whose schema is known to be current


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path):
    # The app and a run can be open at once: wait up to 30s for a write lock
    # instead of failing, and use WAL so readers never block the writer.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    if str(path) not in _ready:       # schema setup once per process, not on every click
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executescript(INDEXES)
        _ready.add(str(path))
    return conn


def text_key(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def cache_get(conn, key):
    row = conn.execute("SELECT value FROM llm_cache WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def cache_put(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO llm_cache (key, value, created_at) VALUES (?, ?, ?)",
                 (key, json.dumps(value), now()))


def daily_backup(path, keep=7):
    """One compressed copy of the DB per day, newest `keep` kept. Returns the new file, or None."""
    folder = Path(path).parent / "backups"
    out = folder / f"copilot-{datetime.now():%Y-%m-%d}.db.gz"
    if out.exists() or not Path(path).exists():
        return None
    folder.mkdir(exist_ok=True)
    tmp = folder / "backup.tmp"
    src, dst = sqlite3.connect(path, timeout=30), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    partial = out.with_suffix(".partial")
    with open(tmp, "rb") as fh, gzip.open(partial, "wb", compresslevel=6) as gz:
        shutil.copyfileobj(fh, gz)
    partial.replace(out)
    tmp.unlink()
    for old in sorted(folder.glob("copilot-*.db.gz"))[:-keep]:
        old.unlink()
    return out


def prune_closed(conn):
    """Closed postings that were filtered out never show anywhere: keep the row
    (it stops them coming back as new) and drop the text. A reopened posting
    gets its text back on the next fetch."""
    n = conn.execute("""UPDATE jobs SET description = '' WHERE stage = 'filtered_out' AND is_active = 0
                        AND COALESCE(description, '') <> ''""").rowcount
    conn.commit()
    return n


def _migrate(conn):
    """Add columns introduced after a DB was created. Never drops data."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for line in SCHEMA.split("CREATE TABLE IF NOT EXISTS runs")[0].splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1].rstrip(",") in ("TEXT", "REAL", "INTEGER") and parts[0] not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {parts[0]} {parts[1].rstrip(',')}")
    conn.commit()


def _encode(fields):
    return {k: (json.dumps(v) if k in JSON_FIELDS and v is not None else v) for k, v in fields.items()}


def decode(row):
    d = dict(row)
    for k in JSON_FIELDS:
        if d.get(k):
            d[k] = json.loads(d[k])
        else:
            d[k] = None if k in ("tailored_resume", "tailor_report") else []
    return d


def upsert_seen(conn, job):
    """Insert a newly fetched posting, or refresh last_seen on a known one.

    Returns True when the posting is new. Known postings keep their scores,
    status and notes; only the listing fields are refreshed.
    """
    ts = now()
    exists = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    if exists:
        conn.execute(
            """UPDATE jobs SET last_seen = ?, is_active = 1, title = ?, location = ?,
               url = ?, apply_url = ?, description = ? WHERE id = ?""",
            (ts, job["title"], job["location"], job["url"], job["apply_url"],
             job["description"], job["id"]),
        )
        return False
    conn.execute(
        """INSERT INTO jobs (id, source, company, title, location, country, department, url, apply_url,
                             description, posted_at, first_seen, last_seen, is_active, stage, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'new', 'New', ?)""",
        (job["id"], job["source"], job["company"], job["title"], job["location"], job.get("country"),
         job["department"], job["url"], job["apply_url"], job["description"],
         job["posted_at"], ts, ts, ts),
    )
    return True


def update(conn, job_id, **fields):
    fields = _encode(fields)
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def mark_closed(conn, companies_ok, seen_ids):
    """Deactivate postings that vanished from a board we fetched successfully.

    Boards that failed to fetch this run are left alone, so a network blip
    never marks a live job as closed.
    """
    if not companies_ok:
        return 0
    marks = ",".join("?" * len(companies_ok))
    rows = conn.execute(
        # Manual jobs were never on a crawled board, so their absence means nothing.
        f"SELECT id FROM jobs WHERE is_active = 1 AND source != 'manual' AND company IN ({marks})",
        list(companies_ok)
    ).fetchall()
    gone = [r["id"] for r in rows if r["id"] not in seen_ids]
    conn.executemany("UPDATE jobs SET is_active = 0 WHERE id = ?", [(i,) for i in gone])
    return len(gone)


def jobs_in_stage(conn, *stages):
    marks = ",".join("?" * len(stages))
    rows = conn.execute(f"SELECT * FROM jobs WHERE stage IN ({marks})", stages).fetchall()
    return [decode(r) for r in rows]


def jobs_for_export(conn):
    rows = conn.execute(
        """SELECT * FROM jobs WHERE stage IN ('scored', 'candidate', 'graded', 'shortlisted', 'tailored', 'error')
           ORDER BY (resume_path IS NULL), (fit_score IS NULL), fit_score DESC,
                    COALESCE(tailored_final, final_score, match_score) DESC"""
    ).fetchall()
    return [decode(r) for r in rows]


def set_status(conn, job_id, status, notes=None):
    if status not in STATUSES:
        raise ValueError(f"Unknown status {status!r}; expected one of {STATUSES}")
    fields = {"status": status}
    if notes is not None:
        fields["notes"] = notes
    if status == "Applied":
        row = conn.execute("SELECT applied_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row and not row["applied_at"]:
            fields["applied_at"] = now()
    update(conn, job_id, **fields)


def record_run(conn, started_at, stats):
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, stats) VALUES (?, ?, ?)",
        (started_at, now(), json.dumps(stats)),
    )


def reset_for_rescore(conn):
    """Send every untailored job back through prefilter and scoring.

    Use after changing the resume, config.yaml, or the scorer. Tailored jobs,
    statuses and notes are kept; LLM fit grades are kept too, so re-running
    does not pay for them twice.
    """
    n = conn.execute(
        """UPDATE jobs SET stage = 'new', filter_reason = NULL
           WHERE stage IN ('filtered_out', 'pending', 'scored', 'candidate', 'graded', 'shortlisted', 'error')
             AND NOT (stage = 'filtered_out' AND COALESCE(description, '') = '')"""
    ).rowcount
    conn.commit()
    return n


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def reset_for_new_resume(conn):
    """A different resume changes every score, so re-score everything.

    Unlike reset_for_rescore, fit grades are cleared too: they were judged
    against the old resume. Jobs with documents go back through the funnel as
    well, so the top ones get fresh documents; until then their old files stay
    linked and are marked as built from an older resume. Jobs you've applied
    to are never touched: their files are what you actually sent.
    """
    n = conn.execute(
        """UPDATE jobs SET stage = 'new', filter_reason = NULL, fit_score = NULL,
                  fit_reason = NULL, dealbreakers = NULL, tailored_fit = NULL
           WHERE stage IS NOT NULL AND stage <> 'filtered_out'
             AND status NOT IN ('Applied', 'Interviewing', 'Offer', 'Rejected')"""
    ).rowcount
    conn.commit()
    return n


# ---------------- edit feedback (the quality loop) ----------------

def add_feedback(conn, job, edit, verdict, final, writer=""):
    conn.execute(
        """INSERT INTO edit_feedback (job_id, edit_id, before, proposed, final, verdict, company, title,
                                      writer, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (job["id"], edit["id"], edit["before"], edit["after"], final, verdict,
         job["company"], job["title"], writer, now()))


def feedback_for(conn, job_id):
    """{edit_id: verdict} already given on this job."""
    return {r["edit_id"]: r["verdict"] for r in conn.execute(
        "SELECT edit_id, verdict FROM edit_feedback WHERE job_id = ? ORDER BY id", (job_id,))}


def feedback_examples(conn, good=5, bad=3):
    """Recent approved/rewritten edits (style to copy) and rejected ones (to avoid)."""
    approved = [dict(r) for r in conn.execute(
        "SELECT before, final FROM edit_feedback WHERE verdict IN ('keep','rewrite') ORDER BY id DESC LIMIT ?",
        (good,))]
    rejected = [dict(r) for r in conn.execute(
        "SELECT before, proposed FROM edit_feedback WHERE verdict = 'reject' ORDER BY id DESC LIMIT ?",
        (bad,))]
    return {"approved": approved, "rejected": rejected}


def feedback_stats(conn):
    rows = dict(conn.execute("SELECT verdict, COUNT(*) FROM edit_feedback GROUP BY verdict").fetchall())
    total = sum(rows.values())
    return {"total": total, "keep": rows.get("keep", 0), "rewrite": rows.get("rewrite", 0),
            "reject": rows.get("reject", 0),
            "keep_rate": rows.get("keep", 0) / total if total else None}


def question_key(question):
    """Normalize a form question so the same question on two forms matches."""
    q = re.sub(r"[^a-z0-9 ]", " ", (question or "").lower())
    return " ".join(w for w in q.split() if w not in {"please", "the", "a", "an", "your", "you", "do", "are"})


def save_answer(conn, question, answer, source="you", job_id=None):
    conn.execute("""INSERT INTO answers (question, qkey, answer, source, job_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(qkey) DO UPDATE SET answer = excluded.answer, source = excluded.source,
                                                    updated_at = excluded.updated_at""",
                 (question, question_key(question), answer, source, job_id, now()))
    conn.commit()


def find_answer(conn, question, min_similarity=0.88):
    """Your saved answer to this question or a near-identical one, else None."""
    from difflib import SequenceMatcher
    key = question_key(question)
    best, best_sim = None, 0.0
    for row in conn.execute("SELECT * FROM answers"):
        sim = 1.0 if row["qkey"] == key else SequenceMatcher(None, row["qkey"], key).ratio()
        if sim > best_sim:
            best, best_sim = row, sim
    if best is None or best_sim < min_similarity:
        return None
    conn.execute("UPDATE answers SET uses = uses + 1 WHERE id = ?", (best["id"],))
    conn.commit()
    return dict(best)


def log_application(conn, job_id, url, ats, mode, outcome, filled=(), unfilled=(), note=""):
    conn.execute("""INSERT INTO applications (job_id, url, ats, mode, outcome, filled, unfilled, note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (job_id, url, ats, mode, outcome, json.dumps(list(filled)), json.dumps(list(unfilled)), note, now()))
    conn.commit()


def company_applications_today(conn, company):
    day = datetime.now(timezone.utc).date().isoformat()
    return conn.execute("""SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id = a.job_id
                           WHERE j.company = ? AND a.outcome = 'submitted' AND a.created_at >= ?""",
                        (company, day)).fetchone()[0]


def applications_today(conn, mode="auto"):
    day = datetime.now(timezone.utc).date().isoformat()
    return conn.execute("SELECT COUNT(*) FROM applications WHERE mode = ? AND outcome = 'submitted' "
                        "AND created_at >= ?", (mode, day)).fetchone()[0]


def note_question(conn, question, kind="text", options=None, required=False, company=""):
    """Remember a form question nothing could answer, so you can answer it once."""
    ts = now()
    conn.execute("""INSERT INTO questions (qkey, question, kind, options, required, company, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(qkey) DO UPDATE SET seen = seen + 1, last_seen = excluded.last_seen,
                        company = excluded.company, options = excluded.options,
                        required = MAX(required, excluded.required)""",
                 (question_key(question), question, kind, json.dumps(options or []), int(bool(required)), company, ts, ts))
    conn.commit()


def open_questions(conn):
    """Questions forms asked that you haven't answered (or skipped) yet, most frequent first."""
    rows = conn.execute("""SELECT q.* FROM questions q LEFT JOIN answers a ON a.qkey = q.qkey
                           WHERE a.id IS NULL ORDER BY q.required DESC, q.seen DESC, q.last_seen DESC""").fetchall()
    return [{**dict(r), "options": json.loads(r["options"] or "[]")} for r in rows]


def all_answers(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM answers ORDER BY uses DESC, updated_at DESC")]


def delete_answer(conn, answer_id):
    conn.execute("DELETE FROM answers WHERE id = ?", (answer_id,))
    conn.commit()
