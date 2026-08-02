STOP_WORDS = {"a", "an", "the", "and", "or", "with", "for", "of", "to", "in", "on", "is", "are", "as", "at", "by"}

def get_words(text):
    text = text.lower()
    words = text.split()
    for punctuation in ['.', ',', '!', '?', ';', ':', '"', "'", '(', ')']:
        words = [word.replace(punctuation, '') for word in words]
    cleaned = {word for word in words if word not in STOP_WORDS}
    return cleaned

def match_score(resume_text, job_text):
    resume_words = get_words(resume_text)
    job_words = get_words(job_text)
    shared = resume_words & job_words
    total = resume_words | job_words
    score = len(shared) / len(total)
    return score, shared

if __name__ == "__main__":
    resume = "Python developer with machine learning and Docker experience"
    job = "Looking for a Python engineer with Docker and cloud skills"
    score, shared = match_score(resume, job)
    print("Score:", score)
    print("Shared words:", shared)