# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The program's entry point: ``python -m tesseract_win`` and the frozen ``TesserACT-Win.exe``.

    TesserACT-Win.exe                       the window
    TesserACT-Win.exe --version             one line naming the program and its engines
    TesserACT-Win.exe --self-test [--full] [--report FILE] [--work DIR]
                                            check the program without a window (see selftest.py)

The windowed Windows program has no console: ``sys.stdout`` and ``sys.stderr`` are None
there, so they are made safe first. Nothing may fail silently: everything goes to a
rotating log file in the user's local data folder, and exceptions nobody caught are
written there too. Tk is imported only when the window is really going to open.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from typing import Any, List, Optional, Sequence

from . import appdirs, branding, osapi

__all__ = ["main", "LOG_FILE_NAME"]

LOG_FILE_NAME = "tesseract-win.log"
_LOG_BYTES = 1_000_000
_LOG_BACKUPS = 3

log = logging.getLogger("tesseract_win")
_instance: Any = None                    # kept for the life of the program: it is the claim to be the only copy


def _safe_streams() -> None:
    """Give the program something to write to when it has no console (``pythonw``, the frozen exe)."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def _setup_logging() -> str:
    """A rotating log file in :func:`appdirs.log_dir`. Returns its path ("" when it cannot be written)."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        if getattr(handler, "_tesseract_win", False):
            root.removeHandler(handler)
            handler.close()
    try:
        folder = appdirs.log_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, LOG_FILE_NAME)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8", delay=True)
    except OSError:
        path = ""
        handler = logging.NullHandler()                  # a PC where the log cannot be written still runs
    handler._tesseract_win = True                        # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    root.addHandler(handler)
    return path


def _install_exception_hooks() -> None:
    """Whatever nobody caught is written to the log, from any thread."""

    def on_error(kind: Any, value: Any, trace: Any) -> None:
        if issubclass(kind, KeyboardInterrupt):
            return
        log.critical("an error nobody handled", exc_info=(kind, value, trace))

    def on_thread_error(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        log.critical("an error nobody handled in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def on_unraisable(args: Any) -> None:
        log.error("an error that could not be raised (%s)", getattr(args, "err_msg", None) or "no detail",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = on_error
    threading.excepthook = on_thread_error
    sys.unraisablehook = on_unraisable


def _value_after(args: List[str], flag: str) -> Optional[str]:
    """``--report FILE`` and ``--report=FILE``; None when the flag is absent or has no value."""
    for index, arg in enumerate(args):
        if arg == flag:
            following = args[index + 1] if index + 1 < len(args) else ""
            return following if following and not following.startswith("--") else None
        if arg.startswith(flag + "="):
            return arg[len(flag) + 1:] or None
    return None


def _say(text: str) -> None:
    """One line on standard output, if there is one (a character it cannot show is replaced)."""
    stream = sys.stdout
    if stream is None:
        return
    try:
        try:
            stream.write(text + "\n")
        except UnicodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write((text + "\n").encode(encoding, "backslashreplace").decode(encoding))
        stream.flush()
    except (OSError, ValueError, UnicodeError, LookupError):
        pass


def _already_open() -> None:
    """Another copy is running: bring its window forward, or at least say so."""
    try:
        shown = osapi.bring_other_instance_to_front(branding.APP_NAME)
    except Exception:
        log.exception("could not bring the other window forward")
        shown = False
    log.info("another copy is already running; this one ends (other window shown: %s)", shown)
    if shown:
        return
    message = "%s is already open." % branding.APP_NAME
    _say(message)
    if osapi.IS_WINDOWS:                                 # no console there: a small box, or nothing is seen
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showinfo(branding.APP_NAME, message, parent=root)
            root.destroy()
        except Exception:
            log.exception("could not show the 'already open' message")


def _run_window(args: Sequence[str]) -> int:
    global _instance
    path = _setup_logging()
    _install_exception_hooks()
    log.info("%s starting; frozen=%s; log=%s", branding.versions_line(), appdirs.is_frozen(), path or "none")
    try:
        osapi.set_process_defaults()                     # before any window exists
    except Exception:
        log.exception("the process defaults could not be set")
    try:
        _instance = osapi.single_instance(branding.APP_NAME)
    except Exception:
        log.exception("the check for another copy failed; starting anyway")
        _instance = object()
    if _instance is None:
        _already_open()
        return 0
    try:
        from .gui.app import run_app                     # Tk is imported here and nowhere earlier
        return int(run_app(list(args)) or 0)
    except SystemExit:
        raise
    except Exception:
        log.critical("the window could not be opened or ended with an error", exc_info=True)
        return 1
    finally:
        closing = getattr(_instance, "close", None)
        if callable(closing):
            try:
                closing()
            except Exception:
                log.exception("releasing the single-copy claim failed")
        logging.shutdown()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the program. ``argv`` are the arguments without the program name (None: ``sys.argv[1:]``)."""
    args = list(sys.argv[1:] if argv is None else argv)
    _safe_streams()
    if "--version" in args:
        _say(branding.versions_line())
        return 0
    if "--self-test" in args:
        _setup_logging()
        _install_exception_hooks()
        try:
            # Nobody is there to click: an engine that will not start or that crashes must not
            # put up a Windows box that waits for ever (the engines inherit the error mode).
            osapi.set_process_defaults()
        except Exception:
            log.exception("the process defaults could not be set")
        from . import selftest                           # no Tk on this path
        return selftest.run_selftest("--full" in args, _value_after(args, "--report"),
                                     _value_after(args, "--work"))
    return _run_window(args)


if __name__ == "__main__":
    sys.exit(main())
