"""Persistent disk cache for downloaded audio tracks.

Cached tracks live as plain files under ``tracks/{video_id}.{ext}`` with
their metadata in a SQLite index (``cache.db`` in the cache directory,
WAL mode). A replay of a cached track needs no network at all: the client
plays the local file straight away. The cache enforces size, entry-count
and age budgets with LRU eviction, and serialises downloads of the same
video across threads so each video is fetched at most once at a time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .db import ensure_schema, open_db, reset_database

CACHE_VERSION = 2
DEFAULT_MAX_SIZE = 2 * 1024**3
DEFAULT_MAX_ENTRIES = 500
DEFAULT_TTL = timedelta(days=30)

CACHE_DB_FILENAME = "cache.db"


def default_cache_dir() -> Path:
    """The default cache directory, honouring MUSIC_CLI_CACHE_DIR and XDG."""
    override = os.environ.get("MUSIC_CLI_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "music-cli"
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "music-cli"
    return Path.home() / ".cache" / "music-cli"


@dataclass(frozen=True)
class TrackMeta:
    """Metadata for one cached track, persisted in the index."""

    title: str
    artists: tuple[str, ...] = ()
    duration: float | None = None
    ext: str = ""


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of a downloader: where the audio landed and its metadata."""

    path: str
    meta: TrackMeta


@dataclass(frozen=True)
class CachedTrack:
    """A track present in the cache, enough to play it without any network."""

    video_id: str
    title: str
    artists: tuple[str, ...]
    duration: float | None
    ext: str
    size: int


@dataclass
class _InFlight:
    """A download slot claimed by one thread; waiters block on ``done``."""

    done: threading.Event = field(default_factory=threading.Event)


