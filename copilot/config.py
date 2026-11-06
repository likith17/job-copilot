import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# How the writer behaves; override any of it under `writing:` in config.yaml.
# Style and focus only: the honesty rules in tailor.py and the checks in code
# always apply on top.
DEFAULT_WRITING = {
    "instructions": (
        "You are a professional resume writer and recruiter. Tailor my resume to best match the "
        "job description so a recruiter will want to interview me, while staying completely "
        "truthful. Lead bullets with strong, varied verbs and the measurable outcome, mirror the "
        "posting's terminology where it truthfully fits, and keep each bullet to 1-2 lines of "
        "plain, human English."
    ),
    "sections": ["summary", "experience", "projects", "skills"],
    "letter_instructions": (
        "Write a specific, human cover letter about the value I can bring to this company: why "
        "this company and role, how my real experience fits their work, and how I can contribute. "
        "Open with the company's work, not 'I am excited to apply'. Plain English, no cliches."
    ),
    "letter_words": [250, 400],
    "letter_points": [],
    # Tools and topics studied in coursework but not on the resume. The letter
    # may mention them as coursework exposure, never as professional experience.
    "coursework": [],
}


@dataclass
class Config:
    resume: Path
    companies: Path
    data_dir: Path
    prefilter: dict = field(default_factory=dict)
    scoring: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    writing: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_WRITING))
    excel: str = "jobs.xlsx"
    apply: dict = field(default_factory=dict)
    cover_letter: Path = None   # set from uploads by inputs.apply()

    @property
    def db_path(self):
        return self.data_dir / "copilot.db"

    @property
    def master_resume_path(self):
        return self.data_dir / "master_resume.json"

    @property
    def output_dir(self):
        return self.data_dir / "applications"

    @property
    def excel_path(self):
        return self.data_dir / self.excel


def _merge(base, over):
    """Recursive dict merge; values in `over` win."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path=None):
    """Read config.yaml. Writing settings left out fall back to DEFAULT_WRITING."""
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    def resolve(p):
        p = Path(p)
        return p if p.is_absolute() else ROOT / p

    cfg = Config(
        resume=resolve(raw.get("resume", "resume.pdf")),
        companies=resolve(raw.get("companies", "companies.yaml")),
        data_dir=resolve(raw.get("data_dir", "data")),
        prefilter=raw.get("prefilter") or {},
        scoring=raw.get("scoring") or {},
        llm=raw.get("llm") or {},
        writing=_merge(DEFAULT_WRITING, raw.get("writing")),
        excel=raw.get("excel", "jobs.xlsx"),
        apply=raw.get("apply") or {},
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg

