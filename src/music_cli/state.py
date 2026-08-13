"""Persistence for the last played track, so playback resumes across sessions.

The last track the user played is saved as an atomically written JSON file
under the config directory; on the next launch the TUI plays it again.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .login import config_dir

STATE_FILENAME = "last-track.json"


@dataclass(frozen=True)
class LastTrack:
    """A playable track identity, enough to resume it in a later session."""

    video_id: str
    title: str
    artists: tuple[str, ...] = ()
    duration: float | None = None


class LastTrackStore:
    """Atomically persists the last played track as JSON in the config dir."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else config_dir() / STATE_FILENAME

    def load(self) -> LastTrack | None:
        """The last played track, or None when nothing is saved or it's invalid."""
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        video_id = raw.get("video_id") if isinstance(raw, dict) else None
        if not isinstance(video_id, str) or not video_id:
            return None
        title = raw.get("title")
        artists = tuple(
            artist for artist in raw.get("artists") or () if isinstance(artist, str)
        )
        duration = raw.get("duration")
        return LastTrack(
            video_id=video_id,
            title=title if isinstance(title, str) and title else video_id,
            artists=artists,
            duration=duration
            if isinstance(duration, (int, float)) and duration > 0
            else None,
        )

    def save(self, track: LastTrack) -> None:
        """Persist ``track``; a crash never leaves a half-written file behind."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix="last-track.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(asdict(track), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise