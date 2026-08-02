from matcher import match_score
from sentence_transformers import SentenceTransformer

print("Loading model...")
model = SentenceTransformer("all-MiniLM-L6-v2")
print("Model loaded.")

# text = "machine learning engineer"
# vector = model.encode(text)

# print("Text:", text)
# print("Vector length:", len(vector))
# print("First 10 numbers:", vector[:10])

import numpy as np
# phrase1 = "machine learning engineer"
# phrase2 = "ML developer"
# phrase3 = "banana bread recipe"

# vec1 = model.encode(phrase1)
# vec2 = model.encode(phrase2)
# vec3 = model.encode(phrase3)

def cosine_similarity(a, b):
    dot = np.dot(a, b)
    return dot / (np.linalg.norm(a) * np.linalg.norm(b))

resume = """Python developer with 3 years experience building machine learning
models, deep learning with PyTorch, computer vision, and Docker deployment."""

job = """We are hiring an ML engineer to develop and deploy AI models in production.
Must know Python, neural networks, and containerization."""

resume_vec = model.encode(resume)
job_vec = model.encode(job)

score = cosine_similarity(resume_vec, job_vec)
print("Resume vs Job match score:", score)
# print("ML engineer  vs  ML developer:", cosine_similarity(vec1, vec2))
# print("ML engineer  vs  banana bread:", cosine_similarity(vec1, vec3))
# A = np.array([1, 0, 1])
# B = np.array([1, 1, 0])

# dot_product = np.dot(A, B)
# magnitude_A = np.linalg.norm(A)
# magnitude_B = np.linalg.norm(B)

# cosine = dot_product / (magnitude_A * magnitude_B)

# print("Dot product:", dot_product)
# print("Magnitude A:", magnitude_A)
# print("Magnitude B:", magnitude_B)
# print("Cosine similarity:", cosine)

# Layer 1: keyword matcher
keyword_score, shared = match_score(resume, job)

# Layer 3: embedding matcher
embedding_score = cosine_similarity(resume_vec, job_vec)

print("\n--- Head to head ---")
print("Keyword (Layer 1) score:", round(keyword_score, 3))
print("Shared words:", shared)
print("Embedding (Layer 3) score:", round(embedding_score, 3))