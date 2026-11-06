"""Tailor a LaTeX (Overleaf) resume in place, then compile it.

The candidate's .tex is the master: its preamble, fonts, spacing and section
commands are never touched. Only the text inside \\item bullets, the summary
paragraph, and the order of skills and bullets change, written back into the
exact spans they came from.

What the AI sees is "editable text": LaTeX turned into plain words, with links
and inline formatting locked as tokens such as [[link:Emergency Routing]]. An
edit that drops or alters a token is rejected in code, so every \\href survives.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TOKEN = re.compile(r"\[\[(?:link|b|i|u|tt|m|x):[^\]]*\]\]")
_FORMAT = {"textbf": "b", "textit": "i", "emph": "i", "underline": "u", "texttt": "tt"}
_ESCAPES = {r"\%": "%", r"\$": "$", r"\&": "&", r"\#": "#", r"\_": "_", r"\{": "{", r"\}": "}"}


class LaTeXError(Exception):
    pass


# ---------------- small parsing helpers ----------------

def _brace_end(s, i):
    """Index just past the group starting at s[i] == '{'."""
    depth = 0
    for j in range(i, len(s)):
        c = s[j]
        if c == "\\":
            continue
        if c == "{" and (j == 0 or s[j - 1] != "\\"):
            depth += 1
        elif c == "}" and (j == 0 or s[j - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return j + 1
    raise LaTeXError("unbalanced braces")


def _args(s, i, n):
    """Read n brace arguments starting at or after s[i]. Returns (args, end)."""
    out = []
    for _ in range(n):
        while i < len(s) and s[i] in " \t\n":
            i += 1
        if i >= len(s) or s[i] != "{":
            raise LaTeXError("expected an argument")
        end = _brace_end(s, i)
        out.append(s[i + 1:end - 1])
        i = end
    return out, i


# ---------------- LaTeX <-> editable text ----------------

def to_editable(latex):
    """LaTeX fragment -> (editable text, {token: original latex})."""
    tokens, out, i = {}, [], 0
    while i < len(latex):
        c = latex[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)\*?", latex[i:])
            if m:
                name, after = m.group(1), i + m.end()
                try:
                    if name == "href":
                        (url, text), end = _args(latex, after, 2)
                        tok = f"[[link:{to_plain(text)}]]"
                    elif name in _FORMAT:
                        (text,), end = _args(latex, after, 1)
                        tok = f"[[{_FORMAT[name]}:{to_plain(text)}]]"
                    else:
                        tok = None
                except LaTeXError:
                    tok = None
                if tok:
                    tokens[tok] = latex[i:end]
                    out.append(tok)
                    i = end
                    continue
                # An unknown command: lock it as-is so it survives untouched.
                # Its display text comes from its arguments only (never re-parse
                # the command itself, which would recurse forever); layout
                # commands (\begin, \fontsize, \vspace ...) show nothing.
                end, shown = after, []
                while end < len(latex) and latex[end] == "{":
                    close = _brace_end(latex, end)
                    shown.append(to_plain(latex[end + 1:close - 1]))
                    end = close
                if name in _LAYOUT:
                    shown = []
                tok = f"[[x:{' '.join(s for s in shown if s) or name}]]"
                tokens[tok] = latex[i:end]
                out.append(tok)
                i = end
                continue
            two = latex[i:i + 2]
            if two in _ESCAPES:
                out.append(_ESCAPES[two])
                i += 2
                continue
            if two in ("\\;", "\\,", "\\!", "\\ ", "\\:", "\\\\"):
                out.append(" ")   # spacing and line breaks read as a space
                i += 2
                continue
            out.append(c)
            i += 1
        elif c == "$":
            end = latex.find("$", i + 1)
            if end == -1:
                out.append(c)
                i += 1
                continue
            tok = f"[[m:{_math_text(latex[i + 1:end])}]]"
            tokens[tok] = latex[i:end + 1]
            out.append(tok)
            i = end + 1
        elif c == "~":
            out.append(" ")
            i += 1
        elif latex.startswith("``", i) or latex.startswith("''", i):
            out.append('"')
            i += 2
        else:
            out.append(c)
            i += 1
    return " ".join("".join(out).split()), tokens


def _math_text(m):
    return {r"\sim": "~", r"\times": "x", r"\approx": "~", r"\geq": ">=", r"\leq": "<="}.get(m.strip(), m.strip())


def to_plain(latex):
    """LaTeX fragment -> readable plain text (for scoring and titles)."""
    text, _ = to_editable(latex) if "\\" in latex or "$" in latex else (" ".join(latex.split()), {})
    text = re.sub(r"\[\s*-?[\d.]+\s*(?:pt|em|ex|in|cm|mm)\s*\]", " ", untoken(text))  # \\[3pt] spacing
    return " ".join(text.replace("{", " ").replace("}", " ").split())


_LAYOUT = {"begin", "end", "fontsize", "selectfont", "vspace", "hspace", "hfill", "par", "centering",
           "small", "large", "Large", "LARGE", "huge", "Huge", "normalsize", "footnotesize", "scriptsize",
           "noindent", "newline", "linebreak", "medskip", "smallskip", "bigskip", "hrule", "quad", "qquad"}


def untoken(text):
    """Editable text -> plain text: [[link:Foo]] -> Foo; layout commands vanish."""
    def shown(m):
        kind, _, body = m.group(0)[2:-2].partition(":")
        return "" if kind == "x" and body in _LAYOUT else body
    return " ".join(TOKEN.sub(shown, text).split())


def from_editable(text, tokens):
    """Editable text -> LaTeX, escaping specials and restoring locked tokens."""
    parts, last = [], 0
    for m in TOKEN.finditer(text):
        parts.append(_escape(text[last:m.start()]))
        parts.append(tokens.get(m.group(0), _escape(untoken(m.group(0)))))
        last = m.end()
    parts.append(_escape(text[last:]))
    return "".join(parts)


def _escape(s):
    s = re.sub(r"(?<!\\)([%$&#_])", r"\\\1", s)
    return s.replace('"', "''") if s.count('"') % 2 else re.sub(r'"([^"]*)"', r"``\1''", s)


def token_problem(original, new):
    """Why an edit breaks a locked link/format token, or None."""
    a, b = sorted(TOKEN.findall(original)), sorted(TOKEN.findall(new))
    if a != b:
        lost = sorted(set(a) - set(b))
        return (f"changes or drops locked link/format {', '.join(lost)}" if lost
                else "adds a link/format token that wasn't in the original")
    return None


# ---------------- structure ----------------

SECTION = re.compile(r"\\([A-Za-z]*section)\*?\s*\{")
ENV_SECTION = re.compile(r"\\begin\{([A-Za-z]*section[A-Za-z]*)\}\s*\{")


def _sections(tex):
    """[(name, heading_start, body_start, body_end)] for every section command."""
    found = []
    for m in SECTION.finditer(tex):
        try:
            (name,), body_start = _args(tex, m.end() - 1, 1)
        except LaTeXError:
            continue
        found.append([to_plain(name).upper(), m.start(), body_start, None])
    for m in ENV_SECTION.finditer(tex):
        try:
            (name,), body_start = _args(tex, m.end() - 1, 1)
        except LaTeXError:
            continue
        close = tex.find(rf"\end{{{m.group(1)}}}", body_start)
        found.append([to_plain(name).upper(), m.start(), body_start, close if close != -1 else None])
    found.sort(key=lambda f: f[1])
    end_doc = tex.find(r"\end{document}")
    end_doc = end_doc if end_doc != -1 else len(tex)
    out = []
    for k, (name, start, body, close) in enumerate(found):
        stop = close if close is not None else (found[k + 1][1] if k + 1 < len(found) else end_doc)
        out.append((name, start, body, stop))
    return out


def _items(body):
    """Bullets inside one itemize body: [(start, end, latex)] relative to body."""
    starts = [m.end() for m in re.finditer(r"\\item\b", body)]
    out = []
    for k, s in enumerate(starts):
        e = starts[k + 1] - len(r"\item") if k + 1 < len(starts) else len(body)
        # trim whitespace on both sides so spans hold just the bullet text
        while s < e and body[s] in " \t\n":
            s += 1
        while e > s and body[e - 1] in " \t\n":
            e -= 1
        out.append((s, e, body[s:e]))
    return out


def _title_dates(header):
    """Item heading -> (title, dates, end of heading). Uses the first two-argument
    command (e.g. \\role{title}{dates}), else splits plain text at \\hfill."""
    for m in re.finditer(r"\\[A-Za-z]+\s*(?=\{)", header):
        try:
            (a, b), end = _args(header, m.end(), 2)
            return to_plain(a), to_plain(b), end
        except LaTeXError:
            continue
    left, _, right = header.partition(r"\hfill")
    return to_plain(left), to_plain(right), len(header)


_LEAD = re.compile(r"^(?:\s|\\vspace\*?\{[^}]*\}|\\medskip|\\smallskip|\\bigskip|\\gap\b)*")
_TITLE = re.compile(r"\\textbf\{([^{}]+)\}")


def parse(tex):
    """Read a LaTeX resume. Returns (master dict, layout).

    master matches MasterResume (bullets as editable text with locked tokens);
    layout records the source spans needed to write edits back.
    """
    secs = _sections(tex)
    if not secs:
        raise LaTeXError("no \\section-style headings found")
    begin = tex.find(r"\begin{document}")
    header_start = (begin + len(r"\begin{document}")) if begin != -1 else 0
    header = tex[header_start:secs[0][1]]
    master = {"contact": _contact(header), "headline": "", "summary": "", "education": [], "skills": [],
              "experience": [], "projects": [], "publications": []}
    layout = {"summary": None, "headline": None, "skills": [], "blocks": {"experience": None, "projects": None}}
    title = _TITLE.search(header)
    if title and "," in title.group(1):
        # "Name, Role": the role part follows each job (see tailor.role_headline).
        comma = title.group(1).index(",") + 1
        spaces = len(title.group(1)[comma:]) - len(title.group(1)[comma:].lstrip())
        master["headline"] = to_plain(title.group(1)[comma:])
        layout["headline"] = (header_start + title.start(1) + comma + spaces, header_start + title.end(1))

    for name, _, body_start, body_end in secs:
        body = tex[body_start:body_end]
        if any(k in name for k in ("SUMMARY", "PROFILE", "OBJECTIVE", "ABOUT")):
            s = len(body) - len(body.lstrip())
            e = len(body.rstrip())
            master["summary"], _ = to_editable(body[s:e])
            layout["summary"] = (body_start + s, body_start + e)
        elif "SKILL" in name:
            heads = list(re.finditer(r"\\textbf\{([^{}]*?):?\}\s*:?\s*", body))
            for k, m in enumerate(heads):
                chunk = body[m.end():heads[k + 1].start() if k + 1 < len(heads) else len(body)]
                # What separates this group from the next stays in place.
                sep = re.search(r"(?:\s|\\\\(?:\[[^\]]*\])?|\\q?quad\b)*$", chunk)
                items_latex = [" ".join(x.split()) for x in _split_top(chunk[:sep.start()]) if x.strip()]
                master["skills"].append({"category": to_plain(m.group(1)).rstrip(":"),
                                         "items": [to_plain(x) for x in items_latex]})
                layout["skills"].append({"span": (body_start + m.start(), body_start + m.end() + sep.start()),
                                         "category_latex": m.group(1), "items_latex": items_latex})
        elif "EXPERIENCE" in name and "PROJECT" not in name or "EMPLOYMENT" in name or "WORK" in name:
            master["experience"], layout["blocks"]["experience"] = _blocks(tex, body_start, body_end, "experience")
        elif "PROJECT" in name:
            master["projects"], layout["blocks"]["projects"] = _blocks(tex, body_start, body_end, "projects")
        elif "EDUCATION" in name:
            lines = [to_plain(l) for l in re.split(r"\\\\|\n\s*\n", body) if to_plain(l).strip()]
            master["education"] = [{"school": lines[0] if lines else "", "degree": "", "dates": "",
                                    "details": lines[1:]}]
        elif "PUBLICATION" in name:
            master["publications"] = [to_plain(p) for p in re.split(r"\n\s*\n", body) if to_plain(p).strip()]
    return master, layout


def _split_top(s):
    """Split on commas that aren't inside braces."""
    parts, depth, cur = [], 0, []
    for c in s:
        depth += c == "{"
        depth -= c == "}"
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


