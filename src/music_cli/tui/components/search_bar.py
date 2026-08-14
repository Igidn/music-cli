"""Search box and its filter dropdown."""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding
from textual.widgets import Input, Select


class SearchInput(Input):
    """Search box; left/right move the cursor, or jump panes at the edges."""

    def action_cursor_left(self, select: bool = False) -> None:
        if not select and self.cursor_position == 0:
            self.app.action_pane_left()
        else:
            super().action_cursor_left(select)

    def action_cursor_right(self, select: bool = False) -> None:
        if not select and self.cursor_at_end and not self._suggestion:
            self.app.action_pane_right()
        else:
            super().action_cursor_right(select)


class FilterSelect(Select, inherit_bindings=False):
    """Search filter dropdown; enter/space opens it, arrows navigate panes."""

    BINDINGS: ClassVar = [
        Binding("enter,space", "show_overlay", "Show menu", show=False)
    ]
