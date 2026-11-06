"""Training data for your own resume-writing model.

    python -m copilot train-data --teacher kimi-k3:cloud --jobs 150     generate (free Ollama cloud teacher)
    python -m copilot train-data --build                                 assemble data/training/sft/*.jsonl

Where examples come from, best first:
  1. your reviews in the app: 👍 kept and ✏️ rewritten edits (gold), 👎 rejected (-> "skip")
  2. every tailoring run (Claude or any model): edits that passed every code check
     and the claims audit (logged by bullets.log_run)
  3. teacher runs from `generate`: the same, on crawled jobs you never tailored

Only edits that pass the same checks the app enforces are kept (rejection
sampling), so the model learns the honest edits, not the teacher's mistakes.
Untouched bullets become "skip" examples, so it learns when to leave a bullet
alone. The eval set's jobs are never trained on.

Task format = copilot.bullets.bullet_prompt with no style instructions: the
fine-tuned model learns the style, and short prompts keep it fast on a laptop GPU.
"""
import json
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from copilot import bullets, db, inputs, llm, tailor

OUT = "sft"


def master_for_training(cfg, say=print):
    """Your latest LaTeX resume, parsed in code (no LLM), else the app's master resume."""
    from copilot import latex
    texs = sorted((cfg.data_dir / "inputs" / "resume").glob("*.tex"))
    if texs:
        return latex.parse(texs[-1].read_text(encoding="utf-8"))[0]
    return tailor.load_or_build_master(cfg, inputs.read_text(cfg.resume), log=say).model_dump()


def _eval_ids(cfg):
    path = cfg.data_dir / "eval_jobs.json"
    return set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()


def generate(cfg, teacher, n_jobs=100, workers=4, say=print):
    """Run a teacher model over graded jobs; log every bullet with its verdict."""
    cfg = inputs.apply(cfg)
    settings = {**llm.DEFAULT_PROVIDERS["local"], **(cfg.llm.get("providers") or {}).get("local", {}),
                "provider": "local", "model": teacher, "workers": 4, "mode": "per_bullet"}
    master = master_for_training(cfg, say)
    conn = db.connect(cfg.db_path)
    done = defaultdict(set)
    if bullets.LOG.exists():
        for line in bullets.LOG.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done[r["model"]].add(r["job_id"])
    skip = _eval_ids(cfg) | done[teacher]
    rows = conn.execute("""SELECT * FROM jobs WHERE fit_score IS NOT NULL AND length(description) > 800
                           ORDER BY fit_score DESC""").fetchall()
    jobs = [db.decode(r) for r in rows if r["id"] not in skip][:n_jobs]
    sections = tuple(cfg.writing.get("sections") or tailor.ALL_SECTIONS)
    check = {**settings, "effort": "low"}
    say(f"Teacher {teacher}: {len(jobs)} jobs ({len(done[teacher])} already done)")

    def one(job):
        try:
            plan = bullets.make_plan(master, job, settings, cfg.writing, None, sections, frame=False)
            resume, report = tailor.apply_plan(master, plan, sections)
            report["rejected"] += plan["pre_rejected"]
            report["targets"] = plan["targets"]
            changed = "\n".join(a["after"] for a in report["accepted"])
            if changed:
                tailor.revert_flagged(master, resume, report, tailor.audit_claims(master, changed, "", check, ""))
            bullets.log_run(master, job, report, settings, "teacher")
            return len(report["accepted"]), len(report["rejected"])
        except llm.LLMUnavailable:
            raise
        except llm.LLMError as exc:
            say(f"  {job['company']}: {str(exc)[:120]}")
            return 0, 0

    totals = [0, 0]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (acc, rej) in enumerate(pool.map(one, jobs), 1):
            totals[0] += acc
            totals[1] += rej
            if i % 10 == 0 or i == len(jobs):
                say(f"  {i}/{len(jobs)} jobs: {totals[0]} accepted edits, {totals[1]} rejected")
    return totals


def _answer(skip, text, reason):
    return json.dumps({"skip": skip, "text": text, "reason": reason}, ensure_ascii=False)


def _targets(rec):
    return rec.get("targets") or bullets.targets_for(rec["bullet_id"], rec["bullet"], rec["requirements"])


