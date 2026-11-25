from docx import Document
from pypdf import PdfReader
from tkinter import filedialog
import tkinter as tk

def read_resume(file_path):
    if file_path.endswith(".docx"):
        return read_docx(file_path)
    elif file_path.endswith(".pdf"):
        return read_pdf(file_path)
    else:
        raise ValueError("Unsupported file type: use .docx or .pdf")

def read_docx(file_path):
    doc = Document(file_path)
    text = ""
    for paragraph in doc.paragraphs:
        text = text + paragraph.text + " "
    return text

def read_pdf(file_path):
    reader = PdfReader(file_path)
    text = ""
    for page in reader.pages:
        text = text + page.extract_text() + " "
    return text

if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    file_path = filedialog.askopenfilename(
        title="Select your resume",
        filetypes=[("Documents", "*.pdf *.docx"), ("All files", "*.*")]
    )
    if not file_path:
        print("No file selected.")
    else:
        resume_text = read_resume(file_path)
        print(resume_text)