def _blocks(tex, body_start, body_end, kind):
    """Items (roles or projects) in a section: one heading plus one itemize each."""
    body = tex[body_start:body_end]
    items, blocks, cursor = [], [], 0
    for m in re.finditer(r"\\begin\{itemize\}(\[[^\]]*\])?", body):
        end_m = body.find(r"\end{itemize}", m.end())
        if end_m == -1:
            break
        header = body[cursor:m.start()]
        lead = _LEAD.match(header).end()
        title, dates, heading_end = _title_dates(header)
        bullets, lead_span, lead_latex = [], None, None
        para = header[heading_end:]
        if to_plain(para).strip():
            s = heading_end + len(para) - len(para.lstrip())
            e = heading_end + len(para.rstrip())
            lead_span, lead_latex = (body_start + cursor + s, body_start + cursor + e), header[s:e]
            bullets.append(to_editable(lead_latex)[0])
        for s, e, latex in _items(body[m.end():end_m]):
            text, _ = to_editable(latex)
            bullets.append(text)
        if kind == "experience":
            items.append({"title": title, "company": "", "dates": dates, "bullets": bullets})
        else:
            items.append({"name": title, "dates": dates, "bullets": bullets})
        block_end = end_m + len(r"\end{itemize}")
        blocks.append({"start": body_start + cursor, "lead_end": body_start + cursor + lead,
                       "items_start": body_start + m.end(), "items_end": body_start + end_m,
                       "end": body_start + block_end, "lead_span": lead_span, "lead_latex": lead_latex,
                       "bullets_latex": [latex for _, _, latex in _items(body[m.end():end_m])]})
        cursor = block_end
    return items, blocks