def _example(rec, skip, text, reason):
    system, user = bullets.bullet_prompt(rec["bullet"], rec["item"], rec["requirements"], _targets(rec))
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user},
                         {"role": "assistant", "content": _answer(skip, text, reason)}],
            "job_id": rec["job_id"], "source": rec.get("source", rec.get("mode"))}


def build(cfg, skip_share=0.25, val_share=0.1, seed=7, say=print):
    """Write data/training/sft/{train,val}.jsonl (chat format) and prefs.jsonl (for preference tuning later)."""
    rng = random.Random(seed)
    held_out = _eval_ids(cfg)
    records = [json.loads(l) for l in bullets.LOG.read_text(encoding="utf-8").splitlines()] if bullets.LOG.exists() else []
    records = [r for r in records if r["job_id"] not in held_out and r["requirements"]]

    by_key = defaultdict(list)            # (job, bullet) -> every attempt at it
    for r in records:
        by_key[(r["job_id"], r["bullet_id"])].append(r)
    positives, skips, prefs = [], [], []
    jobs_with_edits = {r["job_id"] for r in records if r["verdict"] == "accepted"}
    for (job_id, _), recs in by_key.items():
        good = [r for r in recs if r["verdict"] == "accepted"]
        bad = [r for r in recs if r["verdict"] == "rejected" and r["text"]]
        seen = set()
        for r in good:
            if r["text"] not in seen:
                seen.add(r["text"])
                positives.append(_example(r, False, r["text"], r["reason"] or "surfaces a posting requirement"))
        if not good and job_id in jobs_with_edits and _targets(recs[0]) and any(r["verdict"] == "untouched" for r in recs):
            skips.append(_example(recs[0], True, recs[0]["bullet"], "no honest improvement for this posting"))
        for b in bad:     # chosen: an accepted edit, or leaving it alone
            chosen = _answer(False, good[0]["text"], good[0]["reason"]) if good else _answer(True, b["bullet"], "")
            system, user = bullets.bullet_prompt(b["bullet"], b["item"], b["requirements"], _targets(b))
            prefs.append({"prompt": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                          "chosen": chosen, "rejected": _answer(False, b["text"], ""), "job_id": job_id})

    gold = _reviews(cfg, held_out)
    rng.shuffle(skips)
    n_skip = int(skip_share * (len(positives) + len(gold)) / (1 - skip_share))
    data = positives + gold * 3 + skips[:n_skip]       # your own reviews count three times
    jobs = sorted({d["job_id"] for d in data})
    rng.shuffle(jobs)
    val_jobs = set(jobs[:max(1, int(len(jobs) * val_share))]) if len(jobs) > 5 else set()
    out = cfg.data_dir / "training" / OUT
    out.mkdir(parents=True, exist_ok=True)
    rng.shuffle(data)
    train = [d for d in data if d["job_id"] not in val_jobs]
    val = [d for d in data if d["job_id"] in val_jobs]
    for name, rows in (("train", train), ("val", val), ("prefs", prefs)):
        with open(out / f"{name}.jsonl", "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    stats = {"teacher_edits": len(positives), "your_reviews": len(gold), "skips": min(n_skip, len(skips)),
             "train": len(train), "val": len(val), "preference_pairs": len(prefs), "jobs": len(jobs)}
    say("Training set: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    say(f"Written to {out}")
    return stats


def _reviews(cfg, held_out):
    """Your 👍 / ✏️ / 👎 reviews from the app, as examples (needs the job's requirements and the bullet's item)."""
    conn = db.connect(cfg.db_path)
    try:
        master = json.loads(cfg.master_resume_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ids = tailor.bullet_ids(master)
    out = []
    for f in conn.execute("SELECT * FROM edit_feedback"):
        if f["job_id"] in held_out or f["edit_id"] not in ids:
            continue
        row = conn.execute("SELECT tailor_report FROM jobs WHERE id = ?", (f["job_id"],)).fetchone()
        reqs = (json.loads(row["tailor_report"] or "{}") if row else {}).get("requirements") or []
        if not reqs:
            continue
        sec, i, _ = ids[f["edit_id"]]
        rec = {"bullet": f["before"], "item": tailor._item_text(master, sec, i), "requirements": reqs,
               "job_id": f["job_id"], "source": "you"}
        if f["verdict"] in ("keep", "rewrite") and f["final"]:
            out.append(_example(rec, False, f["final"], "your reviewed edit"))
        elif f["verdict"] == "reject":
            out.append(_example(rec, True, f["before"], "you preferred the original"))
    return out
