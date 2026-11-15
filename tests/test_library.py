"""The resume library: which family a job belongs to, and which resume that family uses."""
import pytest

from copilot import db, inputs, library
from copilot.config import Config


@pytest.mark.parametrize("title,family", [
    ("Software Engineer II", "software"),
    ("Senior Backend Engineer", "backend"),
    ("Full Stack Engineer, Growth", "fullstack"),
    ("AI Engineer", "ai"),
    ("Applied AI Engineer, Agents", "ai"),
    ("Machine Learning Engineer, AI Platform", "ml"),      # the family named first wins
    ("Data Engineer", "data_engineer"),
    ("Senior Data Scientist", "data_scientist"),
    ("Forward Deployed Engineer", "fde"),
    ("Forward Deployed AI Engineer", "fde"),
    ("Research Engineer, Machine Learning", "research"),
    ("Member of Technical Staff", "software"),
])
def test_title_decides_the_family(title, family):
    assert library.classify(title) == family


def test_a_vague_title_uses_the_posting_only_when_it_is_clear():
    ml = "You will train models in PyTorch. Machine learning at scale. Model training pipelines and MLOps."
    assert library.classify("Software Engineer", ml) == "ml"
    mixed = "PyTorch and React. Machine learning and TypeScript. Model training and front-end work."
    assert library.classify("Software Engineer", mixed) == "software"


@pytest.fixture
def setup(tmp_path):
    folder = tmp_path / "inputs" / "resume"
    folder.mkdir(parents=True)
    for name in ("default.tex", "backend.tex", "ml.tex"):
        (folder / name).write_text(f"resume {name}", encoding="utf-8")
    cfg = Config(resume=folder / "default.tex", companies=tmp_path / "c.yaml", data_dir=tmp_path)
    return cfg, db.connect(cfg.db_path), folder


def test_a_job_gets_its_familys_resume_and_others_the_default(setup):
    cfg, conn, folder = setup
    library.assign(conn, "backend.tex", "Backend", ["backend"])
    lib = library.Resolver(cfg, conn)
    assert lib.for_job({"title": "Backend Engineer", "description": ""}) == folder / "backend.tex"
    assert lib.for_job({"title": "Data Scientist", "description": ""}) == folder / "default.tex"
    assert lib.label(folder / "backend.tex") == "Backend" and lib.text(folder / "backend.tex") == "resume backend.tex"


def test_a_family_belongs_to_one_resume(setup):
    cfg, conn, _ = setup
    library.assign(conn, "backend.tex", "Backend", ["backend", "ml"])
    library.assign(conn, "ml.tex", "ML", ["ml", "not-a-family"])
    lib = library.entries(conn)
    assert lib["backend.tex"]["roles"] == ["backend"] and lib["ml.tex"]["roles"] == ["ml"]


def test_removed_or_missing_resumes_fall_back_to_the_default(setup):
    cfg, conn, folder = setup
    library.assign(conn, "backend.tex", "Backend", ["backend"])
    library.assign(conn, "ml.tex", "ML", ["ml"])
    library.remove(conn, "backend.tex")
    (folder / "ml.tex").unlink()
    lib = library.Resolver(cfg, conn)
    assert lib.pick("backend") == lib.pick("ml") == folder / "default.tex"
    assert (folder / "backend.tex").exists()                # removing an entry never deletes the file


def test_scores_are_redone_only_when_a_used_resume_changes(setup):
    cfg, conn, folder = setup
    before = library.Resolver(cfg, conn).signature()
    library.assign(conn, "backend.tex", "Backend", [])       # in the library, used for nothing
    assert library.Resolver(cfg, conn).signature() == before
    library.assign(conn, "backend.tex", roles=["backend"])
    changed = library.Resolver(cfg, conn).signature()
    assert changed != before
    (folder / "backend.tex").write_text("edited", encoding="utf-8")
    assert library.Resolver(cfg, conn).signature() != changed


def test_adding_a_library_resume_keeps_the_default(setup):
    cfg, conn, folder = setup
    inputs.set_current(cfg, "resume", folder / "default.tex")
    added = inputs.save_upload(cfg, "resume", "new.tex", b"new resume", force=True, make_default=False)
    assert added.exists() and inputs.current(cfg, "resume") == folder / "default.tex"
