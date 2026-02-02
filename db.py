"""
Database layer for Samruddhi.
- Local: SQLite (database.db)
- Vercel/production: Turso (libsql) when TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are set.
"""

import os

_TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_USE_TURSO = bool(_TURSO_URL and _TURSO_TOKEN)


def _sqlite_conn():
    import sqlite3
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _turso_conn():
    import libsql
    # On Vercel /tmp is writable; local file is a replica, Turso is source of truth.
    local = "/tmp/samruddhi.db" if os.name != "nt" else os.path.join(os.environ.get("TMP", "."), "samruddhi.db")
    c = libsql.connect(local, sync_url=_TURSO_URL, auth_token=_TURSO_TOKEN)
    c.sync()
    return c


def get_db():
    if _USE_TURSO:
        return _turso_conn()
    return _sqlite_conn()


def _close_sync(conn):
    if _USE_TURSO and hasattr(conn, "sync"):
        try:
            conn.sync()
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass


def init_db():
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leftovers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                length REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
        if _USE_TURSO and hasattr(conn, "sync"):
            conn.sync()
    finally:
        _close_sync(conn)


def get_leftovers_sorted():
    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT id, length, created_at FROM leftovers ORDER BY length DESC"
        )
        rows = cur.fetchall()
        # Normalize to dicts (sqlite3.Row already supports keys; libsql may return list-like)
        if rows and hasattr(rows[0], "keys"):
            return [dict(r) for r in rows]
        cols = ["id", "length", "created_at"] if rows else []
        return [dict(zip(cols, r)) for r in rows]
    finally:
        _close_sync(conn)


def delete_leftover(lid):
    conn = get_db()
    try:
        conn.execute("DELETE FROM leftovers WHERE id = ?", (lid,))
        conn.commit()
        if _USE_TURSO and hasattr(conn, "sync"):
            conn.sync()
    finally:
        _close_sync(conn)


def insert_leftover(length):
    conn = get_db()
    try:
        conn.execute("INSERT INTO leftovers (length) VALUES (?)", (round(length, 2),))
        conn.commit()
        if _USE_TURSO and hasattr(conn, "sync"):
            conn.sync()
    finally:
        _close_sync(conn)


def delete_leftovers_batch(ids):
    """Delete multiple leftovers by id in one transaction. ids: list of int."""
    if not ids:
        return
    conn = get_db()
    try:
        placeholders = ",".join("?" * len(ids))
        conn.execute("DELETE FROM leftovers WHERE id IN ({})".format(placeholders), ids)
        conn.commit()
        if _USE_TURSO and hasattr(conn, "sync"):
            conn.sync()
    finally:
        _close_sync(conn)


def insert_leftovers_batch(lengths):
    """Insert multiple leftover lengths in one transaction. lengths: list of float."""
    if not lengths:
        return
    conn = get_db()
    try:
        for length in lengths:
            conn.execute("INSERT INTO leftovers (length) VALUES (?)", (round(length, 2),))
        conn.commit()
        if _USE_TURSO and hasattr(conn, "sync"):
            conn.sync()
    finally:
        _close_sync(conn)


def clear_all_leftovers():
    """Delete all rows from leftovers table (local SQLite or Turso)."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM leftovers")
        conn.commit()
        if _USE_TURSO and hasattr(conn, "sync"):
            conn.sync()
    finally:
        _close_sync(conn)
