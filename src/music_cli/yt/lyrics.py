"""Time-synced lyrics from lrclib.net, as (timestamp, line) pairs."""

from __future__ import annotations

import re

import requests

_LRCLIB_GET = "https://lrclib.net/api/get"
_TIMESTAMP = re.compile(r"\[(\d+):(\d{1,2})(?:\.(\d{1,3}))?\]")
_TIMEOUT = 5.0


def fetch_synced_lyrics(
    title: str, artists: tuple[str, ...], duration: float | None
) -> list[tuple[float, str]]:
    """Return lrclib's synced lyrics for the track, or [] when there are none."""
    params = {
        "track_name": title,
        "artist_name": ", ".join(artists),
    }
    if duration:
        params["duration"] = int(duration)
    try:
        response = requests.get(_LRCLIB_GET, params=params, timeout=_TIMEOUT)
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        lrc = response.json().get("syncedLyrics")
    except ValueError:
        return []
    return parse_lrc(lrc) if lrc else []


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """Parse an LRC string into [(start_seconds, line), ...], oldest first."""
    lines: list[tuple[float, str]] = []
    for raw in lrc.splitlines():
        text = raw.strip()
        starts = [
            int(m.group(1)) * 60 + int(m.group(2)) for m in _TIMESTAMP.finditer(text)
        ]
        if not starts:
            continue
        text = _TIMESTAMP.sub("", text).strip()
        for start in starts:
            lines.append((float(start), text))
    lines.sort(key=lambda kv: kv[0])
    return lines