def _contact(header):
    links = []
    for m in re.finditer(r"\\href\s*\{", header):
        try:
            (url, text), _ = _args(header, m.end() - 1, 2)
        except LaTeXError:
            continue
        if not url.startswith("mailto:"):
            links.append({"label": to_plain(text), "url": url})
    header = re.sub(r"(?<!\\)%.*", "", header)
    names = _TITLE.findall(header)
    plain = to_plain(_TITLE.sub("", header, count=1).replace("$|$", "|"))
    name = to_plain(names[0]).split(",")[0].strip() if names else plain.split("|")[0].strip()
    parts = [p.strip() for p in re.split(r"\|", plain)]
    email = next((p for p in parts if "@" in p), "")
    phone = next((p for p in parts if re.search(r"\d{3}.*\d{4}", p) and "@" not in p), "")
    location = next((p.replace(name, "").strip() for p in parts if "," in p and "@" not in p
                     and not re.search(r"\d{3}.*\d{4}", p)), "")
    return {"name": name, "location": location, "phone": phone, "email": email, "links": links}


# ---------------- writing edits back ----------------

def render(tex, master, resume, changed=frozenset()):
    """Write a tailored resume back into the original LaTeX.

    `resume` has the same shape as `master` (from tailor.apply_plan). Bullets
    that didn't change keep their original LaTeX byte for byte. `changed` holds
    editable texts to bold for the review copy.
    """
    _, layout = parse(tex)
    edits = []   # (start, end, new_latex), non-overlapping

    def latex_for(text, originals):
        orig = originals.get(text)
        body = orig if orig is not None else from_editable(text, _token_map(originals))
        return rf"\textbf{{{body}}}" if text in changed else body

    if layout["summary"] and resume["summary"] != master["summary"] or (layout["summary"] and resume["summary"] in changed):
        s, e = layout["summary"]
        new = from_editable(resume["summary"], to_editable(tex[s:e])[1])
        edits.append((s, e, rf"\textbf{{{new}}}" if resume["summary"] in changed else new))

    if layout["headline"] and resume.get("headline") and resume["headline"] != master.get("headline"):
        edits.append((*layout["headline"], _escape(resume["headline"])))

    if layout["skills"] and [g["category"] for g in resume["skills"]] + [i for g in resume["skills"] for i in g["items"]] \
            != [g["category"] for g in master["skills"]] + [i for g in master["skills"] for i in g["items"]]:
        by_cat = {g["category"]: (g, layout["skills"][k]) for k, g in enumerate(master["skills"])}
        for slot, group in zip(layout["skills"], resume["skills"]):
            mg, lay = by_cat[group["category"]]
            latex_items = dict(zip(mg["items"], lay["items_latex"]))
            line = rf"\textbf{{{lay['category_latex']}:}} " + ", ".join(latex_items[i] for i in group["items"])
            edits.append((*slot["span"], line))

    for kind, key in (("experience", "title"), ("projects", "name")):
        blocks = layout["blocks"][kind]
        if not blocks:
            continue
        index = {item[key]: k for k, item in enumerate(master[kind])}
        new_blocks = []
        for item in resume[kind]:
            k = index[item[key]]
            b = blocks[k]
            originals = {to_editable(lx)[0]: lx for lx in b["bullets_latex"]}
            if b["lead_latex"]:
                originals[to_editable(b["lead_latex"])[0]] = b["lead_latex"]
            same = item["bullets"] == master[kind][k]["bullets"] and not changed & set(item["bullets"])
            head, texts = tex[b["lead_end"]:b["items_start"]], item["bullets"]
            if b["lead_span"]:
                # The first bullet in the tailored order is written as the paragraph.
                s, e = b["lead_span"]
                lead = tex[s:e] if same else latex_for(texts[0], originals)
                head, texts = tex[b["lead_end"]:s] + lead + tex[e:b["items_start"]], texts[1:]
            if same:
                inner = tex[b["items_start"]:b["items_end"]]
            else:
                inner = "\n" + "\n".join(r"\item " + latex_for(t, originals) for t in texts) + "\n"
            new_blocks.append((head, inner, tex[b["items_end"]:b["end"]]))
        # Each slot keeps its own leading spacing (\vspace); content moves between slots.
        out = "".join(tex[b["start"]:b["lead_end"]] + h + inner + tail
                      for b, (h, inner, tail) in zip(blocks, new_blocks))
        if out != tex[blocks[0]["start"]:blocks[-1]["end"]]:
            edits.append((blocks[0]["start"], blocks[-1]["end"], out))

    for s, e, new in sorted(edits, reverse=True):
        tex = tex[:s] + new + tex[e:]
    return tex


