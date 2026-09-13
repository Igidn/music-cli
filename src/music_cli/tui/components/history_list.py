"""Recently played tracks, newest first, under the library playlists."""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding
from textual.widgets import Label, ListItem, ListView

from music_cli.storage.state import PlayedTrack

from .library_tree import LibraryTree
from .messages import QueueAddRequested


class HistoryList(ListView):
    """Recently played tracks; Enter plays the highlighted one."""

    BINDINGS: ClassVar = [Binding("ctrl+n", "queue_add", "Queue next")]

    def on_mount(self) -> None:
        self.border_title = " HISTORY "
        self._tracks: list[PlayedTrack] = []
        self._last_can_add = False

    def set_tracks(self, tracks: list[PlayedTrack]) -> None:
        self._tracks = list(tracks)
        self.clear()
        if not tracks:
            self.index = None
            self.append(ListItem(Label("Nothing played yet", classes="queue-empty")))
            return
        for track in tracks:
            self.append(self._item(track))
        self.index = 0

    def track_at(self, index: int | None) -> PlayedTrack | None:
        if index is None or not 0 <= index < len(self._tracks):
            return None
        return self._tracks[index]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "queue_add":
            return self.track_at(self.index) is not None
        return super().check_action(action, parameters)

    def action_queue_add(self) -> None:
        track = self.track_at(self.index)
        if track is not None:
            self.post_message(QueueAddRequested(track.video_id, track.title))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # refresh_bindings() recomposes the whole footer — too costly per
        # arrow-key repeat. The footer only cares whether ctrl+n applies to
        # the highlighted row, so refresh only when that flips.
        can_add = self.track_at(self.index) is not None
        if can_add != self._last_can_add:
            self._last_can_add = can_add
            self.refresh_bindings()

    def action_cursor_up(self) -> None:
        if self.index in (None, 0):
            self.app.query_one(LibraryTree).focus()
        else:
            super().action_cursor_up()

    @staticmethod
    def _item(track: PlayedTrack) -> ListItem:
        subtitle = " • ".join(track.artists) or "Unknown artist"
        return ListItem(
            Label(track.title, classes="queue-title"),
            Label(subtitle, classes="queue-subtitle"),
        )
