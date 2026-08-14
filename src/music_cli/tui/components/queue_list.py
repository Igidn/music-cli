"""Up-next queue; each item is one row of the queue."""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding
from textual.widgets import Label, ListItem, ListView, Select

from music_cli.yt.extract import PlaylistTrack

from .messages import AddToPlaylistRequested


class QueueList(ListView):
    """Up-next queue; each item is one row of the queue."""

    BINDINGS: ClassVar = [Binding("s", "add_to_playlist", "Add to playlist")]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._tracks: list[PlaylistTrack] = []
        self._last_can_add = False

    def on_mount(self) -> None:
        self.border_title = " UP NEXT "

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "add_to_playlist":
            return (
                self.app.client.library.authenticated
                and self.track_at(self.index) is not None
            )
        return super().check_action(action, parameters)

    def action_add_to_playlist(self) -> None:
        track = self.track_at(self.index)
        if track is not None:
            self.post_message(
                AddToPlaylistRequested(
                    track.video_id, track.title, tuple(track.artists)
                )
            )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # refresh_bindings() recomposes the whole footer — too costly per
        # arrow-key repeat. The footer only cares whether `s` applies to the
        # highlighted row, so refresh only when that flips.
        can_add = self.track_at(self.index) is not None
        if can_add != self._last_can_add:
            self._last_can_add = can_add
            self.refresh_bindings()

    def action_cursor_up(self) -> None:
        if self.index in (None, 0):
            self.app.query_one("#filter-select", Select).focus()
        else:
            super().action_cursor_up()

    def set_tracks(self, tracks: list[PlaylistTrack]) -> None:
        self._tracks = list(tracks)
        self.clear()
        if not tracks:
            self.index = None
            self.append(
                ListItem(
                    Label(
                        "Queue is empty — play a song to start autoplay",
                        classes="queue-empty",
                    )
                )
            )
            return
        for track in tracks:
            self.append(self._item(track))
        # The queue pops its head the moment that track starts playing, so a
        # refresh often leaves the cursor parked (index 0). Setting the index
        # synchronously races the async mount of the new rows and highlights a
        # stale row that is about to be removed, leaving nothing selected. Re-set
        # it after the rebuilt rows are mounted.
        self.index = 0
        self.call_after_refresh(self._highlight_head)
        self.refresh_bindings()

    def _highlight_head(self) -> None:
        if not self._tracks:
            return
        # index may already be 0 (set on a stale row), so the reactive setter
        # would dedup and skip re-highlighting the freshly mounted row. Force a
        # re-validation exactly like ListView.pop/remove do.
        old_index = self.index
        self.index = self.index
        if old_index == self.index:
            self.watch_index(old_index, self.index)

    def track_at(self, index: int | None) -> PlaylistTrack | None:
        """The track the user sees at ``index``, independent of queue mutations.

        The queue is popped as soon as a track is picked, so the list widget
        (rebuilt only on refresh) is the source of truth for what the user
        actually clicked; this is what makes rapid double clicks collapse into
        one request instead of picking whatever slid into the row.
        """
        if index is None or not 0 <= index < len(self._tracks):
            return None
        return self._tracks[index]

    @staticmethod
    def _item(track: PlaylistTrack) -> ListItem:
        subtitle = " • ".join(track.artists)
        if track.duration:
            subtitle = f"{subtitle} · {track.duration}" if subtitle else track.duration
        if not subtitle:
            subtitle = "Unknown artist"
        return ListItem(
            Label(track.title, classes="queue-title"),
            Label(subtitle, classes="queue-subtitle"),
        )
