"""LaTeX resume support: read, edit in place, keep links, compile."""
import copy
import shutil

import pytest

from copilot import latex
from copilot.tailor import check_edit

TEX = r"""\documentclass[10pt]{article}
\usepackage[hidelinks]{hyperref}
\newcommand{\ressection}[1]{\textbf{#1}\par}
\newcommand{\role}[2]{\textbf{#1}\hfill #2\par}
\begin{document}
\begin{center}
{\textbf{JANE DOE}}\\
Austin, TX \;|\; +1 (512) 555-0100 \;|\; \href{mailto:jane@x.com}{jane@x.com} \;|\; \href{https://github.com/jane}{GitHub}
\end{center}

\ressection{SUMMARY}
ML engineer who ships reliable systems.

\ressection{TECHNICAL SKILLS}
\textbf{Languages:} Python, C++, SQL

\textbf{Tools:} Docker, Logging \& Monitoring

\ressection{EXPERIENCE}
\role{Acme - Engineer}{2021 -- 2023}
\begin{itemize}
\item Built \href{https://github.com/jane/router}{Router}, cutting latency 20\% for 3 teams.
\item Fixed a billing defect worth \$500K annually.
\end{itemize}

\ressection{PROJECTS}
\role{Alpha}{2024}
\begin{itemize}
\item Trained a model on 10K images.
\end{itemize}

\vspace{2pt}
\role{Beta}{2025}
\begin{itemize}
\item Wrote a parser in Rust.
\end{itemize}
\end{document}
"""


def test_parse_reads_structure_and_locks_links():
    m, _ = latex.parse(TEX)
    assert m["contact"]["name"] == "JANE DOE" and m["contact"]["email"] == "jane@x.com"
    assert m["contact"]["links"] == [{"label": "GitHub", "url": "https://github.com/jane"}]
    assert m["summary"] == "ML engineer who ships reliable systems."
    assert m["skills"][1] == {"category": "Tools", "items": ["Docker", "Logging & Monitoring"]}
    assert m["experience"][0]["title"] == "Acme - Engineer" and m["experience"][0]["dates"] == "2021 -- 2023"
    assert m["experience"][0]["bullets"][0] == "Built [[link:Router]], cutting latency 20% for 3 teams."
    assert [p["name"] for p in m["projects"]] == ["Alpha", "Beta"]


def test_unchanged_render_is_byte_identical():
    m, _ = latex.parse(TEX)
    assert latex.render(TEX, m, copy.deepcopy(m)) == TEX


def test_edit_keeps_link_and_escapes_specials():
    m, _ = latex.parse(TEX)
    r = copy.deepcopy(m)
    r["experience"][0]["bullets"][0] = "Built [[link:Router]] & cut latency 20% for 3 teams."
    r["summary"] = "ML engineer; 100% reliable & tested."
    out = latex.render(TEX, m, r)
    assert r"\href{https://github.com/jane/router}{Router} \& cut latency 20\% for 3 teams." in out
    assert r"ML engineer; 100\% reliable \& tested." in out
    assert out.split(r"\begin{document}")[0] == TEX.split(r"\begin{document}")[0]   # preamble untouched


def test_reorder_moves_content_but_slots_keep_their_spacing():
    m, _ = latex.parse(TEX)
    r = copy.deepcopy(m)
    r["projects"] = [r["projects"][1], r["projects"][0]]
    r["skills"][0]["items"] = ["SQL", "Python", "C++"]
    out = latex.render(TEX, m, r)
    assert out.index(r"\role{Beta}") < out.index(r"\role{Alpha}")
    assert out.count(r"\vspace{2pt}") == 1 and out.index(r"\vspace{2pt}") > out.index(r"\role{Beta}")
    assert r"\textbf{Languages:} SQL, Python, C++" in out


def test_review_copy_bolds_changed_bullets_only():
    m, _ = latex.parse(TEX)
    r = copy.deepcopy(m)
    r["projects"][0]["bullets"][0] = "Trained a vision model on 10K images."
    out = latex.render(TEX, m, r, changed={"Trained a vision model on 10K images."})
    assert r"\textbf{Trained a vision model on 10K images.}" in out
    assert r"\item Fixed a billing defect worth \$500K annually." in out


def test_edit_that_drops_or_alters_a_link_is_rejected():
    original = "Built [[link:Router]], cutting latency 20% for 3 teams."
    item = "Acme - Engineer\n" + original
    assert "link" in check_edit(original, "Built a router, cutting latency 20% for 3 teams.", item)
    assert "link" in check_edit(original, "Built [[link:Fast Router]], cutting latency 20% for 3 teams.", item)


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="no LaTeX engine installed")
def test_compiles_to_pdf(tmp_path):
    m, _ = latex.parse(TEX)
    r = copy.deepcopy(m)
    r["summary"] = "ML engineer & builder; 100% tested."
    pages = latex.compile_pdf(latex.render(TEX, m, r), tmp_path / "out.pdf")
    assert pages == 1 and (tmp_path / "out.pdf").stat().st_size > 1000
