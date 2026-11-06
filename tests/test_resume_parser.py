import tempfile
from pathlib import Path

from jobmatcher.parsers.resume_parser import ResumeParser

SAMPLE_RESUME = """
John Doe
john.doe@example.com
+1 (555) 123-4567

Summary:
Senior Software Engineer with 5 years of experience in Python, AWS, Docker, Kubernetes.

Experience:
Software Engineer at Acme Corp
- Built microservices in Python and Go
- Used AWS, Docker, Kubernetes, PostgreSQL

Skills: Python, Java, AWS, Docker, Kubernetes, React, PostgreSQL, Redis

Education: BS Computer Science
"""


def test_parse_text():
    parser = ResumeParser()
    profile = parser.parse_text(SAMPLE_RESUME)
    assert profile.email == "john.doe@example.com"
    assert "Python" in profile.skills
    assert "AWS" in profile.skills
    assert profile.experience_years == 5
    assert len(profile.raw_text) > 0


def test_parse_file_txt():
    parser = ResumeParser()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(SAMPLE_RESUME)
        f.flush()
        profile = parser.parse_file(f.name)
        assert "Python" in profile.skills
        Path(f.name).unlink()


def test_parse_empty_raises():
    parser = ResumeParser()
    try:
        parser.parse_file("/nonexistent/file.txt")
        assert False
    except FileNotFoundError:
        pass


def test_extract_email():
    from jobmatcher.models.resume import ResumeProfile

    assert ResumeProfile.extract_email("Contact me at test@example.com") == "test@example.com"
    assert ResumeProfile.extract_email("No email here") == ""
