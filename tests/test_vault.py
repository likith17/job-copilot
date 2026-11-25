"""What the extension keeps for filling pages with the app closed."""
import base64

from copilot import applyserver, db, latex, library
from copilot.config import Config


def test_vault_has_profile_answers_and_resume_files(tmp_path, monkeypatch):
    folder = tmp_path / "inputs" / "resume"
    folder.mkdir(parents=True)
    (folder / "default.tex").write_text("default", encoding="utf-8")
    (folder / "backend.tex").write_text("backend", encoding="utf-8")
    (folder / "old.pdf").write_bytes(b"%PDF old")          # an upload that is not in the library
    cfg = Config(resume=folder / "default.tex", companies=tmp_path / "c", data_dir=tmp_path)
    conn = db.connect(cfg.db_path)
    library.assign(conn, "default.tex", "AI Engineer", ["ai"])
    library.assign(conn, "backend.tex", "Backend Engineer", ["backend"])
    db.save_answer(conn, "How many years of Python?", "3")
    db.save_answer(conn, "Why do you want to work at Acme?", "Because of Acme.")     # about one company
    compiled = []

    def fake_compile(tex, out):
        compiled.append(tex)
        out.write_bytes(b"%PDF " + tex.encode())
        return 2
    monkeypatch.setattr(latex, "compile_pdf", fake_compile)
    monkeypatch.setattr(applyserver.apply, "load_profile", lambda cfg: {"name": "Pat Example", "email": "p@x.io"})

    vault = applyserver.r_vault(cfg, conn, {}, {})
    files = {f["label"]: f for f in vault["files"]}
    assert list(files) == ["AI Engineer (default)", "Backend Engineer"]
    assert files["AI Engineer (default)"]["name"] == "Pat_Example_Resume.pdf"
    assert files["Backend Engineer"]["name"] == "Pat_Example_Resume_Backend_Engineer.pdf"
    assert base64.b64decode(files["Backend Engineer"]["base64"]) == b"%PDF backend"
    assert [a["question"] for a in vault["answers"]] == ["How many years of Python?"]
    assert vault["profile"]["email"] == "p@x.io"

    applyserver.r_vault(cfg, conn, {}, {})
    assert len(compiled) == 2                 # compiled once per resume version, then cached
