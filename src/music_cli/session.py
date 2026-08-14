"""The shared playback engine used by the TUI and the headless daemon.

``PlaybackSession`` owns the auto-next engine (queue, then playlist loop,
then a radio refill) plus play-history recording and settings persistence,
so both frontends behave identically and a single set of preferences
follows the player around.
"""

from __future__ import annotations

from .client import MusicClient
from .core.errors import PlayerError
from .storage.state import (
    SETTING_AUTO_NEXT,
    SETTING_LOOP,
    SETTING_MUTED,
    SETTING_VOLUME,
    PlayedTrack,
    PlayHistoryStore,
    SettingsStore,
)
from .yt.extract import PlaylistTrack, StreamInfo

_ON_OFF = ("on", "off", "toggle")


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
        result = next((r for r in self.client.search(query) if r.video_id), None)
        if result is None:
            raise PlayerError(f"No playable results for {query!r}")
        stream = self.client.play_result(result)
        self.record(stream)
        self._load_up_next(stream.video_id)
        return stream

    def play_video(self, video_id: str, title: str = "") -> StreamInfo:
        stream = self.client.play_video(video_id, title)
        self.record(stream)
        self._load_up_next(video_id)
        return stream

    def play_playlist(self, playlist_id: str) -> StreamInfo:
        """Play a playlist from the top; the client queues the remainder."""
        stream = self.client.play_from_playlist(playlist_id, 0)
        self.record(stream)
        return stream

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

    def _load_up_next(self, video_id: str) -> None:
        """Refresh the up-next queue; a fetch failure just leaves it empty."""
        try:
            self.client.load_queue(video_id)
        except PlayerError:
            pass

    def on_track_end(self) -> None:
        self.client.current = None
        if self.auto_next:
            self.next_track()

    def next_track(self) -> PlaylistTrack | None:
        """Play the next track, refilling the queue when it runs dry.

        Order of fallback: the active playlist loops, then a radio refill
        off the last played track. Returns ``None`` when there is nothing
        to play anywhere; stream-resolution errors propagate.
        """
        client = self.client
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

    def pause(self) -> None:
        self.client.player.pause()

    def resume(self) -> None:
        self.client.player.resume()

    def toggle(self) -> None:
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
        return [
            {
                "video_id": track.video_id,
                "title": track.title,
                "artists": list(track.artists),
                "duration": track.duration,
            }
            for track in self.client.queue
        ]

    def close(self) -> None:
        self.client.close()
        self._history.close()
        self._settings.close()
