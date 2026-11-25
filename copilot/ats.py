"""Matching report: how a resume lines up with one posting, in code (no model call).

It reads the posting's required and preferred parts, finds the technologies and
practices each names, and checks which of them the resume also names. The
"alignment" number is keyword coverage only: an estimate of what a keyword
screen would see. It is not a score from any real applicant tracking system,
and it says nothing about how well the experience itself fits (that is the AI
fit grade).

Suggestions never invent experience: a missing keyword is either something
another of your resumes already says (so you can bring that line over) or
something to add only if it is true.
"""
import re

# term -> pattern (case-insensitive unless it starts with (?-i:)
_PLAIN = """python java javascript typescript rust scala kotlin swift ruby php sql nosql bash
react angular vue svelte next.js node.js express django flask fastapi spring rails graphql grpc rest
html css tailwind redux webpack
pytorch tensorflow keras jax scikit-learn xgboost lightgbm numpy pandas scipy opencv
langchain langgraph llamaindex hugging face transformers rag llm embeddings fine-tuning prompt engineering
vector database pinecone faiss chroma weaviate mlflow kubeflow sagemaker vertex ai bedrock ray triton onnx tensorrt cuda
reinforcement learning computer vision nlp deep learning machine learning generative ai agents diffusion
recommendation systems time series forecasting a/b testing statistics causal inference experimentation
spark hadoop kafka flink airflow dbt snowflake bigquery redshift databricks delta lake etl data pipelines
data warehouse data modeling tableau power bi looker
postgresql mysql mongodb redis elasticsearch dynamodb cassandra sqlite
aws gcp azure docker kubernetes terraform ansible jenkins github actions ci/cd linux git
microservices distributed systems system design api design observability prometheus grafana datadog
unit testing pytest jest selenium playwright agile scrum
ros slam sensor fusion embedded simulation""".split("\n")
_PHRASES = ["hugging face", "fine-tuning", "prompt engineering", "vector database", "vertex ai", "reinforcement learning",
            "computer vision", "deep learning", "machine learning", "generative ai", "recommendation systems",
            "time series", "a/b testing", "causal inference", "delta lake", "data pipelines", "data warehouse",
            "data modeling", "power bi", "github actions", "distributed systems", "system design", "api design",
            "unit testing", "sensor fusion"]
ALIASES = {
    "c++": r"c\+\+", "c#": r"c#", ".net": r"\.net\b", "go": r"(?-i:\bGolang\b|\bGo\b(?=[,/)]| (?:and|or|programming|services)))",
    "spring": r"(?-i:\bSpring(?: Boot| Framework|(?=[,/)])))", "rails": r"ruby on rails|(?-i:\bRails\b)",
    "node.js": r"\bnode(?:\.?js)?\b", "next.js": r"\bnext\.?js\b", "postgresql": r"\bpostgres(?:ql)?\b",
    "kubernetes": r"\bkubernetes\b|\bk8s\b", "scikit-learn": r"scikit[- ]?learn|\bsklearn\b",
    "llm": r"\bllms?\b|large language models?", "rag": r"\bRAG\b|retrieval[- ]augmented",
    "nlp": r"\bnlp\b|natural language processing", "ci/cd": r"\bci\s*/\s*cd\b|continuous (?:integration|delivery|deployment)",
    "rest": r"\brest(?:ful)?\b(?: apis?)?", "aws": r"\baws\b|amazon web services", "gcp": r"\bgcp\b|google cloud",
    "machine learning": r"machine learning|\bml\b", "generative ai": r"generative ai|\bgenai\b|\bgen ai\b",
    "agents": r"(?:\bai|llm|autonomous|coding|multi)[- ]?agents?\b|agentic", "a/b testing": r"a/b test(?:s|ing)?", "fine-tuning": r"fine[- ]?tun(?:e|ed|ing)",
    "etl": r"\betl\b|\belt\b", "data pipelines": r"data pipelines?", "embeddings": r"\bembeddings?\b",
    "microservices": r"micro[- ]?services?", "hugging face": r"hugging ?face", "vue": r"\bvue(?:\.js)?\b",
    "react": r"\breact(?:\.js)?\b", "unit testing": r"unit tests?(?:ing)?", "spark": r"\b(?:py)?spark\b",
    "recommendation systems": r"recommend(?:ation|er) systems?", "time series": r"time[- ]series",
    "vector database": r"vector (?:database|db|store|search)s?", "forecasting": r"\bforecast(?:s|ing)?\b",
    "statistics": r"\bstatistic(?:s|al)\b", "experimentation": r"\bexperiment(?:s|ation)\b", "simulation": r"\bsimulat(?:ion|ions|or)\b",
}


def _vocabulary():
    terms, text = [], " " + " ".join(_PLAIN) + " "
    for phrase in _PHRASES:
        if f" {phrase} " in text:
            terms.append(phrase)
            text = text.replace(f" {phrase} ", " ")
    terms += text.split() + [t for t in ALIASES if t not in terms]
    return {t: re.compile(ALIASES.get(t) or rf"(?<![\w+#]){re.escape(t)}(?![\w+#])", re.I) for t in dict.fromkeys(terms)}


VOCAB = _vocabulary()

MIN_KEYWORDS = 4
HEADING = re.compile(r"^[\s#*•\-–]*([A-Za-z][A-Za-z '’/&,()+-]{2,70}?)[\s:*?]*$")
PREFERRED = re.compile(r"nice[- ]to[- ]have|preferred|bonus|\bplus(?:es)?\b|good to have|ideally|desired|extra credit|"
                       r"stand out|set you apart|even better|helpful", re.I)
