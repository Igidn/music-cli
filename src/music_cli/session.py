"""The shared playback engine used by the TUI and the headless daemon.

``PlaybackSession`` owns the auto-next engine (manual queue, then the
auto-generated queue, then playlist loop, then a radio refill) plus
play-history recording and settings persistence,
so both frontends behave identically and a single set of preferences
follows the player around.
"""

from __future__ import annotations

import threading

from .client import MusicClient
from .core.errors import PlayerError
from .storage.state import (
    SETTING_AUTO_NEXT,
    SETTING_LOOP,
    SETTING_MUTED,
    SETTING_VOLUME,
    DownloadedTrack,
    PlayedTrack,
    PlayHistoryStore,
    SettingsStore,
)
from .yt.extract import PlaylistTrack, StreamInfo

_ON_OFF = ("on", "off", "toggle")


def _load_up_next_safe(client: MusicClient, video_id: str) -> None:
    """Thread body for the up-next refresh; failures just leave it empty."""
    try:
        client.load_queue(video_id)
    except PlayerError:
        pass


class PlaybackSession:
    """Playback operations on top of ``MusicClient``, with state persisted.

    The client stays dumb (resolve, play, queue); this layer adds the
    policy: what to play when a track ends, what goes into the history,
    and which settings survive a restart.
    """

    def __init__(
        self,
        client: MusicClient,
        history_store: PlayHistoryStore | None = None,
        settings_store: SettingsStore | None = None,
    ) -> None:
        self.client = client
        self._history = (
            history_store if history_store is not None else PlayHistoryStore()
        )
        self._settings = (
            settings_store if settings_store is not None else SettingsStore()
        )
        self.last_video_id: str = ""
        # Set when a play started from the Downloads list: auto-next then walks
        # this snapshot of the Downloads list instead of the queue/radio.
        self._downloads_context: list[DownloadedTrack] | None = None
        # Position in that snapshot of the last played download, so a manually
        # queued track can interrupt the walk and it resumes right after.
        self._downloads_pos: int | None = None
        # Tracks the user queued by hand (TUI ctrl+n, `queue add`). Inserted at
        # the front so the last added plays first, popped before the auto
        # queue; the watch-playlist refreshes never touch this list.
        self._next_queue: list[PlaylistTrack] = []
        # Saved settings always win over the player's construction defaults.
        player = client.player
        player.volume = self._settings.get_int(SETTING_VOLUME, player.volume)
        player.muted = self._settings.get_bool(SETTING_MUTED)
        client.loop = self._settings.get_bool(SETTING_LOOP)
        self._auto_next = self._settings.get_bool(SETTING_AUTO_NEXT, True)

    @property
    def auto_next(self) -> bool:
        return self._auto_next

    @auto_next.setter
    def auto_next(self, value: bool) -> None:
        self._auto_next = value
        self._settings.set(SETTING_AUTO_NEXT, value)

    def play_query(self, query: str) -> StreamInfo:
        """Play the first playable search result for ``query``.

        Results without a video id (artists, playlists) cannot be played
        directly and are skipped.
        """
        self._downloads_context = None
        result = next((r for r in self.client.search(query) if r.video_id), None)
        if result is None:
            raise PlayerError(f"No playable results for {query!r}")
        stream = self.client.play_result(result)
        self._load_up_next(stream.video_id)
        self.record(stream)
        return stream

    def prepare_play(self, target: str, request: dict, progress=None) -> StreamInfo:
        """Network-only resolve+download for the daemon's async play path.

        Splits the slow network work (resolve, download into the cache) off the
        daemon's single event loop. Call on a worker thread; hand the result to
        :meth:`commit_play` on the main thread so AVFoundation playback starts
        where it can pump the run loop.

        A ``from_downloads`` request snapshots the Downloads list so playback
        stays inside it (see :meth:`play_video`); downloads are always cached,
        so this prepare returns instantly and there is no long gap where the
        up-next panel would show a stale context.
        """
        self._downloads_context = (
            self.client.downloads.recent() if request.get("from_downloads") else None
        )
        if target == "video_id":
            return self.client.prepare_playable(request["video_id"], progress=progress)
        if target == "query":
            result = next(
                (r for r in self.client.search(request["query"]) if r.video_id), None
            )
            if result is None:
                raise PlayerError(f"No playable results for {request['query']!r}")
            return self.client.prepare_playable(result.video_id, progress=progress)
        raise PlayerError(f"unsupported async play target: {target!r}")

    def commit_play(self, stream: StreamInfo) -> None:
        """Start playback of a prepared stream and apply session bookkeeping.

        Main thread only: start the AV player, refresh the up-next queue and
        record the play. Raises :class:`PlayerError` when playback doesn't start.
        """
        if not self.client.start_playable(stream):
            raise PlayerError(f"Playback did not start for {stream.video_id}")
        if self._downloads_context is None:
            self._load_up_next(stream.video_id)
        else:
            self._bookmark_download(stream.video_id)
        self.record(stream)

    def play_video(
        self,
        video_id: str,
        title: str = "",
        *,
        from_download: bool = False,
    ) -> StreamInfo:
        """Play ``video_id``; ``from_download`` keeps auto-next inside Downloads.

        Playing from the Downloads list snapshots the list order so each
        auto-advance (and manual next) stays in that list rather than falling
        into the queue/radio refill (see :meth:`next_track`). Any other play
        clears the context.
        """
        self._downloads_context = (
            self.client.downloads.recent() if from_download else None
        )
        stream = self.client.play_video(video_id, title)
        if from_download:
            # The downloads walk is driven by the snapshot alone: a watch
            # playlist fetch here would be wasted network and its result is
            # never read while the context is active.
            self._bookmark_download(video_id)
        else:
            self._load_up_next(video_id)
        self.record(stream)
        return stream

    def play_download(self, video_id: str, title: str = "") -> StreamInfo:
        """Play a track from the Downloads list; auto-next stays in that list."""
        return self.play_video(video_id, title, from_download=True)

    def play_queue_track(self, index: int) -> StreamInfo:
        """Play the track shown at ``index`` in the Up-Next panel and record it.

        ``index`` addresses the merged view :meth:`queue` returns: manually
        queued tracks first, then the remaining downloads (inside the
        Downloads context) or the auto queue. A failed play re-inserts the
        track where it was, like the client does for its own queue.
        """
        manual = len(self._next_queue)
        if self._downloads_context:
            up_next = self._downloads_up_next()
            if not 0 <= index < manual + len(up_next):
                raise PlayerError(f"No track at queue index {index}")
            if index < manual:
                return self._play_manual_at(index)
            target = up_next[index - manual]
            return self.play_download(target.video_id, target.title)
        if not 0 <= index < manual + len(self.client.queue):
            raise PlayerError(f"No track at queue index {index}")
        if index < manual:
            return self._play_manual_at(index)
        self._downloads_context = None
        stream = self.client.play_queue_track(index - manual)
        self.record(stream)
        return stream

    def queue_add(
        self,
        *,
        video_id: str = "",
        title: str = "",
        query: str = "",
    ) -> dict:
        """Put a track at the front of the manual queue; last added plays first.

        Accepts a resolved ``video_id`` (``title`` is the display name and may
        be empty) or a ``query``, resolved through search like
        :meth:`play_query`. The manual queue outlives every watch-playlist
        refresh and plays before the auto queue. Returns the added track in
        the :meth:`queue` entry shape.
        """
        if bool(video_id) == bool(query):
            raise PlayerError("queue add needs a query or a video id, not both")
        artists: list[str] = []
        duration = ""
        if query:
            result = next((r for r in self.client.search(query) if r.video_id), None)
            if result is None:
                raise PlayerError(f"No playable results for {query!r}")
            video_id, title = result.video_id, result.title
            artists, duration = list(result.artists), result.duration
        track = PlaylistTrack(
            video_id=video_id,
            title=title,
            artists=artists,
            duration=duration,
        )
        self._next_queue.insert(0, track)
        return self._queue_entry(track)

    def _play_manual_at(self, index: int) -> StreamInfo:
        """Play and drop the manually queued track at ``index``.

        A track that refuses to play goes back where it was, so the manual
        queue stays intact — same failure handling as the client's queue.
        """
        self._pop_manual(index)
        return self.client.current  # type: ignore[return-value]

    def _pop_manual(self, index: int) -> PlaylistTrack:
        track = self._next_queue.pop(index)
        try:
            self.client.play_track(track)
        except PlayerError:
            self._next_queue.insert(index, track)
            raise
        self.record(self.client.current)  # type: ignore[arg-type]
        return track

    def play_playlist(self, playlist_id: str, start_index: int = 0) -> StreamInfo:
        """Play a playlist from ``start_index``; the client queues the remainder."""
        self._downloads_context = None
        stream = self.client.play_from_playlist(playlist_id, start_index)
        self.record(stream)
        return stream

    def play_album(self, album_id: str, start_index: int = 0) -> StreamInfo:
        """Play an album from ``start_index``; the client queues the remainder."""
        self._downloads_context = None
        stream = self.client.play_album(album_id, start_index)
        self.record(stream)
        return stream

    def stop(self) -> None:
        """Stop playback and drop the queue/current track.

        Keeps the daemon (and the history) alive; ``quit`` is the shutdown
        analogue.
        """
        self.client.player.stop()
        self.client.current = None
        self.client.queue = []
        self._next_queue = []

    def resume_last(self) -> StreamInfo | None:
        """Replay the most recent history entry, if any."""
        track = self._history.most_recent()
        if track is None:
            return None
        return self.play_video(track.video_id, track.title)

    def record(self, stream: StreamInfo) -> None:
        """Persist one play into history and remember it for radio refills."""
        self._history.record(
            PlayedTrack(
                video_id=stream.video_id,
                title=stream.title,
                artists=tuple(stream.artists),
                duration=stream.duration,
            )
        )
        self.last_video_id = stream.video_id
        self._prefetch_next()

    def remove_download(self, video_id: str) -> None:
        """Delete one offline download (audio file and its index entry)."""
        self.client.remove_download(video_id)

    def _prefetch_next(self) -> None:
        """Download the next up-next track ahead of auto-advance, if used.

        Only the immediate N+1 track (``queue[0]``) is fetched, never the
        whole queue, and only while auto-next is on. Runs in a background
        thread so the download never blocks playback. Best-effort:
        :meth:`client.prefetch` swallows failures.
        """
        # Loop mode replays the current track on end; it never advances to
        # queue[0], so a speculative download there would be wasted network.
        if not self.auto_next or self.client.loop:
            return
        if self._next_queue:
            target = self._next_queue[0].video_id
        elif self._downloads_context or not self.client.queue:
            # Inside Downloads every candidate is already cached locally.
            return
        else:
            target = self.client.queue[0].video_id
        threading.Thread(
            target=self.client.prefetch, args=(target,), daemon=True, name="prefetch"
        ).start()

    def _load_up_next(self, video_id: str) -> None:
        """Refresh the up-next queue off the daemon thread.

        ``load_queue`` is a slow network call; running it inline on the daemon's
        single event loop would stall every other request (a cached play, status,
        next) behind it for the whole fetch. Spawn it instead, exactly like
        :meth:`_prefetch_next`; a fetch failure just leaves the queue empty.
        """
        threading.Thread(
            target=_load_up_next_safe,
            args=(self.client, video_id),
            daemon=True,
            name="up-next",
        ).start()

    def on_track_end(self) -> None:
        self.client.current = None
        if self.auto_next:
            self.next_track()

    def next_track(self) -> PlaylistTrack | None:
        """Play the next track, refilling the queue when it runs dry.

        A manually queued track (see :meth:`queue_add`) always plays first;
        it may interrupt a Downloads walk, which then resumes where it left
        off. Playing from the Downloads list wraps to the first track at the
        end, so the list plays continuously like a normal playlist. Otherwise
        the active playlist loops, then a radio refill off the last played
        track. Returns ``None`` when there is nothing to play anywhere;
        stream-resolution errors propagate.
        """
        client = self.client
        if self._next_queue:
            return self._pop_manual(0)
        if self._downloads_context:
            return self._next_download()
        if not client.queue:
            client.loop_playlist()
        if not client.queue and self.last_video_id:
            try:
                client.load_queue(self.last_video_id, radio=True)
            except PlayerError:
                pass
        if not client.queue:
            return None
        track = client.next()
        self.record(self.client.current)  # type: ignore[arg-type]
        return track

    def _next_download(self) -> PlaylistTrack | None:
        """Play the next track in the Downloads snapshot, wrapping to the first
        at the end so the list plays continuously (like a normal playlist).

        Walks from ``_downloads_pos``, the snapshot index of the last played
        download, so a manually queued track (never a member of the snapshot)
        can interrupt the walk without losing the position. Without a
        bookmark, locate the currently playing track; on natural track end
        :meth:`on_track_end` clears ``client.current`` before this runs, so
        fall back to ``last_video_id``.
        """
        tracks = self._downloads_context or ()
        if not tracks:
            self._downloads_context = None
            return None
        if self._downloads_pos is None:
            current = self.client.current
            current_vid = (
                current.video_id if current is not None else self.last_video_id
            )
            self._downloads_pos = next(
                (i for i, track in enumerate(tracks) if track.video_id == current_vid),
                None,
            )
        if self._downloads_pos is None:
            # No bookmark and the current track isn't in the snapshot; stop.
            self._downloads_context = None
            return None
        nxt = tracks[(self._downloads_pos + 1) % len(tracks)]
        return self.play_video(nxt.video_id, nxt.title, from_download=True)

    def _bookmark_download(self, video_id: str) -> None:
        """Remember where the Downloads walk is, so it can resume after a
        manually queued track interrupts it."""
        for i, track in enumerate(self._downloads_context or ()):
            if track.video_id == video_id:
                self._downloads_pos = i
                return

    def pause(self) -> None:
        self.client.player.pause()

    def resume(self) -> None:
        self.client.player.resume()

    def toggle(self) -> None:
        if self.client.current is None:
            # Nothing loaded: resume the last history track, same as `resume`.
            if self.resume_last() is None:
                return  # nothing to play; stays idle
            return
        self.client.player.toggle()

    def seek(
        self, *, offset: float | None = None, position: float | None = None
    ) -> None:
        """Seek absolutely or relatively; a no-op with nothing loaded."""
        if self.client.current is None:
            return
        if position is not None:
            self.client.player.seek(position)
        elif offset is not None:
            self.client.player.seek_relative(offset)

    def set_volume(self, *, volume: int | None = None, delta: int | None = None) -> int:
        """Set or bump the volume (the player clamps to 0-100); persists."""
        player = self.client.player
        if volume is not None:
            player.volume = volume
        elif delta is not None:
            player.volume = player.volume + delta
        self._settings.set(SETTING_VOLUME, player.volume)
        return player.volume

    def set_muted(self, state: str) -> bool:
        value = self._resolve_state(state, self.client.player.muted)
        self.client.player.muted = value
        self._settings.set(SETTING_MUTED, value)
        return value

    def set_loop(self, state: str) -> bool:
        value = self._resolve_state(state, self.client.loop)
        self.client.loop = value
        self._settings.set(SETTING_LOOP, value)
        return value

    def set_auto_next(self, state: str) -> bool:
        value = self._resolve_state(state, self.auto_next)
        self.auto_next = value
        return value

    @staticmethod
    def _resolve_state(state: str, current: bool) -> bool:
        """Map an on/off/toggle word onto a new boolean value."""
        if state not in _ON_OFF:
            raise ValueError(
                f"Invalid state {state!r}: expected 'on', 'off' or 'toggle'"
            )
        return not current if state == "toggle" else state == "on"

    def status(self) -> dict:
        """A JSON-serializable snapshot for IPC clients."""
        player = self.client.player
        current = self.client.current
        return {
            "state": "stopped"
            if current is None
            else "paused"
            if player.paused
            else "playing",
            "track": {
                "video_id": current.video_id,
                "title": current.title,
                "artists": list(current.artists),
                "duration": current.duration,
            }
            if current is not None
            else None,
            "position": player.position,
            "duration": player.duration,
            "volume": player.volume,
            "muted": player.muted,
            "loop": self.client.loop,
            "auto_next": self.auto_next,
            "queue": self.queue(),
        }

    def queue(self) -> list[dict]:
        """The Up-Next view: manual queue first, then the auto queue."""
        if self._downloads_context:
            source: list[PlaylistTrack | DownloadedTrack] = [
                *self._next_queue,
                *self._downloads_up_next(),
            ]
        else:
            source = [*self._next_queue, *self.client.queue]
        return [self._queue_entry(track) for track in source]

    def _downloads_up_next(self) -> list[DownloadedTrack]:
        """The remaining tracks of the active Downloads list.

        Mirrors the order :meth:`_next_download` walks (from the bookmark, or
        the currently playing track before the first bookmark), so the
        Up-Next panel shows exactly what auto-advance will play.
        """
        tracks = list(self._downloads_context or ())
        pos = self._downloads_pos
        if pos is None:
            current = self.client.current
            if current is not None:
                pos = next(
                    (
                        i
                        for i, track in enumerate(tracks)
                        if track.video_id == current.video_id
                    ),
                    None,
                )
        if pos is not None:
            return tracks[pos + 1 :]
        return tracks

    @staticmethod
    def _queue_entry(track: PlaylistTrack | DownloadedTrack) -> dict:
        return {
            "video_id": track.video_id,
            "title": track.title,
            "artists": list(track.artists),
            "duration": track.duration,
        }

    def close(self) -> None:
        self.client.close()
        self._history.close()
        self._settings.close()
