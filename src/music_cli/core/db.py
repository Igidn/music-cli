"""Shared SQLite plumbing for the app's persistent state.

Two databases replace the app's JSON files and marker files:

* ``cache.db`` in the cache directory — the audio cache index.
* ``music-cli.db`` in the config directory — play history and settings.

Both are plain stdlib ``sqlite3`` databases opened in WAL mode so a crash
never leaves a half-written page behind, and schema versions are tracked
with ``PRAGMA user_version``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1


def open_db(path: str | Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    """Open (creating the parent directory) a SQLite connection in WAL mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def reset_database(path: str | Path) -> None:
    """Delete a database and its WAL side files (used to recover corruption)."""
    path = Path(path)
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{path}{suffix}").unlink()
        except OSError:
            pass


def open_db_with_recovery(path: str | Path) -> sqlite3.Connection:
    """Open a database with its schema ensured, resetting it when corrupt.

    A corrupt database is discarded and recreated from scratch (orphan audio
    files are re-adopted by the cache). The smoke query forces a real read
    so corruption deeper than the header still triggers recovery.
    """
    try:
        conn = open_db(path)
        ensure_schema(conn)
        conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
        return conn
    except sqlite3.DatabaseError:
        reset_database(path)
        conn = open_db(path)
        ensure_schema(conn)
        return conn


def ensure_schema(conn: sqlite3.Connection, version: int = SCHEMA_VERSION) -> None:
    """Upgrade the database to ``version``, running migrations as needed."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= version:
        return
    _migrate(conn, current, version)
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()


def _migrate(conn: sqlite3.Connection, current: int, target: int) -> None:
    """Run each migration step in order; fresh databases get the full schema."""
    version = current
    if version < 1:
        _schema_v1(conn)
        version = 1
    if version < target:
        raise NotImplementedError(f"No migration path from schema v{version}")


def _schema_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            video_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            artists TEXT NOT NULL DEFAULT '[]',
            duration REAL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            added REAL NOT NULL,
            last_used REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS play_history (
            video_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            artists TEXT NOT NULL DEFAULT '[]',
            duration REAL,
            played INTEGER NOT NULL DEFAULT 1,
            last_played REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )