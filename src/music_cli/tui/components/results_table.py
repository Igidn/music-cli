"""Search results table with per-row search-result lookup."""

from __future__ import annotations

from typing import ClassVar

from rich.cells import cell_len, set_cell_size
from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Input

from music_cli.yt.search import SearchResult

from .messages import AddToPlaylistRequested

TYPE_COLORS = {
    "SONG": "#a78bfa",
    "VIDEO": "#67e8f9",
    "ALBUM": "#fbbf24",
    "ARTIST": "#f472b6",
    "PLAYLIST": "#34d399",
    "PROFILE": "#94a3b8",
    "PODCAST": "#fb923c",
    "EPISODE": "#fb923c",
}


class ResultsTable(DataTable, inherit_bindings=False):
    """Search results table with per-row search-result lookup."""

    BINDINGS: ClassVar = [
        Binding("enter", "select_cursor", "Select", show=False),
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("home", "scroll_home", "Home", show=False),
        Binding("end", "scroll_end", "End", show=False),
        Binding("ctrl+home", "scroll_top", "Top", show=False),
        Binding("ctrl+end", "scroll_bottom", "Bottom", show=False),
        Binding("s", "add_to_playlist", "Add to playlist"),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._results: dict[str, SearchResult] = {}
        self._last_can_add = False

    def action_cursor_up(self) -> None:
        if not self.row_count or self.cursor_coordinate.row == 0:
            self.app.query_one("#search-input", Input).focus()
        else:
            super().action_cursor_up()

    def on_mount(self) -> None:
        self.add_column("", key="type", width=10)
        self.add_column("Title", key="title", width=34)
        self.add_column("Artist", key="artist", width=13)
        self.add_column("Album", key="album", width=8)
        self.add_column("Time", key="duration", width=7)

    def set_results(self, results: list[SearchResult]) -> None:
        self.clear()
        self._results.clear()
        for result in results:
            key = result.video_id or result.browse_id
            if not key:
                continue
            artists = ", ".join(result.artists)
            self.add_row(
                Text(
                    f" {result.type_label}",
                    style=TYPE_COLORS.get(result.type_label, "grey58"),
                ),
                self._fit(result.title, "title"),
                self._fit(artists, "artist"),
                self._fit(result.album or "", "album"),
                self._fit(result.duration or "", "duration"),
                key=key,
            )
            self._results[key] = result
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "add_to_playlist":
            return (
                self.app.client.library.authenticated
                and self.selected_result() is not None
            )
        return super().check_action(action, parameters)

    def action_add_to_playlist(self) -> None:
        result = self.selected_result()
        if result is not None:
            self.post_message(
                AddToPlaylistRequested(
                    result.video_id, result.title, tuple(result.artists)
                )
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # refresh_bindings() recomposes the whole footer — too costly per
        # arrow-key repeat. The footer only cares whether `s` applies to the
        # highlighted row, so refresh only when that flips.
        can_add = str(event.row_key.value) in self._results
        if can_add != self._last_can_add:
            self._last_can_add = can_add
            self.refresh_bindings()

    def _fit(self, value: str, column_key: str) -> str:
        """Truncate a cell value to its column width, appending '...' when cut off."""
        column = self.columns[column_key]
        max_width = column.get_render_width(self) - 2 * self.cell_padding
        if cell_len(value) <= max_width:
            return value
        return set_cell_size(value, max_width - 3) + "... "

    def selected_result(self) -> SearchResult | None:
        if self.row_count == 0:
            return None
        coordinate = self.coordinate_to_cell_key(self.cursor_coordinate)
        return self._results.get(str(coordinate.row_key.value))
