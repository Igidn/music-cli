"""Reusable widgets for the music-cli TUI, one file per component."""

from __future__ import annotations

from .history_list import HistoryList
from .library_tree import LibraryTree
from .messages import AddToPlaylistRequested
from .now_playing import NowPlaying
from .queue_list import QueueList
from .results_table import TYPE_COLORS, ResultsTable
from .search_bar import FilterSelect, SearchInput
from .top_bar import TopBar
from .waveform import Waveform

__all__ = [
    "TYPE_COLORS",
    "AddToPlaylistRequested",
    "FilterSelect",
    "HistoryList",
    "LibraryTree",
    "NowPlaying",
    "QueueList",
    "ResultsTable",
    "SearchInput",
    "TopBar",
    "Waveform",
]
