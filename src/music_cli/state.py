"""Persistence for play history and player settings.

Everything lives in a single SQLite database (``music-cli.db`` in the
config directory, WAL mode):

* ``play_history`` — every track the user has played, with a running
  ``played`` counter and the last time it was heard. The most recent
  entry is what playback resumes from on startup, and the counters are
  the raw material for future year-in-review-style features.
* ``settings`` — persistent player preferences (volume, muted, loop) and
  small app flags (the ``no_sign_in`` marker), stored as text key/value.

Legacy JSON/marker files are imported once on first access and removed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .db import ensure_schema, open_db, reset_database
from .paths import config_dir

STATE_DB_FILENAME = "music-cli.db"
LEGACY_STATE_FILENAME = "last-track.json"

SETTING_VOLUME = "volume"
SETTING_MUTED = "muted"
SETTING_LOOP = "loop"
SETTING_AUTO_NEXT = "auto_next"


@dataclass(frozen=True)
class PlayedTrack:
    """One row of play history: a track identity plus its play counts."""

    video_id: str
    title: str
    artists: tuple[str, ...] = ()
    duration: float | None = None
    played: int = 1
    last_played: float | None = None


class PlayHistoryStore:
    """SQLite-backed play history, one row per track with a play counter."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else config_dir() / STATE_DB_FILENAME
        self._conn: sqlite3.Connection | None = None

    def record(self, track: PlayedTrack) -> None:
        """Record a playback of ``track``, bumping its counter.

        Every playback counts — a manual play, an auto-next play, and a
        loop repeat alike — and refreshes the track's last-played time.
        """
        conn = self._ensure_open()
        now = time.time()
        conn.execute(
            """
            INSERT INTO play_history
                (video_id, title, artists, duration, played, last_played)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title = excluded.title,
                artists = excluded.artists,
                duration = excluded.duration,
                played = play_history.played + 1,
                last_played = excluded.last_played
            """,
            (
                track.video_id,
                track.title or track.video_id,
                json.dumps(list(track.artists)),
                track.duration,
                now,
            ),
        )
        conn.commit()

    def most_recent(self) -> PlayedTrack | None:
        """The most recently played track, for resuming playback."""
        row = self._ensure_open().execute(
            "SELECT * FROM play_history ORDER BY last_played DESC LIMIT 1"
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def top(self, limit: int = 50) -> list[PlayedTrack]:
        """The most-played tracks, highest count first."""
        rows = self._ensure_open().execute(
            "SELECT * FROM play_history ORDER BY played DESC, last_played DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _ensure_open(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = self._open_database()
            self._migrate_legacy_json(conn)
            self._conn = conn
        return self._conn

    def _open_database(self) -> sqlite3.Connection:
        try:
            conn = open_db(self.path)
            ensure_schema(conn)
            return conn
        except sqlite3.DatabaseError:
            reset_database(self.path)
            conn = open_db(self.path)
            ensure_schema(conn)
            return conn

    def _migrate_legacy_json(self, conn: sqlite3.Connection) -> None:
        """Import the pre-SQLite ``last-track.json`` once, then remove it."""
        path = self.path.parent / LEGACY_STATE_FILENAME
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            video_id = raw.get("video_id")
            if isinstance(video_id, str) and video_id:
                title = raw.get("title")
                artists = tuple(
                    artist
                    for artist in raw.get("artists") or ()
                    if isinstance(artist, str)
                )
                duration = raw.get("duration")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO play_history
                        (video_id, title, artists, duration, played, last_played)
                    VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (
                        video_id,
                        title if isinstance(title, str) and title else video_id,
                        json.dumps(list(artists)),
                        duration
                        if isinstance(duration, (int, float)) and duration > 0
                        else None,
                        time.time(),
                    ),
                )
                conn.commit()
        path.unlink(missing_ok=True)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PlayedTrack:
        try:
            artists = json.loads(row["artists"] or "[]")
        except ValueError:
            artists = []
        return PlayedTrack(
            video_id=row["video_id"],
            title=row["title"] or row["video_id"],
            artists=tuple(a for a in artists if isinstance(a, str)),
            duration=row["duration"],
            played=row["played"] or 1,
            last_played=row["last_played"],
        )


class SettingsStore:
    """SQLite-backed key/value settings (player prefs and app flags)."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else config_dir() / STATE_DB_FILENAME
        self._conn: sqlite3.Connection | None = None

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._ensure_open().execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key)
        if value is None:
            return default
        return value == "1"

    def set(self, key: str, value: str | int | bool) -> None:
        conn = self._ensure_open()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, "1" if value is True else "0" if value is False else str(value)),
        )
        conn.commit()

    def delete(self, key: str) -> None:
        conn = self._ensure_open()
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _ensure_open(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                conn = open_db(self.path)
                ensure_schema(conn)
            except sqlite3.DatabaseError:
                reset_database(self.path)
                conn = open_db(self.path)
                ensure_schema(conn)
            self._conn = conn
        return self._conn