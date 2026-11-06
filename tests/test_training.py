"""Training-set assembly: what becomes an example, and what never does."""
import json

from copilot import bullets, training
from copilot.config import Config

REQS = [{"need": "LLM apps", "keywords": ["LLM", "RAG"], "evidence": ["E1.1", "E1.2", "E1.3"]}]


def _rec(job, bid, verdict, text, reason=""):
    return {"job_id": job, "company": "Acme", "title": "MLE", "bullet_id": bid, "bullet": f"Built thing {bid} for 40% gain",
            "item": "Role at Acme", "requirements": REQS, "model": "teacher", "mode": "teacher", "at": "x",
            "verdict": verdict, "text": text, "reason": reason}


def test_build(tmp_path, monkeypatch):
    log = tmp_path / "training" / "bullets.jsonl"
    log.parent.mkdir()
    monkeypatch.setattr(bullets, "LOG", log)
    cfg = Config(resume=tmp_path / "r.pdf", companies=tmp_path / "c.yaml", data_dir=tmp_path)
    (tmp_path / "eval_jobs.json").write_text(json.dumps(["eval-job"]))
    rows = []
    for j in range(8):
        job = f"job{j}"
        rows += [_rec(job, "E1.1", "accepted", "Built RAG thing E1.1 for 40% gain", "surfaces RAG"),
                 _rec(job, "E1.2", "untouched", "Built thing E1.2 for 40% gain"),
                 _rec(job, "E1.3", "rejected", "Built LLM thing", "lost metric 40%")]
    rows.append(_rec("eval-job", "E1.1", "accepted", "SHOULD NOT APPEAR"))
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    stats = training.build(cfg, say=lambda *_: None)
    out = tmp_path / "training" / "sft"
    data = [json.loads(l) for f in ("train.jsonl", "val.jsonl") for l in (out / f).read_text().splitlines()]
    assert all(d["job_id"] != "eval-job" for d in data)
    answers = [json.loads(d["messages"][2]["content"]) for d in data]
    assert stats["teacher_edits"] == 8 and stats["preference_pairs"] == 8
    assert any(a["skip"] for a in answers) and any(not a["skip"] for a in answers)
    assert not any("LLM thing" == a["text"] for a in answers)            # rejected edits never trained on
    assert {d["job_id"] for d in data if d in [*map(json.loads, (out / "val.jsonl").read_text().splitlines())]} \
        .isdisjoint({d["job_id"] for d in map(json.loads, (out / "train.jsonl").read_text().splitlines())})
    user = data[0]["messages"][1]["content"]
    assert "JOB REQUIREMENTS" in user and "TERMS TO WORK IN, IF TRUE: LLM, RAG" in user
