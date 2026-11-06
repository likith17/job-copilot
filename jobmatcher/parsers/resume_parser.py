from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Set

from jobmatcher.models.resume import ResumeProfile
from jobmatcher.parsers.skills import extract_skills


class ResumeParser:
    """Parses resume files (PDF, DOCX, TXT) into ResumeProfile."""

    SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

    def __init__(self, custom_skills: Optional[list[str]] = None):
        self.custom_skills = custom_skills or []

    def parse_file(self, file_path: str | Path) -> ResumeProfile:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Resume file not found: {file_path}")

        ext = path.suffix.lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported resume format '{ext}'. Supported: {self.SUPPORTED_EXTENSIONS}")

        text = self._extract_text(path, ext)
        if not text.strip():
            raise ValueError(f"Could not extract text from resume: {file_path}")

        return self.parse_text(text)

    def parse_text(self, text: str) -> ResumeProfile:
        # Basic heuristic extraction
        email = ResumeProfile.extract_email(text)
        phone = ResumeProfile.extract_phone(text)

        # Name: heuristic - first line that looks like a name (2 words capitalized)
        name = self._extract_name(text)

        skills = extract_skills(text, custom_skills=self.custom_skills)

        # Titles: look for common title patterns in text
        titles = self._extract_titles(text)

        # Experience years: search for "X years" or "X+ years"
        exp_years = self._extract_experience_years(text)

        # Summary: first 500 chars or first paragraph
        summary = self._extract_summary(text)

        # Keywords: top frequent non-stopwords? For now, use skills + title words
        keywords = set(skills)
        keywords.update([t.lower() for t in titles])

        return ResumeProfile(
            raw_text=text,
            name=name,
            email=email,
            phone=phone,
            summary=summary,
            skills=skills,
            experience_years=exp_years,
            titles=titles,
            keywords=keywords,
        )

    def _extract_text(self, path: Path, ext: str) -> str:
        if ext == ".pdf":
            return self._extract_pdf(path)
        elif ext == ".docx":
            return self._extract_docx(path)
        elif ext in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="ignore")
        else:
            return ""

    def _extract_pdf(self, path: Path) -> str:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            texts = []
            for page in reader.pages:
                try:
                    texts.append(page.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(texts)
        except Exception as e:
            raise RuntimeError(f"Failed to parse PDF with pypdf: {e}. Try installing pdfminer.six as fallback.") from e

    def _extract_docx(self, path: Path) -> str:
        try:
            import docx

            doc = docx.Document(str(path))
            return "\n".join([para.text for para in doc.paragraphs])
        except Exception as e:
            raise RuntimeError(f"Failed to parse DOCX: {e}") from e

    def _extract_name(self, text: str) -> str:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return ""
        # heuristic: first line with 2-3 capitalized words, not containing email, not too long
        for line in lines[:5]:
            if "@" in line or len(line) > 50:
                continue
            if re.match(r"^([A-Z][a-z]+[\s-]?){1,3}$", line):
                return line
            # fallback: first line short
            if len(line.split()) <= 4 and not re.search(r"\d", line):
                # check capitalized
                words = line.split()
                if all(w[0].isupper() for w in words if w):
                    return line
        return lines[0][:50] if lines else ""

    def _extract_titles(self, text: str) -> list[str]:
        common_titles = [
            "Software Engineer",
            "Senior Software Engineer",
            "Staff Engineer",
            "Principal Engineer",
            "Software Developer",
            "Full Stack Engineer",
            "Frontend Engineer",
            "Backend Engineer",
            "Data Scientist",
            "Data Engineer",
            "Machine Learning Engineer",
            "ML Engineer",
            "DevOps Engineer",
            "Site Reliability Engineer",
            "SRE",
            "Product Manager",
            "Project Manager",
            "Engineering Manager",
            "Tech Lead",
        ]
        found = []
        text_lower = text.lower()
        for title in common_titles:
            if title.lower() in text_lower:
                if title not in found:
                    found.append(title)

        # Also regex for "Experience as <title>"
        pattern = re.compile(r"(?:as|role|position|title)\s*[:\-]?\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,3}\sEngineer)", re.IGNORECASE)
        for m in pattern.finditer(text):
            t = m.group(1).strip()
            if t not in found and len(t) < 40:
                found.append(t)

        return found[:10]

    def _extract_experience_years(self, text: str) -> Optional[int]:
        # Patterns like "5 years of experience", "5+ years"
        patterns = [
            r"(\d+)\+?\s*years?\s+of\s+experience",
            r"(\d+)\+?\s*years?\s+experience",
            r"experience\s*:\s*(\d+)\s*years?",
            r"(\d+)\s*years?\s+in\s+\w+",
        ]
        max_years = None
        for pat in patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                try:
                    y = int(m.group(1))
                    if 0 <= y <= 50:
                        if max_years is None or y > max_years:
                            max_years = y
                except ValueError:
                    continue
        return max_years

    def _extract_summary(self, text: str) -> str:
        # First paragraph or first 3 lines
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return ""
        # Look for summary section
        summary_match = re.search(r"summary[:\-]?\s*(.+?)(?:\n\n|\r\n\r\n|experience|skills|education)", text, re.IGNORECASE | re.DOTALL)
        if summary_match:
            return summary_match.group(1).strip()[:800]
        return " ".join(lines[:5])[:800]


def parse_resume_file(file_path: str | Path, custom_skills: Optional[list[str]] = None) -> ResumeProfile:
    parser = ResumeParser(custom_skills=custom_skills)
    return parser.parse_file(file_path)
