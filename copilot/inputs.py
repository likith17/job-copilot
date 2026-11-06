"""Uploaded resume and cover letter versions.

Every upload is kept under data/inputs/<kind>/ with a timestamp, and the most
recent upload becomes the default for later runs. config.yaml's `resume` is
only the fallback for before anything has been uploaded.
"""
import dataclasses
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

KINDS = ("resume", "cover_letter")
ALLOWED = {".pdf", ".docx", ".tex"}   # .tex: Overleaf source, tailored in place


def _dir(cfg):
    return cfg.data_dir / "inputs"


def _state_path(cfg):
    return _dir(cfg) / "current.json"


def _state(cfg):
    try:
        return json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class LooksGenerated(ValueError):
    """The upload looks like a tailored resume, not the master resume."""


def looks_generated(cfg, filename, data):
    """Is this file a tailored resume (ours or another tool's) instead of the master?
    Tailoring a tailored resume drifts further from the real one every round."""
    stem = Path(filename).stem
    if re.search(r"_resume_|^review_|_20\d{6}$", stem, re.I):
        return True
    digest = hashlib.sha256(data).hexdigest()
    for old in (cfg.data_dir / "applications").glob("*/*"):
        if old.is_file() and old.stat().st_size == len(data) and hashlib.sha256(old.read_bytes()).hexdigest() == digest:
            return True
    return False


def save_upload(cfg, kind, filename, data: bytes, force=False):
    """Store an uploaded file and make it the default for `kind`."""
    if kind not in KINDS:
        raise ValueError(kind)
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError(f"{filename}: upload a PDF, DOCX or .tex")
    if kind == "resume" and not force and looks_generated(cfg, filename, data):
        raise LooksGenerated(f"{filename} looks like a resume already tailored for one job. Upload your master "
                             "resume instead (your Overleaf .tex is best): tailoring a tailored resume drifts "
                             "further from the real one, and every upload re-grades all your jobs.")
    folder = _dir(cfg) / kind
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(filename).stem)[:60]
    path = folder / f"{datetime.now():%Y-%m-%d_%H%M%S}_{stem}{suffix}"
    path.write_bytes(data)
    set_current(cfg, kind, path)
    return path


def set_current(cfg, kind, path):
    state = _state(cfg)
    state[kind] = str(path)
    _dir(cfg).mkdir(parents=True, exist_ok=True)
    _state_path(cfg).write_text(json.dumps(state, indent=2), encoding="utf-8")


def current(cfg, kind):
    """The default file for `kind`: the last upload, else the config fallback."""
    p = _state(cfg).get(kind)
    if p and Path(p).exists():
        return Path(p)
    if kind == "resume" and cfg.resume.exists():
        return cfg.resume
    return None


def history(cfg, kind):
    folder = _dir(cfg) / kind
    return sorted(folder.glob("*"), reverse=True) if folder.exists() else []


def apply(cfg):
    """A copy of cfg pointing at the current uploads."""
    return dataclasses.replace(cfg, resume=current(cfg, "resume") or cfg.resume,
                               cover_letter=current(cfg, "cover_letter"))


def read_text(path):
    if not path:
        return ""
    if Path(path).suffix.lower() == ".tex":
        from copilot import latex
        from copilot.tailor import resume_to_text
        tex = Path(path).read_text(encoding="utf-8")
        try:
            return resume_to_text(latex.parse(tex)[0])      # a resume: sections and bullets
        except latex.LaTeXError:
            body = tex.split(r"\begin{document}")[-1].split(r"\end{document}")[0]
            return latex.to_plain(body)                      # e.g. a cover letter
    from resume_reader import read_resume
    return read_resume(str(path))
