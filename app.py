from sentence_transformers import SentenceTransformer
import numpy as np
from resume_reader import read_resume
from database import create_table, save_job, get_jobs_ranked
import tkinter as tk
from tkinter import filedialog

model = SentenceTransformer("all-MiniLM-L6-v2")

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

# 1. Make sure the database table exists
create_table()

# 2. Read the real resume via file browser
root = tk.Tk()
root.withdraw()
resume_path = filedialog.askopenfilename(title="Select your resume", filetypes=[("Documents", "*.pdf *.docx")])
if not resume_path:
    print("No resume selected. Exiting.")
    exit()
resume_text = read_resume(resume_path)
resume_vec = model.encode(resume_text)
print("Resume loaded and embedded.\n")

# 3. Score one job against it
company = input("Company: ")
title = input("Job title: ")
url = input("Job URL: ")
with open("job.txt", "r", encoding="utf-8") as f:
    description = f.read()

job_vec = model.encode(description)
score = float(cosine_similarity(resume_vec, job_vec))
print(f"\nMatch score: {round(score, 3)}")

# 4. Save it to the database
save_job(company, title, url, description, score)

# 5. Show all jobs ranked
print("\n--- Your jobs, ranked by match ---")
for row in get_jobs_ranked():
    print(row)