"""Resume library: one master resume per job family, chosen for each job.

A job's family comes from its title (and, for vague titles, from the posting's
wording): deterministic, no model call. The library maps families to the master
resumes you uploaded; a family with no resume of its own uses your default
resume (the current upload in the app).

Files live in data/inputs/resume/ like every upload, and are never modified:
tailoring writes copies into data/applications/. The library itself (labels and
family assignments) is stored in the database.
"""
import hashlib
import json
import re

from copilot import db, inputs

FAMILIES = {
    "software": "Software Engineer",
    "backend": "Backend Engineer",
    "fullstack": "Full-Stack Engineer",
    "ai": "AI Engineer",
    "ml": "ML Engineer",
    "data_engineer": "Data Engineer",
    "data_scientist": "Data Scientist",
    "fde": "Forward Deployed Engineer",
    "research": "Research Engineer",
}

# What a title says about the job's family. When two match, the one named first in
# the title wins ("Machine Learning Engineer, AI Platform" is an ML job).
TITLE = {
    "research": r"research (engineer|scientist)|applied scientist|\bresearcher\b|member of (the )?research",
    "fde": r"forward[- ]deployed|deployed engineer|solutions engineer|customer engineer|field engineer|"
           r"implementation engineer|deployment strategist",
    "data_scientist": r"data scien|\banalytics scientist|statistician|quantitative (analyst|researcher)|decision scien",
    "data_engineer": r"data engineer|analytics engineer|\betl\b|data platform|data infrastructure|data pipeline",
    "ml": r"machine learning|\bml\b|mlops|deep learning|computer vision|\bnlp\b|perception|autonomy",
    "ai": r"\bai\b|\bllms?\b|generative|genai|\bagents?\b|agentic|prompt engineer",
    "fullstack": r"full[- ]?stack|front[- ]?end|web (developer|engineer)|product engineer|ui engineer",
    "backend": r"back[- ]?end|\bapis?\b|platform engineer|infrastructure engineer|distributed systems|"
               r"systems engineer|site reliability",
}
# For titles that name no family ("Software Engineer II", "Member of Technical Staff"):
# the family the posting talks about most, if it clearly does.
POSTING = {
    "ml": r"machine learning|model training|pytorch|tensorflow|mlops|computer vision",
    "ai": r"\bllms?\b|large language model|generative ai|\brag\b|ai agents?|prompt",
    "data_engineer": r"\betl\b|data pipelines?|airflow|\bdbt\b|spark|data warehouse|snowflake",
    "data_scientist": r"a/b test|statistical|experimentation|causal|forecasting",
    "fullstack": r"react|typescript|front[- ]?end|full[- ]?stack|next\.js",
    "backend": r"back[- ]?end|microservices|distributed systems|\bapis?\b|kubernetes",
}
_TITLE = {k: re.compile(v, re.I) for k, v in TITLE.items()}
_POSTING = {k: re.compile(v, re.I) for k, v in POSTING.items()}
META_KEY = "resume_library"


def classify(title, description=""):
    """The job family for a posting: one of FAMILIES."""
    hits = [(m.start(), order, fam) for order, (fam, rx) in enumerate(_TITLE.items())
            if (m := rx.search(title or ""))]
    if hits:
        return min(hits)[2]
    counts = {fam: len(rx.findall((description or "")[:6000])) for fam, rx in _POSTING.items()}
    best = max(counts, key=counts.get)
    runner_up = sorted(counts.values())[-2]
    return best if counts[best] >= 3 and counts[best] >= 2 * runner_up else "software"


# ---------------- the library itself ----------------

def entries(conn):
    """{file name: {"label": str, "roles": [family, ...]}}"""
    raw = db.get_meta(conn, META_KEY)
    return json.loads(raw) if raw else {}


def _save(conn, lib):
    db.set_meta(conn, META_KEY, json.dumps(lib))
    conn.commit()


def assign(conn, file, label=None, roles=None):
    """Add a resume to the library, or change its label or families. A family has one
    resume: assigning it here takes it away from any other resume."""
    lib = entries(conn)
    entry = lib.setdefault(file, {"label": label or file, "roles": []})
    if label is not None:
        entry["label"] = label.strip() or file
    if roles is not None:
        roles = [r for r in roles if r in FAMILIES]
        for other, e in lib.items():
            if other != file:
                e["roles"] = [r for r in e["roles"] if r not in roles]
        entry["roles"] = roles
    _save(conn, lib)
    return lib


def remove(conn, file):
    """Take a resume out of the library. The file itself stays in your uploads."""
    lib = entries(conn)
    lib.pop(file, None)
    _save(conn, lib)


class Resolver:
    """Which resume a job uses, with the text, structure and hash of each resume
    read once per run."""

    def __init__(self, cfg, conn, default_text=None):
        """default_text: the default resume's text, when the caller has already read it."""
        self.cfg = inputs.apply(cfg)
        self.folder = self.cfg.data_dir / "inputs" / "resume"
        self.lib = {f: e for f, e in entries(conn).items() if (self.folder / f).exists()}
        self.by_family = {r: self.folder / f for f, e in self.lib.items() for r in e["roles"]}
        self._text, self._sha, self._master = {}, {}, {}
        if default_text is not None:
            self._text[self.cfg.resume] = default_text

    def pick(self, family):
        """The family's own resume, else the default one."""
        return self.by_family.get(family, self.cfg.resume)

    def family(self, job):
        return job.get("role_family") or classify(job.get("title"), job.get("description"))

    def for_job(self, job):
        return self.pick(self.family(job))

    def label(self, path):
        return self.lib.get(path.name, {}).get("label") or "Default resume"

    def text(self, path):
        if path not in self._text:
            self._text[path] = inputs.read_text(path)
        return self._text[path]

    def sha(self, path):
        if path not in self._sha:
            self._sha[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self._sha[path]

    def master(self, path, log=print):
        """The resume as structure for tailoring. A .tex is parsed in code; a PDF or
        DOCX needs the model to structure it once (see tailor.load_or_build_master)."""
        if path not in self._master:
            import dataclasses

            from copilot import latex, tailor
            if path.suffix.lower() == ".tex":
                self._master[path] = tailor.MasterResume.model_validate(
                    latex.parse(path.read_text(encoding="utf-8"))[0])
            else:
                self._master[path] = tailor.load_or_build_master(
                    dataclasses.replace(self.cfg, resume=path), self.text(path), log=log)
        return self._master[path]

    def signature(self):
        """Changes whenever any resume a job could use changes: scores are then redone."""
        parts = [self.sha(self.cfg.resume)] + sorted(f"{r}:{self.sha(p)}" for r, p in self.by_family.items())
        return hashlib.sha256("|".join(parts).encode()).hexdigest()
