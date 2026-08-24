"""High-level glue wiring search, stream extraction and playback together."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core.errors import PlayerError
from .player import create_player
from .storage.cache import AudioCache, DownloadResult, TrackMeta
from .storage.state import DownloadsStore
from .yt.cookies import Cookies
from .yt.extract import PlaylistTrack, StreamExtractor, StreamInfo, WatchPlaylist
from .yt.playlists import Library
from .yt.search import SearchFilter, SearchResult, YTmusicSearch

PLAY_START_TIMEOUT = 6.0
PRIMARY_PLAY_ATTEMPTS = 2
FALLBACK_PLAYER_CLIENTS = ("web_embedded", "web", "tv")


class MusicClient:
    """Wires search, stream resolution, playback and the autoplay queue.

    Owns the ``YTmusicSearch``, ``StreamExtractor``, ``WatchPlaylist`` and
    a platform-appropriate :func:`~.player.create_player` instance, and
    exposes the operations the UI needs: searching, playing search results
    or queued tracks, fetching the autoplay queue, and transport controls.
    """

    def __init__(
        self,
        *,
        cookies: Cookies | None = None,
        volume: int = 80,
        on_track_end: Callable[[], None] | None = None,
        extractor_factory: Callable[..., StreamExtractor] = StreamExtractor,
        cache: AudioCache | None = None,
        downloads: DownloadsStore | None = None,
    ) -> None:
        self._cookies = cookies
        self._extractor_factory = extractor_factory
        self.cache = cache or AudioCache()
        self.downloads = downloads or DownloadsStore()
        self.search_api = YTmusicSearch()
        self.extractor = extractor_factory(cookies)
        self.watch = WatchPlaylist(cookies=cookies)
        self.library = Library(cookies=cookies)
        self.queue: list[PlaylistTrack] = []
        self.current: StreamInfo | None = None
        self._playlist: list[PlaylistTrack] = []
        self._in_flight: set[str] = set()
        self._play_lock = threading.Lock()
        # Live download-progress hook, consulted per download. The daemon sets it
        # only after the client is built, so the player reads it via a getter.
        self._download_progress: Callable[[dict[str, Any]], None] | None = None
        self.player = create_player(
            volume=volume,
            on_track_end=on_track_end,
            cookies=cookies,
            cache=self.cache,
            download_progress=lambda: self._download_progress,
        )

    @property
    def on_track_end(self) -> Callable[[], None] | None:
        return self.player.on_track_end

    @on_track_end.setter
    def on_track_end(self, callback: Callable[[], None] | None) -> None:
        self.player.on_track_end = callback

    @property
    def loop(self) -> bool:
        return self.player.loop

    @loop.setter
    def loop(self, value: bool) -> None:
        self.player.loop = value

    @property
    def download_progress(self) -> Callable[[dict[str, Any]], None] | None:
        return self._download_progress

    @download_progress.setter
    def download_progress(self, hook: Callable[[dict[str, Any]], None] | None) -> None:
        self._download_progress = hook

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

    def play_video(self, video_id: str, title: str = "") -> StreamInfo:
        """Resolve and start playing a video by id, with no search context.

        Used to resume the last played track on startup.
        """
        self._play(video_id, title)
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
        self.player.stop()
        self.player.pump()
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
        if self.cache is not None:
            cached = self.cache.lookup(video_id)
            if cached is not None:
                stream = StreamInfo(
                    video_id=cached.video_id,
                    title=cached.title,
                    stream_url="",
                    artists=list(cached.artists),
                    duration=cached.duration,
                    ext=cached.ext,
                )
                self.player.play(stream)
                if self._playback_started():
                    self.current = stream
                    return
                # The cached file is broken or stale; drop it and fall
                # through to a fresh resolution and download.
                self.cache.discard(video_id)
                last_error = PlayerError(f"Playback did not start for {video_id}")
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

    def prefetch(self, video_id: str) -> bool:
        """Download ``video_id`` into the cache without playing it.

        Returns True when the track is cached afterwards. Best-effort:
        failures are swallowed. Downloads of the same video are shared with
        the player, so a prefetch never doubles up on a playback download.
        """
        if self.cache is None:
            return False

        def downloader(target: Path) -> DownloadResult:
            stream = self.extractor.resolve(video_id)
            filepath = self.extractor.download(
                video_id, str(target), progress_hook=self._download_progress
            )
            return DownloadResult(
                path=filepath,
                meta=TrackMeta(
                    title=stream.title,
                    artists=tuple(stream.artists),
                    duration=stream.duration,
                    ext=Path(filepath).suffix.lstrip("."),
                ),
            )

        try:
            path = self.cache.get_or_download(video_id, downloader)
        except PlayerError:
            return False
        return path is not None

    def download(self, video_id: str, progress=None) -> StreamInfo:
        """Download ``video_id`` for offline listening and record it.

        Wraps :meth:`prepare_playable` (network-only resolve + cache) then pins
        the cached file so eviction leaves it alone, and adds it to the
        downloads index. Safe to call from a worker thread. Already-downloaded
        tracks resolve from the cache instantly and are re-recorded.
        """
        stream = self.prepare_playable(video_id, progress=progress)
        if self.cache is not None:
            self.cache.pin(video_id)
        self.downloads.record(
            video_id, stream.title, tuple(stream.artists), stream.duration
        )
        return stream

    def remove_download(self, video_id: str) -> None:
        """Delete a downloaded track's audio and forget it in the index."""
        if self.cache is not None:
            self.cache.discard(video_id)
        self.downloads.remove(video_id)

    def prepare_playable(self, video_id: str, progress=None) -> StreamInfo:
        """Resolve ``video_id`` and download it into the cache. Network only.

        Returns a :class:`StreamInfo` whose audio is available locally (either
        the cached entry, or a freshly downloaded one). Never touches the AV
        player, so it is safe to run on a background thread while the serve
        loop keeps servicing other requests. Raises :class:`PlayerError` when
        no client can resolve and fetch the track.
        """
        last_error: PlayerError | None = None
        for client_name in (None, *FALLBACK_PLAYER_CLIENTS):
            try:
                return self._resolve_download(video_id, client_name, progress)
            except PlayerError as error:
                last_error = error
        raise last_error or PlayerError(f"Failed to play {video_id}")

    def _resolve_download(
        self, video_id: str, client_name: str | None, progress
    ) -> StreamInfo:
        """Resolve+download with one extractor; cached tracks need no network."""
        if client_name is None and self.cache is not None:
            cached = self.cache.lookup(video_id)
            if cached is not None:
                return StreamInfo(
                    video_id=cached.video_id,
                    title=cached.title,
                    stream_url="",
                    artists=list(cached.artists),
                    duration=cached.duration,
                    ext=cached.ext,
                )
        extractor = (
            self.extractor
            if client_name is None
            else self._extractor_factory(self._cookies, player_client=client_name)
        )
        stream = extractor.resolve(video_id)
        if self.cache is not None:
            hook = progress or self._download_progress

            def downloader(target: Path) -> DownloadResult:
                filepath = extractor.download(
                    video_id, str(target), progress_hook=hook
                )
                return DownloadResult(
                    path=filepath,
                    meta=TrackMeta(
                        title=stream.title,
                        artists=tuple(stream.artists),
                        duration=stream.duration,
                        ext=Path(filepath).suffix.lstrip("."),
                    ),
                )

            if self.cache.get_or_download(video_id, downloader) is None:
                raise PlayerError(f"Failed to download {video_id}")
        return stream

    def start_playable(self, stream: StreamInfo) -> bool:
        """Start AV playback of a **prepared** local stream.

        Must run on the daemon's main thread (it pumps the run loop). Returns
        True when playback actually started; a broken cached file is dropped.
        """
        self.player.stop()
        self.player.pump()
        self.player.play(stream)
        if self._playback_started():
            self.current = stream
            return True
        if self.cache is not None and not stream.stream_url:
            self.cache.discard(stream.video_id)
        return False

    def _playback_started(self, timeout: float = PLAY_START_TIMEOUT) -> bool:
        """Whether playback is actually underway, failing fast on load errors.

        A known duration alone does not count: after a track ends the player
        is paused, and if the next play request was dropped the new item can
        be loaded (duration known) while the rate stays at zero. Only report
        started when the position is advancing or the player is running.
        """
        deadline = time.monotonic() + timeout
        grace = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            # AVFoundation only advances the media pipeline while the main
            # run loop is serviced; pump it so playback can actually start
            # here (the daemon calls this on its main thread).
            self.player.pump()
            if self.player.position > 0.5:
                return True
            if self.player.duration and not self.player.paused:
                return True
            if self.player.eof_reached:
                return False
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
        self._playlist = []
        self.queue = [track for track in tracks if track.video_id != video_id]
        if not self.queue:
            self.queue = tracks
        return self.queue

    def play_from_playlist(self, playlist_id: str, start_index: int = 0) -> StreamInfo:
        """Play ``playlist_id`` from ``start_index``, queueing the remainder.

        The up-next queue becomes the rest of the playlist, so auto-next
        continues the playlist in order.
        """
        tracks = self.library.tracks(playlist_id)
        return self._play_tracklist(tracks, start_index, "Playlist is empty")

    def play_album(self, album_id: str, start_index: int = 0) -> StreamInfo:
        """Play ``album_id`` from ``start_index``, queueing the remainder.

        Album search results have no video id, only a browse id; the track
        list is fetched first and then played like a playlist, so auto-next
        continues the album in order.
        """
        tracks = self.library.album_tracks(album_id)
        return self._play_tracklist(tracks, start_index, "Album is empty")

    def _play_tracklist(
        self, tracks: list[PlaylistTrack], start_index: int, empty_error: str
    ) -> StreamInfo:
        """Play ``tracks`` from ``start_index``; the rest become up-next."""
        if not tracks:
            raise PlayerError(empty_error)
        if not 0 <= start_index < len(tracks):
            start_index = 0
        self._playlist = tracks
        self.queue = tracks[start_index + 1 :]
        self.play_track(tracks[start_index])
        return self.current  # type: ignore[return-value]

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

    def play_queue_track(self, index: int) -> StreamInfo:
        """Play the queued track at ``index``, popping it, re-inserting on failure.

        Mirrors :meth:`next`'s failure handling: a track that refuses to play
        goes back where it was so the queue stays intact.
        """
        if not 0 <= index < len(self.queue):
            raise PlayerError(f"No track at queue index {index}")
        track = self.queue.pop(index)
        try:
            stream = self.play_track(track)
        except PlayerError:
            self.queue.insert(index, track)
            raise
        return stream  # type: ignore[return-value]

    def loop_playlist(self) -> bool:
        """Re-queue the active playlist when auto-next exhausts it.

        Returns ``True`` when a playlist is active and its tracks have been
        re-queued for another pass; ``False`` when nothing to loop.
        """
        if not self._playlist:
            return False
        self.queue = list(self._playlist)
        return True

    def close(self) -> None:
        self.player.close()
        if self.cache is not None:
            self.cache.close()
        self.downloads.close()
