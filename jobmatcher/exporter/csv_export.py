from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List

from jobmatcher.models.job import RankedJob


DEFAULT_CSV_FIELDS = [
    "id",
    "title",
    "company",
    "location",
    "department",
    "team",
    "employment_type",
    "source",
    "url",
    "match_score",
    "explanation",
    "matched_skills",
    "missing_skills",
    "title_score",
    "skills_score",
    "experience_score",
    "keyword_score",
]


def export_to_csv(ranked_jobs: List[RankedJob], output_path: str | Path, fields: List[str] | None = None) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fields = fields or DEFAULT_CSV_FIELDS

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for rj in ranked_jobs:
            writer.writerow(rj.to_dict())

    return output_path


def export_to_json(ranked_jobs: List[RankedJob], output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = [rj.to_dict() for rj in ranked_jobs]

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return output_path