REQUIRED = re.compile(r"requirement|qualification|must[- ]have|what you(?:'|’)?(?:ll)? (?:need|bring|have)|you have|"
                      r"minimum|who you are|about you|what we(?:'|’)?re looking for|skills|experience|you(?:'|’)ll need|"
                      r"you might|you may be|thrive|ideal candidate|you bring|we(?:'|’)?d love", re.I)
OTHER = re.compile(r"benefit|perks|compensation|salary|\babout (?!you\b)|equal opportunity|in this role|you will\b|"
                   r"responsibilit|what you(?:'|’)?ll do|the role|how to apply|our (?:mission|values|story)|why join", re.I)
INLINE_PREFERRED = re.compile(r"\b(?:preferred|nice to have|is a plus|a plus|bonus|ideally|desirable)\b", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*(?:years?|yrs?)", re.I)
BLOCKERS = [
    (re.compile(r"security clearance|\bTS/SCI\b|secret clearance", re.I),
     "asks for a security clearance (not available on F-1 OPT)"),
    (re.compile(r"u\.?s\.? citizen(?:ship)?(?! or)|must be a (?:us|u\.s\.) (?:citizen|person)", re.I),
     "asks for U.S. citizenship"),
    (re.compile(r"\bph\.?d\b[^.\n]{0,40}\brequired|requires? a ph\.?d", re.I), "asks for a PhD"),
]


def split_posting(description):
    """{"required": text, "preferred": text}. With no recognizable sections, the whole
    posting counts as required, minus sentences that say "preferred" or "a plus"."""
    required, preferred, mode, found = [], [], "required", False
    for line in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z•\-])", description or ""):
        line = line.strip()
        if not line:
            continue
        # A heading is short and is not a bullet ("- Experience with Docker" is a requirement).
        head = HEADING.match(line) if len(line.split()) <= 7 and line[0] not in "-•–" else None
        if head and not line.rstrip().endswith("."):
            name = head.group(1)
            if PREFERRED.search(name):
                mode, found = "preferred", True
                continue
            if OTHER.search(name):
                mode = "other"
                continue
            if REQUIRED.search(name):
                mode, found = "required", True
                continue
        if mode == "preferred" or INLINE_PREFERRED.search(line):
            preferred.append(line)
        elif mode == "required":
            required.append(line)
    if not found or not required:      # nothing we could call a requirements section
        required = [l for l in re.split(r"\n+|(?<=[.!?])\s+", description or "") if l.strip()
                    and not INLINE_PREFERRED.search(l)]
    return {"required": "\n".join(required), "preferred": "\n".join(preferred)}


def keywords(text):
    """Vocabulary terms a text names, most mentioned first."""
    counts = {t: len(rx.findall(text or "")) for t, rx in VOCAB.items()}
    return [t for t, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n]


def report(resume_text, job, other_resumes=None, years=None):
    """The matching report for one resume and one posting.

    other_resumes: {label: text} of your other library resumes, used to say where a
    missing keyword already appears. years: your years of experience, if known."""
    parts = split_posting(job.get("description"))
    have = set(keywords(resume_text))
    required = keywords(parts["required"])
    preferred = [k for k in keywords(parts["preferred"]) if k not in required]

    def cover(terms):
        return {"covered": [t for t in terms if t in have], "missing": [t for t in terms if t not in have]}

    req, pref = cover(required), cover(preferred)
    req_cov = len(req["covered"]) / len(required) if required else None
    pref_cov = len(pref["covered"]) / len(preferred) if preferred else None
    if len(required) + len(preferred) < MIN_KEYWORDS:
        alignment = None        # too few keywords for a percentage to mean anything
    elif pref_cov is None or req_cov is None:
        alignment = round(100 * (req_cov if pref_cov is None else pref_cov))
    else:
        alignment = round(100 * (0.75 * req_cov + 0.25 * pref_cov))

    gaps = []
    asked = [int(n) for n in YEARS.findall(parts["required"]) if int(n) <= 20]
    if asked and years is not None and min(asked) > float(years):
        gaps.append(f"asks for {min(asked)}+ years of experience; your profile says {years:g}")
    posting = job.get("description") or ""
    gaps += [text for rx, text in BLOCKERS if rx.search(posting)]

    suggestions = []
    for term in req["missing"] + pref["missing"]:
        where = [label for label, text in (other_resumes or {}).items() if VOCAB[term].search(text)]
        kind = "required" if term in req["missing"] else "preferred"
        if where:
            suggestions.append({"keyword": term, "kind": kind, "on_file": True,
                                "advice": f"Your {where[0]} already mentions it: bring that line into this resume."})
        else:
            suggestions.append({"keyword": term, "kind": kind, "on_file": False,
                                "advice": "None of your resumes mention it. Add it only if you have really used it; "
                                          "otherwise leave it out."})
    return {"alignment": alignment, "required": req, "preferred": pref, "gaps": gaps, "suggestions": suggestions,
            "missing_keywords": req["missing"] + pref["missing"],
            "note": "Keyword coverage only: an estimate, not a score from a real applicant tracking system."}


def compare(master_text, tailored_text, job, **kw):
    """Reports for the master resume and the tailored one, and the keywords tailoring added."""
    before, after = report(master_text, job, **kw), report(tailored_text, job, **kw)
    gained = [k for k in before["missing_keywords"] if k not in after["missing_keywords"]]
    return {"before": before, "after": after, "gained": gained}