def _token_map(originals):
    tokens = {}
    for lx in originals.values():
        tokens.update(to_editable(lx)[1])
    return tokens


# ---------------- compiling ----------------

def engine_for(tex):
    """XeLaTeX for fontspec/unicode-math documents (as on Overleaf), else pdfLaTeX."""
    return "xelatex" if re.search(r"\\usepackage(\[[^\]]*\])?\{(fontspec|unicode-math)\}", tex) else "pdflatex"


def compile_pdf(tex, out_pdf, timeout=240):
    """Compile LaTeX source to out_pdf. Returns the page count."""
    engine = engine_for(tex)
    if not shutil.which(engine):
        raise LaTeXError(f"{engine} not found; install MiKTeX or TeX Live")
    out_pdf = Path(out_pdf)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.tex"
        src.write_text(tex, encoding="utf-8")
        try:
            proc = subprocess.run([engine, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                                  cwd=tmp, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise LaTeXError(f"{engine} timed out after {timeout}s") from exc
        pdf = Path(tmp) / "resume.pdf"
        if proc.returncode != 0 or not pdf.exists():
            errors = [l for l in proc.stdout.splitlines() if l.startswith("!")][:3]
            raise LaTeXError(f"{engine} failed: {' | '.join(errors) or proc.stdout[-300:]}")
        m = re.search(r"Output written on .*?\((\d+) pages?", proc.stdout)
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, out_pdf)
        return int(m.group(1)) if m else 1
