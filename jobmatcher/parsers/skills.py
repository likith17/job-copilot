"""
Curated skill database and extraction logic.
Designed to be easily extensible - add skills to SKILL_KEYWORDS or via custom list.
"""

from __future__ import annotations
import re
from typing import Set, List, Dict


# Comprehensive tech skill taxonomy - easy to extend
SKILL_KEYWORDS: Dict[str, List[str]] = {
    # Programming languages
    "Python": ["python"],
    "Java": [r"\bjava\b"],
    "JavaScript": ["javascript", "js"],
    "TypeScript": ["typescript", "ts"],
    "Go": [r"\bgo\b", r"\bgolang\b"],
    "Rust": [r"\brust\b"],
    "C++": [r"c\+\+"],
    "C#": [r"c#", r"\bcsharp\b"],
    "Ruby": [r"\bruby\b"],
    "PHP": [r"\bphp\b"],
    "Swift": [r"\bswift\b"],
    "Kotlin": [r"\bkotlin\b"],
    "Scala": [r"\bscala\b"],
    # Frontend
    "React": [r"\breact\b", r"react\.js", r"reactjs"],
    "Vue": [r"\bvue\b", r"vue\.js"],
    "Angular": [r"\bangular\b"],
    "Next.js": [r"next\.js", r"nextjs"],
    "HTML": [r"\bhtml\b"],
    "CSS": [r"\bcss\b"],
    "Tailwind": [r"tailwind"],
    # Backend / Frameworks
    "Node.js": [r"node\.js", r"nodejs", r"\bnode\b"],
    "Django": [r"\bdjango\b"],
    "Flask": [r"\bflask\b"],
    "FastAPI": [r"fastapi"],
    "Spring": [r"\bspring\b", r"spring boot"],
    "Express": [r"\bexpress\b", r"express\.js"],
    # Data / ML / AI
    "Machine Learning": [r"machine learning", r"\bml\b", r"ml/ai"],
    "Deep Learning": [r"deep learning"],
    "Data Science": [r"data science"],
    "TensorFlow": [r"tensorflow"],
    "PyTorch": [r"pytorch"],
    "Pandas": [r"\bpandas\b"],
    "NumPy": [r"numpy"],
    "SQL": [r"\bsql\b"],
    "NoSQL": [r"nosql"],
    "PostgreSQL": [r"postgres", r"postgresql"],
    "MySQL": [r"\bmysql\b"],
    "MongoDB": [r"mongodb", r"\bmongo\b"],
    "Redis": [r"\bredis\b"],
    "Elasticsearch": [r"elasticsearch"],
    "Snowflake": [r"snowflake"],
    "Spark": [r"\bspark\b", r"apache spark"],
    "Airflow": [r"airflow"],
    "Hadoop": [r"\bhadoop\b"],
    # Cloud / DevOps
    "AWS": [r"\baws\b", r"amazon web services"],
    "Azure": [r"\bazure\b"],
    "GCP": [r"\bgcp\b", r"google cloud"],
    "Docker": [r"\bdocker\b"],
    "Kubernetes": [r"kubernetes", r"\bk8s\b"],
    "Terraform": [r"terraform"],
    "Jenkins": [r"\bjenkins\b"],
    "CI/CD": [r"ci/cd", r"\bci\b", r"\bcd\b", r"continuous integration"],
    "Linux": [r"\blinux\b"],
    "Git": [r"\bgit\b"],
    # Methodologies & Other
    "REST": [r"\brest\b", r"rest api", r"restful"],
    "GraphQL": [r"graphql"],
    "gRPC": [r"grpc"],
    "Microservices": [r"microservices"],
    "Agile": [r"\bagile\b"],
    "Scrum": [r"\bscrum\b"],
    "System Design": [r"system design"],
    "Product Management": [r"product management"],
    "Leadership": [r"leadership", r"team lead", r"tech lead"],
    "Communication": [r"communication"],
    # Add more as needed
}

# Flatten for quick lookup
SKILL_DB: List[str] = sorted(SKILL_KEYWORDS.keys())

# Precompile regex patterns for performance
COMPILED_PATTERNS: Dict[str, List[re.Pattern]] = {
    skill: [re.compile(p, re.IGNORECASE) for p in patterns]
    for skill, patterns in SKILL_KEYWORDS.items()
}


def extract_skills(text: str, custom_skills: List[str] | None = None) -> Set[str]:
    """
    Extract skills from text using regex matching.
    Returns set of canonical skill names.
    """
    if not text:
        return set()

    found: Set[str] = set()
    # text = text.lower() but regex is case-insensitive

    for skill, patterns in COMPILED_PATTERNS.items():
        for pat in patterns:
            if pat.search(text):
                found.add(skill)
                break

    if custom_skills:
        text_lower = text.lower()
        for cs in custom_skills:
            if cs.lower() in text_lower:
                found.add(cs)

    return found


def extract_skills_with_context(text: str) -> Dict[str, List[str]]:
    """
    Returns dict mapping skill -> list of snippets where found.
    Useful for debugging/explanations.
    """
    result: Dict[str, List[str]] = {}
    sentences = re.split(r"[.\n]", text)
    for skill, patterns in COMPILED_PATTERNS.items():
        snippets = []
        for sent in sentences:
            for pat in patterns:
                if pat.search(sent):
                    snippets.append(sent.strip()[:120])
                    break
        if snippets:
            result[skill] = snippets[:3]
    return result


def add_custom_skill(skill_name: str, patterns: List[str]) -> None:
    """Extend global skill DB at runtime."""
    SKILL_KEYWORDS[skill_name] = patterns
    COMPILED_PATTERNS[skill_name] = [re.compile(p, re.IGNORECASE) for p in patterns]
    if skill_name not in SKILL_DB:
        SKILL_DB.append(skill_name)
        SKILL_DB.sort()
