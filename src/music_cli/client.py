"""High-level glue wiring search, stream extraction and playback together."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .player import (
    Cookies,
    MpvPlayer,
    PlayerError,
    PlaylistTrack,
    StreamExtractor,
    StreamInfo,
    WatchPlaylist,
)
from .search import SearchFilter, SearchResult, YTmusicSearch

PLAY_START_TIMEOUT = 6.0
PRIMARY_PLAY_ATTEMPTS = 2
FALLBACK_PLAYER_CLIENTS = ("web_safari", "web", "tv")


class MusicClient:
    """Wires search, stream resolution, playback and the autoplay queue.

    Owns the ``YTmusicSearch``, ``StreamExtractor``, ``WatchPlaylist`` and
    ``MpvPlayer`` instances and exposes the operations the UI needs:
    searching, playing search results or queued tracks, fetching the autoplay
    queue, and transport controls.
    """

    def __init__(
        self,
        *,
        cookies: Cookies | None = None,
        volume: int = 80,
        audio_output: str | None = None,
        on_track_end: Callable[[], None] | None = None,
        extractor_factory: Callable[..., StreamExtractor] = StreamExtractor,
    ) -> None:
        self._cookies = cookies
        self._extractor_factory = extractor_factory
        self.search_api = YTmusicSearch()
        self.extractor = extractor_factory(cookies)
        self.watch = WatchPlaylist(cookies=cookies)
        self.player = MpvPlayer(
            volume=volume,
            audio_output=audio_output,
            on_track_end=on_track_end,
        )
        self.queue: list[PlaylistTrack] = []
        self.current: StreamInfo | None = None
        self._in_flight: set[str] = set()
        self._play_lock = threading.Lock()

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filter: SearchFilter | None = None,
    ) -> list[SearchResult]:
        return self.search_api.search(query, limit=limit, filter=filter)

    def play_result(self, result: SearchResult) -> StreamInfo:
        """Resolve and start playing a search result."""
        self._play(result.video_id, result.title)
        return self.current  # type: ignore[return-value]

    def play_track(self, track: PlaylistTrack) -> StreamInfo:
        """Resolve and start playing a queued track."""
        self._play(track.video_id, track.title)
        return self.current  # type: ignore[return-value]

    def _play(self, video_id: str, title: str) -> None:
        """Resolve and start playing, retrying with fresh URLs and fallback clients.

        YouTube stream URLs occasionally reject playback (HTTP 403) even when
        freshly resolved; a fresh extraction, or a different player client,
        typically succeeds.

        Duplicate requests for the same video (for example a double click on
        the same row) are ignored: the same stream is never resolved twice at
        once, which would hammer YouTube and risk throttling playback.
        """
        if self.current is not None and self.current.video_id == video_id:
            return
        with self._play_lock:
            if video_id in self._in_flight:
                return
            self._in_flight.add(video_id)
        try:
            self._play_attempts(video_id)
        finally:
            self._in_flight.discard(video_id)

    def _play_attempts(self, video_id: str) -> None:
        last_error: PlayerError | None = None
        for client_name in (None, *FALLBACK_PLAYER_CLIENTS):
            attempts = PRIMARY_PLAY_ATTEMPTS if client_name is None else 1
            extractor = self.extractor
            if client_name is not None:
                extractor = self._extractor_factory(
                    self._cookies, player_client=client_name
                )
            for _ in range(attempts):
                try:
                    stream = extractor.resolve(video_id)
                except PlayerError as error:
                    last_error = error
                    continue
                self.player.play(stream)
                if self._playback_started():
                    self.current = stream
                    return
                last_error = PlayerError(f"Playback did not start for {video_id}")
        self.current = None
        raise last_error or PlayerError(f"Failed to play {video_id}")

    def _playback_started(self, timeout: float = PLAY_START_TIMEOUT) -> bool:
        """Whether playback is actually underway, failing fast on load errors."""
        deadline = time.monotonic() + timeout
        grace = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self.player.position > 0.5 or self.player.duration:
                return True
            if time.monotonic() > grace and not self.player.playing:
                return False
            time.sleep(0.2)
        return False

    def load_queue(self, video_id: str, *, radio: bool = False) -> list[PlaylistTrack]:
        """Fetch the watch playlist for ``video_id`` and keep the up-next tracks.

        The watch playlist starts with the current video; it is dropped so the
        queue only holds tracks that come after it.
        """
        tracks = self.watch.get(video_id, radio=radio)
        self.queue = [track for track in tracks if track.video_id != video_id]
        if not self.queue:
            self.queue = tracks
        return self.queue

    def next(self) -> PlaylistTrack | None:
        """Play the next queued track, or return ``None`` if the queue is empty."""
        if not self.queue:
            return None
        track = self.queue.pop(0)
        try:
            self.play_track(track)
        except PlayerError:
            self.queue.insert(0, track)
            raise
        return track

    def close(self) -> None:
        self.player.close()
