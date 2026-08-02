import sqlite3

def get_connection():
    return sqlite3.connect("jobs.db")

def create_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT,
            title TEXT,
            description TEXT,
            match_score REAL,
            date_added TEXT
        )
    """)
    conn.commit()
    conn.close()
    print("Table ready.")

from datetime import date

def save_job(company, title, description, match_score):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO jobs (company, title, description, match_score, date_added) VALUES (?, ?, ?, ?, ?)",
        (company, title, description, match_score, str(date.today()))
    )
    conn.commit()
    conn.close()

def get_jobs_ranked():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT company, title, match_score, date_added FROM jobs ORDER BY match_score DESC")
    rows = cursor.fetchall()
    conn.close()
    return rows

create_table()

create_table()

# Add a few fake jobs to test
save_job("Google", "ML Engineer", "Build ML systems", 0.82)
save_job("Amazon", "Data Scientist", "Analyze data", 0.65)
save_job("Netflix", "AI Engineer", "Recommendation models", 0.91)

# Read them back, ranked
print("\n--- Jobs ranked by match ---")
for row in get_jobs_ranked():
    print(row)