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

def _format_duration(raw: Any) -> str:
    if isinstance(raw, int):
        minutes, seconds = divmod(raw, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"
    if isinstance(raw, str):
        return raw
    return ""


def _parse_artists(result: dict[str, Any]) -> list[str]:
    artists = [
        artist["name"]
        for artist in (result.get("artists") or [])
        if isinstance(artist, dict) and artist.get("name")
    ]
    if artists:
        return artists
    author = result.get("author")
    if isinstance(author, dict) and author.get("name"):
        return [author["name"]]
    if isinstance(author, str) and author:
        return [author]
    return []


def _parse_album(result: dict[str, Any]) -> str:
    album = result.get("album")
    if isinstance(album, dict):
        return album.get("name", "")
    if isinstance(album, str):
        return album
    return ""


def parse_search_result(result: dict[str, Any]) -> SearchResult:
    duration = _format_duration(result.get("duration"))
    if not duration:
        duration = _format_duration(result.get("duration_seconds"))
    year = result.get("year")
    return SearchResult(
        result_type=result.get("resultType", "unknown"),
        title=result.get("title") or "Unknown",
        artists=_parse_artists(result),
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
