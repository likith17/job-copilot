"""Cover letters from your own letter template (Autofill info -> Your story).

When your story has [Role] and [Company] placeholders, it is your cover letter:
every word of it is kept. Only three things change per job: the role, the
company, and the one bracketed company-specific sentence, which the model
writes from the job posting. That sentence is fact-checked against your
resume and story; if it claims anything they don't support, it is left out
rather than sent. One short model call per letter.
"""
import re

from pydantic import BaseModel

from copilot import llm

ROLE, COMPANY = "[Role]", "[Company]"
SLOT = re.compile(r"\[[^\]]*company[- ]specific[^\]]*\]", re.I)
SIGN_OFF = re.compile(r"\n\s*(?:sincerely|best regards|kind regards|regards|best|thank you|thanks)[,!.]?\s*\n?.*$",
                      re.I | re.S)
TITLE_NOISE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]|\s+[-–—|]\s+.*$")


class CompanyLine(BaseModel):
    sentence: str
    connection: str     # the exact words of the sentence about the candidate's own background


SYSTEM = """You write ONE sentence for a job seeker's cover letter. Everything else in the letter is theirs.

The sentence says what interests them about this company's product, mission, or engineering problem, and connects it
to one concrete thing from their background below. Rules:
- First person, 25 to 45 words, plain and specific.
- Say only what the job posting says about the company and the work. Do not add facts about the company.
- Name only experience the background states, in its own words. Never add experience, tools, or numbers.
- No flattery or filler: never use passionate, thrilled, excited, dream, innovative, cutting-edge, world-class.
Also return `connection`: copied exactly from your sentence, the words that describe the candidate's own background.

THE CANDIDATE'S BACKGROUND:
"""


def is_template(text):
    return bool(text) and COMPANY in text


def role_name(title):
    """The role as it reads in a sentence: "Software Engineer, New Grad (2027) - Remote" -> "Software Engineer, New Grad"."""
    return TITLE_NOISE.sub("", title or "").strip(" ,-") or (title or "").strip()


def fill(template, job, sentence="", name=""):
    """The template with this job's role, company and company sentence. With no
    sentence, its paragraph is left out."""
    text = template.replace(ROLE, role_name(job.get("title"))).replace(COMPANY, job.get("company") or "your company")
    if sentence:
        text = SLOT.sub(lambda _: sentence.strip(), text)
    else:
        text = SLOT.sub("", text)
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip()
    if not re.match(r"\s*(?:dear|hello|hi|to whom)\b", text, re.I):
        text = f"Dear {job.get('company') or 'Hiring'} Hiring Team,\n\n" + text
    if name and not SIGN_OFF.search(text[-200:]):
        text += f"\n\nSincerely,\n{name}"
    return text


def for_answers(story, job=None):
    """Your story as background for answering form questions: placeholders filled
    for this job, or taken out when there is no job."""
    if not is_template(story):
        return story
    if job:
        return SLOT.sub("", story.replace(ROLE, role_name(job.get("title"))).replace(COMPANY, job.get("company", "")))
    return SLOT.sub("", story.replace(f" the {ROLE} position at {COMPANY}", " a new team")
                    .replace(ROLE, "this role").replace(COMPANY, "this company"))


def company_sentence(template, job, settings):
    from copilot.grade import trim_posting
    background = SLOT.sub("", template).replace(ROLE, "the role").replace(COMPANY, "the company")
    user = f"COMPANY: {job['company']}\nROLE: {job['title']}\n\nJOB POSTING:\n{trim_posting(job.get('description'))}"
    out, _ = llm.parse(settings, [{"type": "text", "text": SYSTEM + background}], user, CompanyLine, max_tokens=800)
    s = re.sub(r"\s+", " ", out.sentence).strip().strip('"')
    connection = re.sub(r"\s+", " ", out.connection).strip().strip('"')
    if not 8 <= len(s.split()) <= 70:
        return "", ""
    return s, (connection if connection and connection in s else s)


_WORD = re.compile(r"[a-z][a-z0-9+#.-]{3,}")


def _about_company_is_from_posting(sentence, connection, job):
    """The part about the company may use only the posting's own words (names and
    content words; small words don't count)."""
    rest = sentence.replace(connection, " ").lower()
    source = (job.get("description") or "").lower() + " " + (job.get("company") or "").lower()
    words = [w.strip(".-") for w in _WORD.findall(rest)]
    words = [w for w in words if w not in _COMMON]
    return not words or sum(w[:6] in source for w in words) / len(words) >= 0.7


_COMMON = set("""what draws interests interest about with that this their they them work working company team teams
role build building builds helps help which where would could should into from have having been your also makes
make making especially particularly focus focused because through connects connect connecting given like mission
product products problem problems engineering engineers people users customers clients scale""".split())


def from_template(template, job, master, settings, check, trusted):
    """(letter, notes). notes say what was left out and why; never blocking."""
    from copilot import tailor
    notes = []
    try:
        sentence, connection = company_sentence(template, job, settings)
    except llm.LLMUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - the letter still stands without this sentence
        sentence, connection, notes = "", "", [f"company sentence not written ({str(exc)[:80]})"]
    if sentence and not _about_company_is_from_posting(sentence, connection, job):
        notes.append("company sentence left out: it says things about the company the posting doesn't")
        sentence = ""
    if sentence:
        # Only the words about you are claims to check; the rest comes from the posting.
        flags = tailor.audit_claims(master, "", connection, check, trusted)
        if flags:
            notes.append("company sentence left out: " + "; ".join(flags)[:200])
            sentence = ""
    name = (master.get("contact") or {}).get("name", "")
    return fill(template, job, sentence, name), notes
