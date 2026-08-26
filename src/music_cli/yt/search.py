from __future__ import annotations

import re
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

    # Regex for artist: query syntax: artist:"name" rest / artist:'name' rest / artist:name rest
    _ARTIST_QUERY_RE = re.compile(
        r'^artist:\s*(?:"([^"]+)"|\'([^\']+)\'|(\S+))\s*(.*)$',
        re.IGNORECASE,
    )

    @classmethod
    def parse_artist_query(cls, query: str) -> tuple[str | None, str]:
        """Parse an ``artist:"name"`` prefix from a search query.

        Returns ``(artist_name, track_query)``. When no artist: prefix is
        found, ``artist_name`` is ``None`` and ``track_query`` is the whole
        query.

        Examples::

            artist:"Drake"           -> ("Drake", "")
            artist:'Drake' God's Plan -> ("Drake", "God's Plan")
            artist:Drake              -> ("Drake", "")
            artist:Drake God's Plan   -> ("Drake", "God's Plan")
        """
        m = cls._ARTIST_QUERY_RE.match(query)
        if m:
            name = m.group(1) or m.group(2) or m.group(3)
            rest = m.group(4).strip()
            return (name, rest)
        return (None, query)

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filter: SearchFilter | None = None,
    ) -> list[SearchResult]:
        raw = self._api.search(query=query, limit=limit, filter=filter)
        return [parse_search_result(item) for item in raw[:limit]]

    def get_artist_tracks(
        self, artist_browse_id: str, *, limit: int = 20
    ) -> list[SearchResult]:
        """Fetch an artist's top tracks by their browse ID.

        Uses ``get_artist`` for the top songs, then expands via
        ``get_playlist`` when the artist has a songs playlist browse ID.
        Returns :class:`SearchResult` rows that look like song results.
        """
        artist_data = self._api.get_artist(artist_browse_id)
        songs = artist_data.get("songs", {})
        results: list[dict[str, Any]] = list(songs.get("results", []) or [])

        browse_id = songs.get("browseId")
        if browse_id:
            playlist = self._api.get_playlist(browse_id, limit=limit)
            tracks = playlist.get("tracks", []) or []
            results = tracks[:limit]

        return [_parse_artist_track(item) for item in results[:limit]]

    def get_artist_tracks_by_name(
        self, name: str, *, limit: int = 20
    ) -> list[SearchResult]:
        """Find an artist by name and return their top tracks.

        Searches for the artist, then fetches their top songs.
        Raises :class:`ArtistNotFound` when no artist matches.
        """
        browse_id = find_artist_browse_id(self._api, name)
        return self.get_artist_tracks(browse_id, limit=limit)


class ArtistNotFound(LookupError):
    """Raised when no artist matches the given name."""


def _parse_artist_track(item: dict[str, Any]) -> SearchResult:
    """Convert an artist song / playlist track dict to a SearchResult.

    Handles two input formats:
    - ``get_artist`` songs (keys: ``artist`` str, ``album`` str)
    - ``get_playlist`` tracks (keys: ``artists`` list-of-dicts, ``album`` dict)
    """
    item = dict(item)
    item.setdefault("resultType", "song")
    return parse_search_result(item)


def find_artist_browse_id(api: YTMusic, name: str) -> str:
    """Search for an artist by name and return their browse ID.

    Raises :class:`ArtistNotFound` when no artist matches.
    """
    results = api.search(query=name, filter="artists", limit=5)
    for r in results:
        browse_id = r.get("browseId", "")
        if browse_id:
            return browse_id
        # Artist results from ytmusicapi carry the channel ID as browseId
        # only in the unfiltered search; filtered search may use channelId.
        channel_id = r.get("channelId", "")
        if channel_id:
            return channel_id
    raise ArtistNotFound(f"No artist found for “{name}”")