class AudioCache:
    """An LRU disk cache of downloaded audio tracks.

    Thread-safe. Audio files are moved into place atomically
    (``os.replace``) and the SQLite index runs in WAL mode, so a crash never
    leaves a half-written track or index behind. A corrupt index is rebuilt
    from scratch (orphan audio files are re-adopted).
    """

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        max_size: int = DEFAULT_MAX_SIZE,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl: timedelta | None = DEFAULT_TTL,
    ) -> None:
        self.directory = (
            Path(directory) if directory is not None else default_cache_dir()
        )
        self.max_size = max_size
        self.max_entries = max_entries
        self.ttl = ttl
        self._tracks_dir = self.directory / "tracks"
        self._db_path = self.directory / CACHE_DB_FILENAME
        self._conn: sqlite3.Connection | None = None
        self._entries: dict[str, dict[str, Any]] = {}
        self._inflight: dict[str, _InFlight] = {}
        self._lock = threading.RLock()
        self._dirty = False
        self._load()

    def lookup(self, video_id: str) -> CachedTrack | None:
        """Cached metadata for ``video_id``, or None when not cached.

        Entries whose file has vanished are dropped. A hit refreshes the
        entry's last-use time (the eviction clock).
        """
        with self._lock:
            entry = self._entries.get(video_id)
            if entry is None:
                return None
            path = self._tracks_dir / f"{video_id}.{entry['ext']}"
            if not path.is_file():
                self._entries.pop(video_id, None)
                self._dirty = True
                return None
            entry["last_used"] = time.time()
            return CachedTrack(
                video_id=video_id,
                title=entry["title"],
                artists=tuple(entry.get("artists") or ()),
                duration=entry.get("duration"),
                ext=entry["ext"],
                size=entry["size"],
            )

    def path_for(self, video_id: str) -> Path | None:
        """The cached audio file for ``video_id``, or None when not cached.

        A lighter check than :meth:`lookup` (no metadata, no last-use bump);
        used to decide whether a download is needed.
        """
        with self._lock:
            entry = self._entries.get(video_id)
            if entry is None:
                return None
            path = self._tracks_dir / f"{video_id}.{entry['ext']}"
            if not path.is_file():
                self._entries.pop(video_id, None)
                self._dirty = True
                return None
            return path

    def tmp_path(self, video_id: str) -> Path:
        """A unique download target inside the cache, without extension.

        Downloaders write to ``<target>.<ext>``; :meth:`commit` renames the
        finished file to its permanent name atomically.
        """
        return self._tracks_dir / f".{video_id}-{uuid.uuid4().hex[:10]}"

    def get_or_download(
        self,
        video_id: str,
        downloader: Callable[[Path], DownloadResult],
    ) -> Path | None:
        """Return the cached file for ``video_id``, downloading it when needed.

        ``downloader`` receives a bare target path inside the cache and must
        write the audio to ``<target>.<ext>``, returning where it landed and
        the track metadata. Concurrent callers for the same video wait for
        the active download and reuse its result instead of downloading
        again. Returns None when the download failed.
        """
        cached = self.path_for(video_id)
        if cached is not None:
            return cached
        if not self.claim_download(video_id):
            cached = self.path_for(video_id)
            if cached is not None:
                return cached
            if not self.claim_download(video_id):
                return self.path_for(video_id)
        ok = False
        target: Path | None = None
        try:
            target = self.tmp_path(video_id)
            result = downloader(target)
            path = self.commit(video_id, result.meta, src=result.path)
            ok = True
            return path if path.is_file() else None
        finally:
            if not ok and target is not None:
                self._remove_leftovers(target)
            self.finish_download(video_id)

    def claim_download(self, video_id: str) -> bool:
        """Block until any active download of ``video_id`` finishes, then claim.

        Returns True when this thread must perform the download itself, or
        False when another thread's download of the same video completed
        while this thread waited (check :meth:`path_for` again).
        """
        with self._lock:
            inflight = self._inflight.get(video_id)
            if inflight is None:
                self._inflight[video_id] = _InFlight()
                return True
        inflight.done.wait()
        return False

    def finish_download(self, video_id: str) -> None:
        """Release the download slot claimed with :meth:`claim_download`."""
        with self._lock:
            inflight = self._inflight.pop(video_id, None)
        if inflight is None:
            return
        inflight.done.set()

    def commit(
        self,
        video_id: str,
        meta: TrackMeta,
        src: str | os.PathLike[str],
    ) -> Path:
        """Atomically move a freshly downloaded file into the cache and index it."""
        src_path = Path(src)
        ext = meta.ext or src_path.suffix.lstrip(".") or "m4a"
        dest = self._tracks_dir / f"{video_id}.{ext}"
        os.replace(src_path, dest)
        now = time.time()
        with self._lock:
            self._entries[video_id] = {
                "title": meta.title or video_id,
                "artists": list(meta.artists),
                "duration": meta.duration,
                "ext": ext,
                "size": dest.stat().st_size,
                "added": now,
                "last_used": now,
            }
            self._dirty = True
            self._persist()
        self.evict()
        return dest

    def discard(self, video_id: str) -> None:
        """Remove ``video_id`` from the cache (file and index entry)."""
        with self._lock:
            entry = self._entries.pop(video_id, None)
            if entry is not None:
                self._drop(video_id, entry)
            else:
                for path in self._tracks_dir.glob(f"{video_id}.*"):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                self._dirty = True
            self._persist()

    def evict(self) -> None:
        """Evict entries over the size, count or age budgets, LRU first."""
        with self._lock:
            self._evict_ttl()
            self._evict_budget()
            self._persist()

    def clear(self) -> None:
        """Delete every cached track and reset the index."""
        with self._lock:
            for path in self._tracks_dir.iterdir():
                try:
                    path.unlink()
                except OSError:
                    pass
            self._entries.clear()
            self._dirty = True
            self._persist()

    def close(self) -> None:
        """Flush any pending index changes to disk and close the database."""
        with self._lock:
            self._persist()
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _load(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._tracks_dir.mkdir(parents=True, exist_ok=True)
        conn = self._open_database()
        self._conn = conn
        for row in conn.execute("SELECT * FROM tracks"):
            entry = self._entry_from_row(row)
            if entry is None:
                continue
            path = self._tracks_dir / f"{row['video_id']}.{entry['ext']}"
            if not path.is_file():
                self._dirty = True
                continue
            self._entries[row["video_id"]] = entry
        for path in self._tracks_dir.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            video_id, dot, ext = path.name.rpartition(".")
            if not dot or video_id in self._entries:
                continue
            self._entries[video_id] = {
                "title": video_id,
                "artists": [],
                "duration": None,
                "ext": ext,
                "size": path.stat().st_size,
                "added": time.time(),
                "last_used": time.time(),
            }
            self._dirty = True
        self.evict()

    def _open_database(self) -> sqlite3.Connection:
        """Open the index database, rebuilding it when it is corrupt."""
        try:
            conn = open_db(self._db_path)
            ensure_schema(conn)
            conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
            return conn
        except sqlite3.DatabaseError:
            reset_database(self._db_path)
            conn = open_db(self._db_path)
            ensure_schema(conn)
            return conn

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> dict[str, Any] | None:
        ext = row["ext"]
        if not isinstance(ext, str) or not ext:
            return None
        try:
            artists = json.loads(row["artists"] or "[]")
        except ValueError:
            artists = []
        return {
            "title": row["title"] or row["video_id"],
            "artists": [a for a in artists if isinstance(a, str)],
            "duration": row["duration"],
            "ext": ext,
            "size": row["size"] or 0,
            "added": row["added"],
            "last_used": row["last_used"],
        }

    def _evict_ttl(self) -> None:
        if self.ttl is None:
            return
        cutoff = time.time() - self.ttl.total_seconds()
        for video_id, entry in list(self._entries.items()):
            if entry.get("last_used", 0) >= cutoff:
                continue
            self._drop(video_id, entry)

    def _evict_budget(self) -> None:
        if (
            len(self._entries) <= self.max_entries
            and self._total_size() <= self.max_size
        ):
            return
        for video_id, entry in sorted(
            self._entries.items(), key=lambda item: item[1]["last_used"]
        ):
            self._drop(video_id, entry)
            if (
                len(self._entries) <= self.max_entries
                and self._total_size() <= self.max_size
            ):
                break

    def _total_size(self) -> int:
        return sum(entry.get("size", 0) for entry in self._entries.values())

    def _drop(self, video_id: str, entry: dict[str, Any]) -> None:
        path = self._tracks_dir / f"{video_id}.{entry['ext']}"
        try:
            path.unlink()
        except OSError:
            pass
        self._entries.pop(video_id, None)
        self._dirty = True

    def _remove_leftovers(self, target: Path) -> None:
        for leftover in self._tracks_dir.glob(f"{target.name}.*"):
            try:
                leftover.unlink()
            except OSError:
                pass

    def _persist(self) -> None:
        if not self._dirty or self._conn is None:
            return
        self._dirty = False
        rows = [
            (
                video_id,
                entry["title"],
                json.dumps(list(entry.get("artists") or ())),
                entry.get("duration"),
                entry["ext"],
                entry.get("size") or 0,
                entry.get("added", time.time()),
                entry.get("last_used", time.time()),
            )
            for video_id, entry in self._entries.items()
        ]
        try:
            with self._conn:
                self._conn.execute("DELETE FROM tracks")
                self._conn.executemany(
                    "INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
                )
        except sqlite3.DatabaseError:
            # A write failure means the database is gone or corrupt; fall
            # back to keeping the in-memory index so playback still works.
            reset_database(self._db_path)
            conn = open_db(self._db_path)
            ensure_schema(conn)
            self._conn.close()
            self._conn = conn
            with self._conn:
                self._conn.execute("DELETE FROM tracks")
                self._conn.executemany(
                    "INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
                )
