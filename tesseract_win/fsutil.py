# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""File operations that must not fail on Windows.

Antivirus, the search indexer, OneDrive and Explorer's preview pane hold new files
open for a moment, so "access denied" on the first try is normal there. Every helper
here retries briefly before it gives up. Standard library only; no Tk, no engines.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from typing import Any, Callable, Iterable, List, Optional

__all__ = [
    "retry", "atomic_write_text", "atomic_write_json", "read_json", "replace_file",
    "move_dir", "remove_tree", "ensure_dir", "free_bytes", "is_writable_dir",
    "natural_key", "safe_name", "format_bytes", "format_duration", "now_iso", "first_existing",
]

RETRIES = 6
RETRY_DELAY = 0.35   # seconds; about two seconds in all


def retry(operation: Callable[[], Any], *, attempts: int = RETRIES, delay: float = RETRY_DELAY,
          errors: tuple = (OSError,)) -> Any:
    """Run ``operation``; on ``errors`` wait and try again. The last error is raised."""
    last: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        try:
            return operation()
        except errors as exc:           # noqa: PERF203 - the retry is the point
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
    assert last is not None
    raise last


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def atomic_write_text(path: str, text: str, *, encoding: str = "utf-8", newline: str = "\n") -> None:
    """Write to a temporary name beside ``path``, then rename over it.

    Line ends are LF on every platform, so a file written on Windows equals one written on Linux.
    """
    folder = os.path.dirname(os.path.abspath(path))
    ensure_dir(folder)
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding=encoding, newline=newline) as handle:
        handle.write(text)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        retry(lambda: os.replace(tmp, path))
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: str, default: Any = None) -> Any:
    """Read a JSON file. A missing, unreadable or damaged file gives ``default``, never an error."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def replace_file(src: str, dst: str) -> None:
    """Move ``src`` over ``dst`` (same drive), with retries."""
    retry(lambda: os.replace(src, dst))


def _clear_readonly_and_retry(func: Callable[..., Any], path: str, _exc: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    func(path)


def remove_tree(path: str) -> bool:
    """Delete a folder and everything in it. True if it is gone afterwards.

    Clears read-only flags and retries; never raises for a folder that is already gone.
    """
    if not os.path.lexists(path):
        return True

    def attempt() -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, onerror=_clear_readonly_and_retry)
        else:
            os.remove(path)

    try:
        retry(attempt)
    except OSError:
        return not os.path.lexists(path)
    return not os.path.lexists(path)


def move_dir(src: str, dst: str) -> None:
    """Move folder ``src`` to ``dst`` (``dst`` must not exist).

    Renames when it can (same drive), retried; copies and deletes as the last resort.
    """
    ensure_dir(os.path.dirname(os.path.abspath(dst)))
    try:
        retry(lambda: os.rename(src, dst))
        return
    except OSError:
        pass
    shutil.copytree(src, dst)
    remove_tree(src)


def free_bytes(path: str) -> Optional[int]:
    """Free space this user may use at ``path`` or its nearest existing parent. None if unknown."""
    probe = os.path.abspath(path)
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def is_writable_dir(path: str) -> bool:
    """A real write test: create and delete a small file. ``os.access`` is not reliable on Windows."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".tesseract-win-write-test-%d" % os.getpid())
        with open(probe, "w", encoding="ascii") as handle:
            handle.write("ok")
        retry(lambda: os.remove(probe), attempts=3)
        return True
    except OSError:
        return False


_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> List[Any]:
    """Sort key that puts KP_2 before KP_10."""
    return [int(part) if part.isdigit() else part.casefold() for part in _DIGITS.split(text)]


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *("COM%d" % i for i in range(1, 10)),
                     *("LPT%d" % i for i in range(1, 10))}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\s]+')
MAX_NAME = 60


def safe_name(text: str) -> str:
    """A sample name that is a valid, portable folder and file name on Windows.

    Characters Windows forbids and all white space become ``_``; leading and trailing dots,
    spaces and underscores go; reserved device names get a trailing ``_``; at most 60 characters.
    An empty result becomes ``sample``.
    """
    name = _UNSAFE.sub("_", text.strip())
    name = re.sub(r"_+", "_", name).strip("._ ")
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME].rstrip("._ ")
    if not name:
        name = "sample"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name += "_"
    return name


def format_bytes(count: Optional[float]) -> str:
    """Sizes as Explorer shows them (1 GB = 1024 x 1024 x 1024 bytes)."""
    if count is None:
        return "unknown"
    value = float(count)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "bytes":
                return "%d bytes" % int(value)
            return ("%.0f %s" if value >= 100 else "%.1f %s") % (value, unit)
        value /= 1024
    return "%.1f TB" % value


def format_duration(seconds: float) -> str:
    """06:41 under an hour, 2 h 14 min above."""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%02d:%02d" % (seconds // 60, seconds % 60)
    return "%d h %02d min" % (seconds // 3600, (seconds % 3600) // 60)


def now_iso() -> str:
    """Local time, to the second, e.g. 2026-11-17T03:42:10."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def first_existing(paths: Iterable[str]) -> str:
    for path in paths:
        if path and os.path.exists(path):
            return path
    return ""
