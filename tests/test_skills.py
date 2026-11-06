from jobmatcher.parsers.skills import extract_skills, SKILL_DB, add_custom_skill
from jobmatcher.ranking.skills import SkillMatcher


def test_extract_python_skill():
    text = "Experienced in Python, Docker and AWS. Built ML models with TensorFlow."
    skills = extract_skills(text)
    assert "Python" in skills
    assert "Docker" in skills
    assert "AWS" in skills
    assert "TensorFlow" in skills


def test_extract_skills_empty():
    assert extract_skills("") == set()


def test_skill_db_not_empty():
    assert len(SKILL_DB) > 20


def test_custom_skill():
    add_custom_skill("MyCustomSkill", [r"my-custom-tech"])
    skills = extract_skills("We use my-custom-tech daily")
    assert "MyCustomSkill" in skills


def test_skill_matcher_basic():
    matcher = SkillMatcher()
    resume_skills = {"Python", "Docker", "AWS"}
    job_text = "Looking for Python developer with Docker and Kubernetes experience"
    matched, missing, score, job_skills = matcher.match(resume_skills, job_text)
    assert "Python" in matched
    assert "Docker" in matched
    assert "Kubernetes" in job_skills
    assert 0 <= score <= 1


def test_skill_matcher_no_job_skills():
    matcher = SkillMatcher()
    matched, missing, score, job_skills = matcher.match({"Python"}, "Generic job with no tech")
    assert score == 0.0
