# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Where things are: the bundled engines, the example reads, settings and logs.

Works the same from a source checkout (Linux or Windows) and from the frozen Windows
program. Never looks at the working directory: that is whatever started the program.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

from . import branding
from .model import EnginePaths

__all__ = ["is_frozen", "app_dir", "resource_dir", "find_engines", "engines_dir", "example_dir",
           "user_data_dir", "log_dir", "settings_path", "exe_suffix", "EngineMissing"]


class EngineMissing(Exception):
    """A bundled program is not where it should be. ``self.missing`` lists the file names."""

    def __init__(self, missing: List[str], folder: str):
        super().__init__("missing in %s: %s" % (folder, ", ".join(missing)))
        self.missing = missing
        self.folder = folder


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_suffix() -> str:
    return ".exe" if os.name == "nt" else ""


def app_dir() -> str:
    """The folder the user unzipped (frozen), or the repository root (source checkout)."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_dir() -> str:
    """Read-only data shipped inside the program (icons). ``sys._MEIPASS`` when frozen."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", app_dir())
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _engine_dir_candidates() -> List[str]:
    root = app_dir()
    out = []
    override = os.environ.get("TESSERACT_WIN_ENGINES", "")
    if override:
        out.append(override)
    if is_frozen():
        out.append(os.path.join(root, "engines"))                     # the shipped layout
    else:
        platform = "windows" if os.name == "nt" else "linux"
        out.append(os.path.join(root, "build", "engines", platform))  # a source checkout
    return out


def engines_dir() -> str:
    """The first candidate folder that exists; otherwise the last candidate."""
    candidates = _engine_dir_candidates()
    for folder in candidates:
        if os.path.isdir(folder):
            return folder
    return candidates[-1]


def find_engines(folder: Optional[str] = None) -> EnginePaths:
    """Locate both engines and the detection sketch. Raises :class:`EngineMissing`."""
    folder = folder or engines_dir()
    asm = os.path.join(folder, "tesseract-asm" + exe_suffix())
    scepter = os.path.join(folder, "scepter" + exe_suffix())
    sketch = os.path.join(folder, "om2detect.sketch")
    missing = [os.path.basename(p) for p in (asm, scepter) if not os.path.isfile(p)]
    if missing:
        raise EngineMissing(missing, folder)
    return EnginePaths(tesseract_asm=asm, scepter=scepter,
                       detect_sketch=sketch if os.path.isfile(sketch) else "")


def example_dir() -> str:
    return os.path.join(app_dir(), "example")


def user_data_dir() -> str:
    """Per-user folder for settings and logs (local, not roaming)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, branding.DATA_DIR_NAME)


def log_dir() -> str:
    return os.path.join(user_data_dir(), "logs")


def settings_path() -> str:
    return os.path.join(user_data_dir(), "settings.json")
