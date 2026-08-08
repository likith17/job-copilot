#check
import sqlite3
conn = sqlite3.connect("jobs.db")
cursor = conn.cursor()
cursor.execute("SELECT company, title, match_score, date_added FROM jobs ORDER BY match_score DESC")
for row in cursor.fetchall():
    print(row)
conn.close()

#clean
# import sqlite3
# conn = sqlite3.connect("jobs.db")
# cursor = conn.cursor()
# cursor.execute("DELETE FROM jobs WHERE company='Columbia University' AND match_score < 0.5")
# conn.commit()
# print(f"Deleted {cursor.rowcount} row(s)")
# conn.close()