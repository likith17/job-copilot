import tempfile
from pathlib import Path

from jobmatcher.models.job import Job, RankedJob
from jobmatcher.exporter.csv_export import export_to_csv, export_to_json


def make_ranked():
    job = Job(id="1", title="Software Engineer", company="Acme", location="Remote", url="https://example.com", source="greenhouse")
    return RankedJob(job=job, score=85.5, explanation="Great match", matched_skills=["Python"], missing_skills=["Go"])


def test_export_csv():
    ranked = [make_ranked()]
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        path = f.name

    try:
        out = export_to_csv(ranked, path)
        assert Path(out).exists()
        content = Path(out).read_text()
        assert "Software Engineer" in content
        assert "Acme" in content
        assert "85.5" in content
    finally:
        Path(path).unlink(missing_ok=True)


def test_export_json():
    ranked = [make_ranked()]
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name

    try:
        out = export_to_json(ranked, path)
        assert Path(out).exists()
        content = Path(out).read_text()
        assert "Software Engineer" in content
        assert "Acme" in content
    finally:
        Path(path).unlink(missing_ok=True)


def test_export_empty():
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        path = f.name

    try:
        out = export_to_csv([], path)
        assert Path(out).exists()
        # Should still have header
        content = Path(out).read_text()
        assert "title" in content.lower()
    finally:
        Path(path).unlink(missing_ok=True)
