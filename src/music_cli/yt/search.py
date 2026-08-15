from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ytmusicapi import YTMusic

SearchFilter = Literal[
    "songs",
    "videos",
    "albums",
    "artists",
    "playlists",
    "community_playlists",
    "featured_playlists",
    "profiles",
    "podcasts",
    "episodes",
]

TYPE_LABELS = {
    "song": "SONG",
    "video": "VIDEO",
    "album": "ALBUM",
    "artist": "ARTIST",
    "playlist": "PLAYLIST",
    "profile": "PROFILE",
    "podcast": "PODCAST",
    "episode": "EPISODE",
}


def format_duration(raw: Any) -> str:
    """Seconds -> "m:ss"/"h:mm:ss"; strings pass through; junk -> "" ("--:--" up to caller)."""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (int, float)) or raw < 0:
        return ""
    total = int(raw)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _artist_names(raw: Any) -> list[str]:
    """Normalise a ytmusicapi `artist(s)`-style value (str/dict/list) to names."""
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, dict):
        return [raw["name"]] if raw.get("name") else []
    if isinstance(raw, list):
        names = [a.get("name") if isinstance(a, dict) else a for a in raw]
        # yt-dlp sometimes repeats an artist within `artists`; dedupe in order.
        return list(dict.fromkeys(n for n in names if n))
    return []


def parse_artists(info: dict[str, Any]) -> list[str]:
    """Artist names from list-of-dicts (ytmusicapi) or list-of-str (yt-dlp).

    Artist-type search results carry the name under the singular ``artist``
    key (a plain string from ytmusicapi), so that key is checked too.
    """
    for key in ("artists", "artist"):
        if artists := _artist_names(info.get(key)):
            return artists
    for key in ("author", "creator", "uploader"):
        val = info.get(key)
        if isinstance(val, dict) and val.get("name"):
            return [val["name"]]
        if isinstance(val, str) and val:
            return [val]
    return []


def _parse_album(result: dict[str, Any]) -> str:
    album = result.get("album")
    if isinstance(album, dict):
        return album.get("name", "")
    if isinstance(album, str):
        return album
    return ""


def parse_search_result(result: dict[str, Any]) -> SearchResult:
    duration = format_duration(result.get("duration"))
    if not duration:
        duration = format_duration(result.get("duration_seconds"))
    year = result.get("year")
    artists = parse_artists(result)
    # Artist-type rows come back from ytmusicapi with no `title`; the name
    # lives under `artist` (filtered search) or as the first parsed artist
    # (unfiltered search's top result). Without this the row shows "Unknown".
    title = result.get("title")
    if not title and result.get("resultType") == "artist":
        title = artists[0] if artists else None
    return SearchResult(
        result_type=result.get("resultType", "unknown"),
        title=title or "Unknown",
        artists=artists,
        album=_parse_album(result),
        duration=duration,
        video_id=result.get("videoId") or "",
        browse_id=result.get("browseId") or "",
        year=str(year) if year else "",
        raw=result,
    )


@dataclass(frozen=True)
class SearchResult:
    result_type: str
    title: str
    artists: list[str]
    album: str
    duration: str
    video_id: str
    browse_id: str
    year: str
    raw: dict[str, Any] = field(repr=False)

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.result_type, self.result_type.upper())

    @property
    def subtitle(self) -> str:
        parts = [", ".join(self.artists)]
        if self.album:
            parts.append(self.album)
        if self.year:
            parts.append(self.year)
        return " • ".join(p for p in parts if p)


class YTmusicSearch:
    def __init__(self, api: YTMusic | None = None) -> None:
        self._api = api or YTMusic()

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filter: SearchFilter | None = None,
    ) -> list[SearchResult]:
        raw = self._api.search(query=query, limit=limit, filter=filter)
        return [parse_search_result(item) for item in raw[:limit]]
