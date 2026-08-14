"""Recently played tracks, newest first, under the library playlists."""

from __future__ import annotations

from textual.widgets import Label, ListItem, ListView

from music_cli.storage.state import PlayedTrack

from .library_tree import LibraryTree


class HistoryList(ListView):
    """Recently played tracks; Enter plays the highlighted one."""

    def on_mount(self) -> None:
        self.border_title = " HISTORY "
        self._tracks: list[PlayedTrack] = []

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
