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
            url TEXT UNIQUE,
            description TEXT,
            match_score REAL,
            date_added TEXT
        )
    """)
    conn.commit()
    conn.close()
    print("Table ready.")

from datetime import date

def save_job(company, title, url, description, match_score):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO jobs (company, title, url, description, match_score, date_added) VALUES (?, ?, ?, ?, ?, ?)",
            (company, title, url, description, match_score, str(date.today()))
        )
        conn.commit()
        print(f"Saved: {title} at {company}")
    except sqlite3.IntegrityError:
        print(f"Skipped duplicate: {title} at {company}")
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
create_table()

save_job("Google", "ML Engineer", "google.com/job/1", "Build ML systems", 0.82)
save_job("Amazon", "Data Scientist", "amazon.com/job/2", "Analyze data", 0.65)
save_job("Netflix", "AI Engineer", "netflix.com/job/3", "Recommendation models", 0.91)
save_job("Google", "ML Engineer", "google.com/job/1", "Build ML systems", 0.82)  # same URL — should be skipped

print("\n--- Jobs ranked by match ---")
for row in get_jobs_ranked():
    print(row)