"""Acceso a SQLite. Cada microservicio tiene su propio archivo de base de datos."""
import sqlite3
from contextlib import contextmanager


@contextmanager
def get_db(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
