"""Search results table with per-row search-result lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.cells import cell_len, set_cell_size
from rich.text import Text
from textual import events
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


@dataclass(frozen=True)
class _ColumnSpec:
    """Sizing policy for one column, in content cells (padding excluded)."""

    key: str
    label: str
    min_width: int  # below this a droppable column is dropped, not squeezed
    max_width: int  # cap while flexing; fixed columns have min == max
    weight: int  # share of spare width


_COLUMNS: ClassVar[tuple[_ColumnSpec, ...]] = (
    _ColumnSpec("type", "", min_width=10, max_width=10, weight=0),
    _ColumnSpec("title", "Title", min_width=14, max_width=70, weight=8),
    _ColumnSpec("artist", "Artist", min_width=9, max_width=28, weight=3),
    _ColumnSpec("album", "Album", min_width=6, max_width=22, weight=2),
    _ColumnSpec("duration", "Time", min_width=7, max_width=7, weight=0),
)

# Droppable columns are sacrificed in this order as the viewport narrows.
# Title and Time always stay visible; if even they don't fit, Title absorbs
# whatever is left above one cell and clipping takes over below that.
_DROP_ORDER: ClassVar[tuple[str, ...]] = ("album", "artist", "type")

_SPECS = {spec.key: spec for spec in _COLUMNS}

# Column widths before the first layout pass sizes them to the viewport.
_INITIAL_WIDTHS: ClassVar[dict[str, int]] = {
    "type": 10,
    "title": 34,
    "artist": 13,
    "album": 8,
    "duration": 7,
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
        self._ordered_results: list[SearchResult] = []
        self._last_can_add = False
        self._applied_layout: tuple[int, ...] | None = None

    def action_cursor_up(self) -> None:
        if not self.row_count or self.cursor_coordinate.row == 0:
            self.app.query_one("#search-input", Input).focus()
        else:
            super().action_cursor_up()

    def on_mount(self) -> None:
        for spec in _COLUMNS:
            self.add_column(spec.label, key=spec.key, width=_INITIAL_WIDTHS[spec.key])
        self.call_after_refresh(self._refit)

    def on_resize(self, event: events.Resize) -> None:
        # Regions and scrollbar visibility only settle after the next layout
        # pass, so refit one frame later; _refit deduplicates itself.
        self.call_after_refresh(self._refit)

    def set_results(self, results: list[SearchResult]) -> None:
        self._ordered_results = [r for r in results if r.video_id or r.browse_id]
        self._fill_rows()
        # Row count changes toggle the vertical scrollbar, which changes how
        # much width the columns get to divide up.
        self.call_after_refresh(self._refit)

    def _fill_rows(self, highlighted: str | None = None) -> None:
        """(Re)populate rows from the stored results for the current layout."""
        if highlighted is None and self.row_count:
            coordinate = self.coordinate_to_cell_key(self.cursor_coordinate)
            highlighted = str(coordinate.row_key.value)

        builders = {
            "type": lambda r: Text(
                f" {r.type_label}",
                style=TYPE_COLORS.get(r.type_label, "grey58"),
            ),
            "title": lambda r: self._fit(r.title, "title"),
            "artist": lambda r: self._fit(", ".join(r.artists), "artist"),
            "album": lambda r: self._fit(r.album or "", "album"),
            "duration": lambda r: self._fit(r.duration or "--:--", "duration"),
        }
        cell_keys = [column.key.value for column in self.ordered_columns]

        self.clear()
        self._results.clear()
        restored_index = None
        for index, result in enumerate(self._ordered_results):
            key = result.video_id or result.browse_id
            self.add_row(*(builders[k](result) for k in cell_keys), key=key)
            self._results[key] = result
            if key == highlighted:
                restored_index = index

        if restored_index is not None:
            self.move_cursor(row=restored_index)

    def _refit(self) -> None:
        """Resize columns to the available content width."""
        available = self.scrollable_content_region.width
        if available <= 0:
            return
        widths = self._compute_layout(available)
        if tuple(widths.items()) == self._applied_layout:
            return
        order = [column.key.value for column in self.ordered_columns]
        if order != list(widths):
            # The set of visible columns changed. DataTable has no column
            # hiding and re-added columns go to the end, so recreate them all.
            # Save the highlighted row before rebuilding, since _rebuild_columns
            # clears rows and _fill_rows would lose the cursor.
            highlighted = None
            if self.row_count:
                try:
                    coordinate = self.coordinate_to_cell_key(self.cursor_coordinate)
                    highlighted = str(coordinate.row_key.value)
                except Exception:  # noqa: BLE001
                    pass
            self._rebuild_columns(widths)
            self._fill_rows(highlighted=highlighted)
        else:
            for key, width in widths.items():
                self.columns[key].width = width
            self._fill_rows()
        self._applied_layout = tuple(widths.items())

    def _rebuild_columns(self, widths: dict[str, int]) -> None:
        self.clear()
        for column in list(self.columns.values()):
            self.remove_column(column.key)
        for key, width in widths.items():
            spec = _SPECS[key]
            self.add_column(spec.label, key=key, width=width)

    def _compute_layout(self, available: int) -> dict[str, int]:
        specs = _SPECS
        padding_total = 2 * self.cell_padding
        visible = [spec.key for spec in _COLUMNS]

        def required(width: int) -> bool:
            return (
                padding_total * len(visible)
                + sum(specs[key].min_width for key in visible)
                <= width
            )

        for key in _DROP_ORDER:
            if required(available):
                break
            visible.remove(key)

        budget = available - padding_total * len(visible)
        fixed_total = sum(
            specs[key].max_width for key in visible if specs[key].weight == 0
        )
        flexible = [specs[key] for key in visible if specs[key].weight > 0]
        flexible_min = sum(spec.min_width for spec in flexible)

        widths = {
            key: specs[key].max_width for key in visible if specs[key].weight == 0
        }
        pool = budget - fixed_total

        if not flexible:
            return widths  # degenerate terminal; let the renderer clip

        if pool >= flexible_min:
            for spec in flexible:
                widths[spec.key] = spec.min_width
                pool -= spec.min_width
            return self._distribute_spare(widths, flexible, pool)

        # Squeezed past every minimum: split what remains proportionally,
        # never going below one cell per column.
        weight_sum = sum(spec.weight for spec in flexible)
        widths.update(
            {spec.key: max(pool * spec.weight // weight_sum, 1) for spec in flexible}
        )
        return widths

    @staticmethod
    def _distribute_spare(
        widths: dict[str, int], flexible: list[_ColumnSpec], spare: int
    ) -> dict[str, int]:
        while spare > 0:
            open_specs = [s for s in flexible if widths[s.key] < s.max_width]
            weight_sum = sum(s.weight for s in open_specs)
            if not open_specs or weight_sum == 0:
                break  # everything capped: leave the rest blank
            spent = 0
            for spec in open_specs:
                take = min(
                    spare * spec.weight // weight_sum,
                    spec.max_width - widths[spec.key],
                )
                widths[spec.key] += take
                spent += take
            if spent == 0:
                break
            spare -= spent
        return widths

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
        return set_cell_size(value, max(max_width - 3, 0)) + "..."

    def selected_result(self) -> SearchResult | None:
        if self.row_count == 0:
            return None
        coordinate = self.coordinate_to_cell_key(self.cursor_coordinate)
        return self._results.get(str(coordinate.row_key.value))
