# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The little the window remembers between two launches: its size and the last folders.

Nothing else is stored (decision D4, audit row C-20). The ticks and numbers of the
Trimming and Assembly pages start at the standard on every launch, and the state of a
batch lives in its results folder, not here.

The file is ``appdirs.settings_path()``: JSON with a format number, written to a temporary
name and renamed over the old file. Reading never fails: a missing, damaged or newer file
simply gives the defaults, and a folder that no longer exists on this PC is forgotten.
No Tk here; the window size is kept in 96 dpi units so that it means the same on a screen
with another scaling.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .. import appdirs, fsutil

__all__ = ["FORMAT", "FOLDER_KEYS", "Settings", "SettingsStore", "load", "save"]

log = logging.getLogger(__name__)

#: Format number of the settings file. A file with a higher number is left alone.
FORMAT = 1
#: The folders that are remembered: where reads were last added from, where the results
#: folder was last browsed, and the results folder of the last run.
FOLDER_KEYS = ("reads", "results_browse", "last_results")

_MIN_SIDE, _MAX_SIDE = 400, 20000


@dataclass
class Settings:
    """What is remembered. Sizes are for a 96 dpi screen; 0 means "nothing stored"."""
    window_width: int = 0
    window_height: int = 0
    maximized: bool = False
    folders: Dict[str, str] = field(default_factory=dict)

    def window_size(self) -> Optional[Tuple[int, int]]:
        if self.window_width and self.window_height:
            return self.window_width, self.window_height
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"format": FORMAT,
                "window": {"width": self.window_width, "height": self.window_height,
                           "maximized": self.maximized},
                "folders": {key: value for key, value in self.folders.items() if key in FOLDER_KEYS}}


def _side(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    whole = int(value)
    return whole if _MIN_SIDE <= whole <= _MAX_SIDE else 0


def _from_dict(data: Any) -> Settings:
    made = Settings()
    if not isinstance(data, dict):
        return made
    number = data.get("format", 0)
    if isinstance(number, bool) or not isinstance(number, int) or number > FORMAT or number < 1:
        return made                                   # damaged, or written by a newer program
    window = data.get("window")
    if isinstance(window, dict):
        width, height = _side(window.get("width")), _side(window.get("height"))
        if width and height:
            made.window_width, made.window_height = width, height
        made.maximized = window.get("maximized") is True
    folders = data.get("folders")
    if isinstance(folders, dict):
        for key in FOLDER_KEYS:
            value = folders.get(key)
            if isinstance(value, str) and value.strip() and "\x00" not in value:
                made.folders[key] = value
    return made


def load(path: Optional[str] = None) -> Settings:
    """Read the settings. Never raises: anything unusable gives the defaults."""
    try:
        return _from_dict(fsutil.read_json(path or appdirs.settings_path(), None))
    except Exception:                                 # a start-up must never fail on this file
        log.exception("the settings could not be read; using the defaults")
        return Settings()


def save(settings: Settings, path: Optional[str] = None) -> bool:
    """Write the settings. False, never an error, when they cannot be written."""
    target = path or appdirs.settings_path()
    try:
        fsutil.ensure_dir(os.path.dirname(target))
        fsutil.atomic_write_json(target, settings.to_dict())
        return True
    except Exception:
        log.warning("the settings could not be written to %s", target, exc_info=True)
        return False


class SettingsStore:
    """The settings of one window: read once, changed in memory, written when asked.

    ``remember=False`` (demonstrations, tests) reads and writes nothing.
    """

    def __init__(self, path: Optional[str] = None, *, remember: bool = True):
        self.path = path
        self.remember = remember
        self.settings = load(path) if remember else Settings()

    # -- folders
    def folder(self, key: str) -> str:
        """A remembered folder that still exists on this PC, or ""."""
        value = self.settings.folders.get(key, "")
        try:
            return value if value and os.path.isdir(value) else ""
        except (OSError, ValueError):
            return ""

    def recorded_folder(self, key: str) -> str:
        """The remembered folder as it was written, whether or not it can be reached now."""
        return self.settings.folders.get(key, "")

    def set_folder(self, key: str, path: str) -> None:
        if key not in FOLDER_KEYS:
            raise KeyError(key)
        if path:
            self.settings.folders[key] = path
        else:
            self.settings.folders.pop(key, None)

    # -- window
    def window_size(self) -> Optional[Tuple[int, int]]:
        return self.settings.window_size()

    def set_window(self, width: int, height: int, maximized: bool = False) -> None:
        """Remember the size (96 dpi units). The size of a maximized window is not taken:
        the one from before stays, so that "restore" gives a sensible window next time."""
        self.settings.maximized = bool(maximized)
        if not maximized and _side(width) and _side(height):
            self.settings.window_width, self.settings.window_height = int(width), int(height)

    def save(self) -> bool:
        return save(self.settings, self.path) if self.remember else False
