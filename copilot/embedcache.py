"""Job and resume embeddings on disk, so a resume change only embeds what changed.

Kept out of the main DB: they are large, and safe to delete at any time.
"""
import hashlib
import sqlite3
import threading

import numpy as np

from copilot.config import ROOT

PATH = ROOT / "data" / "embeddings.db"
_lock = threading.Lock()
_conn = None


def _db():
    global _conn
    if _conn is None:
        PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(PATH, check_same_thread=False, timeout=30)
        _conn.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vec BLOB)")
    return _conn


def _key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def get(text):
    with _lock:
        row = _db().execute("SELECT vec FROM embeddings WHERE key = ?", (_key(text),)).fetchone()
    return np.frombuffer(row[0], dtype=np.float32) if row else None


def put(text, vec):
    with _lock:
        _db().execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?)",
                      (_key(text), np.asarray(vec, dtype=np.float32).tobytes()))
        _db().commit()
