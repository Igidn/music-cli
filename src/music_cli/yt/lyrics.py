"""Lyrics from lrclib.net: synced (timestamp, line) pairs plus plain text."""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

_LRCLIB_GET = "https://lrclib.net/api/get"
_TIMESTAMP = re.compile(r"\[(\d+):(\d{1,2})(?:\.(\d{1,3}))?\]")
_TIMEOUT = 5.0


@dataclass(frozen=True)
class Lyrics:
    """Lyrics for one track.

    ``synced`` is a list of (start_seconds, line) pairs, ordered by start;
    ``plain`` is the same lyrics without timestamps, or None when lrclib
    has no entry at all.
    """

    synced: list[tuple[float, str]]
    plain: str | None


def fetch_lyrics(
    title: str, artists: tuple[str, ...], duration: float | None
) -> Lyrics:
    """Return lrclib's lyrics for the track, or an empty result when there are none."""
    params = {
        "track_name": title,
        "artist_name": ", ".join(artists),
    }
    if duration:
        params["duration"] = int(duration)
    try:
        response = requests.get(_LRCLIB_GET, params=params, timeout=_TIMEOUT)
    except requests.RequestException:
        return Lyrics(synced=[], plain=None)
    if response.status_code != 200:
        return Lyrics(synced=[], plain=None)
    try:
        data = response.json()
    except ValueError:
        return Lyrics(synced=[], plain=None)
    lrc = data.get("syncedLyrics")
    plain = data.get("plainLyrics")
    return Lyrics(
        synced=parse_lrc(lrc) if isinstance(lrc, str) and lrc else [],
        plain=plain.strip() if isinstance(plain, str) and plain.strip() else None,
    )


def fetch_synced_lyrics(
    title: str, artists: tuple[str, ...], duration: float | None
) -> list[tuple[float, str]]:
    """Return lrclib's synced lyrics for the track, or [] when there are none."""
    return fetch_lyrics(title, artists, duration).synced


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """Parse an LRC string into [(start_seconds, line), ...], oldest first."""
    lines: list[tuple[float, str]] = []
    for raw in lrc.splitlines():
        text = raw.strip()
        starts = []
        for m in _TIMESTAMP.finditer(text):
            frac = m.group(3)
            ms = int((frac + "00")[:3]) / 1000 if frac else 0.0
            starts.append(int(m.group(1)) * 60 + int(m.group(2)) + ms)
        if not starts:
            continue
        text = _TIMESTAMP.sub("", text).strip()
        for start in starts:
            lines.append((float(start), text))
    lines.sort(key=lambda kv: kv[0])
    return lines
