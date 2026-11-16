# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The questions and boxes of the shell and of the Samples page.

- :func:`ask_overwrite`: the one question Start may ask (audit A-5, decision D2): what to
  do with samples that are already assembled. Skip is the default; when every sample is
  already assembled the choice is Overwrite or Cancel. Esc and the close box start nothing.
- :func:`show_about`: versions, attribution, "Save a problem report" and "Open log folder".
- :func:`show_message`, :func:`show_list`: a plain message, and a message with a list.
- :func:`choose_files`, :func:`choose_folder`: the file dialogs of Windows.

All of them are our own windows (``widgets.Dialog``) so that the buttons carry words; only
the file choosers are the system's. The functions that build the words need no display.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .. import appdirs, branding, fsutil, osapi
from ..model import Existing, ExistingResult
from . import problem_report
from . import state as st
from . import texts
from . import widgets as w

__all__ = ["overwrite_question", "ask_overwrite", "show_about", "about_body", "about_choices",
           "save_problem_report", "show_message", "show_list", "choose_files", "choose_folder",
           "FASTQ_PATTERNS"]

#: What the file chooser shows by default.
FASTQ_PATTERNS = ("*.fastq.gz", "*.fq.gz", "*.fastq", "*.fq")


# --------------------------------------------------------------------------- overwrite or skip

def overwrite_question(existing: Mapping[str, ExistingResult], total: int) -> texts.Question:
    """The words of the overwrite-or-skip question for the results found.

    ``existing`` is what ``results.existing_results`` returned; ``total`` the number of
    samples that would run. The list behind "Show the 12 samples" names each sample with
    the day it was assembled, and says when it was made with other settings.
    """
    names = sorted(existing, key=fsutil.natural_key)
    different = [existing[name] for name in names if not existing[name].same_settings]
    summary = ""
    if different:
        counted = Counter(item.settings_summary for item in different if item.settings_summary)
        if counted:
            summary = counted.most_common(1)[0][0]
    lines: List[str] = []
    for name in names:
        item = existing[name]
        when = st.short_date(item.finished_at)
        extra = [part for part in (when, st.Words.OVERWRITE_OTHER_SETTINGS if not item.same_settings else "") if part]
        lines.append("%s  (%s)" % (name, ", ".join(extra)) if extra else name)
    return texts.overwrite_question(len(names), max(total, len(names)), len(different), summary, lines)


def ask_overwrite(parent: Any, existing: Mapping[str, ExistingResult], total: int) -> Optional[Existing]:
    """Ask what to do with the samples that are already assembled.

    Returns ``Existing.SKIP`` or ``Existing.OVERWRITE``; None means Cancel: nothing starts.
    """
    answer = w.Dialog.ask(parent, overwrite_question(existing, total), level="warn")
    if answer == Existing.SKIP.value:
        return Existing.SKIP
    if answer == Existing.OVERWRITE.value:
        return Existing.OVERWRITE
    return None


# --------------------------------------------------------------------------- plain boxes

def show_message(parent: Any, title: str, body: str, *, level: str = "info",
                 button: Optional[str] = None) -> None:
    """One message and one button."""
    w.Dialog(parent, title=title, body=body, level=level,
             choices=[texts.Choice("ok", button or texts.BUTTON["ok"])], default="ok", cancel="ok").show()


def show_list(parent: Any, title: str, body: str, lines: Sequence[str]) -> None:
    """A message with a list that can be opened under it (the files that were not added)."""
    dialog = w.Dialog(parent, title=title, body=body, details="\n".join(lines),
                      details_label=texts.BUTTON["show_details"],
                      choices=[texts.Choice("close", texts.BUTTON["close"])], default="close", cancel="close")
    if dialog.details is not None:
        dialog.details.open()
    dialog.show()


def about_body() -> str:
    return "%s\n\n%s" % (branding.about_text(), st.Words.LOGS_NOTE)


def about_choices() -> List[texts.Choice]:
    """The buttons of the About box, left to right."""
    return [texts.Choice("report", texts.BUTTON["save_report"]),
            texts.Choice("logs", texts.BUTTON["open_log_folder"]),
            texts.Choice("close", texts.BUTTON["close"])]


def save_problem_report(parent: Any, results_dir: str = "") -> str:
    """Audit part A-9: write the problem report to the Desktop and say where it is.

    Returns the path of the zip ("" when it could not be written, which is said too)."""
    path = ""
    for folder in (osapi.desktop_dir(), os.path.expanduser("~"), appdirs.user_data_dir()):
        try:
            path = problem_report.write_problem_report(folder, results_dir)
            break
        except OSError:
            continue
    if not path:
        show_message(parent, st.Words.REPORT_TITLE, st.Words.REPORT_NOT_SAVED, level="warn",
                     button=texts.BUTTON["close"])
        return ""
    answer = w.Dialog(parent, title=st.Words.REPORT_TITLE, level="ok", wrap=460,
                      body=st.Words.REPORT_SAVED.format(name=os.path.basename(path), folder=os.path.dirname(path)),
                      choices=[texts.Choice("show", texts.BUTTON["open_folder"]),
                               texts.Choice("close", texts.BUTTON["close"])],
                      default="close", cancel="close").show()
    if answer == "show":
        osapi.reveal(path)
    return path


def show_about(parent: Any, results_dir: str = "") -> bool:
    """The About box: versions, attribution, Save a problem report, Open log folder.

    ``results_dir`` is the results folder in use, whose run log goes into a problem report.
    True when the log folder was opened from the box."""
    answer = w.Dialog(parent, title=st.Words.ABOUT_TITLE, body=about_body(), wrap=460,
                      choices=about_choices(), default="close", cancel="close").show()
    if answer == "report":
        save_problem_report(parent, results_dir)
        return False
    if answer != "logs":
        return False
    folder = appdirs.log_dir()
    try:
        fsutil.ensure_dir(folder)
    except OSError:
        pass
    return osapi.open_path(folder)


# --------------------------------------------------------------------------- file choosers

def _start_folder(folder: str) -> str:
    try:
        return folder if folder and os.path.isdir(folder) else ""
    except (OSError, ValueError):
        return ""


def choose_files(parent: Any, initial_folder: str = "") -> Tuple[str, ...]:
    """The system's file chooser for read files. An empty tuple when it was cancelled."""
    from tkinter import filedialog
    options = dict(parent=parent, title=st.Words.CHOOSE_FILES,
                   filetypes=[(st.Words.FASTQ_FILES, " ".join(FASTQ_PATTERNS)), (st.Words.ALL_FILES, "*.*")])
    start = _start_folder(initial_folder)
    if start:
        options["initialdir"] = start
    chosen = filedialog.askopenfilenames(**options)
    if not chosen:
        return ()
    if isinstance(chosen, str):                        # some Tk builds give one Tcl list
        chosen = parent.tk.splitlist(chosen)
    return tuple(os.path.normpath(str(path)) for path in chosen)


def choose_folder(parent: Any, title: str, initial_folder: str = "", must_exist: bool = True) -> str:
    """The system's folder chooser. "" when it was cancelled."""
    from tkinter import filedialog
    options = dict(parent=parent, title=title, mustexist=must_exist)
    start = _start_folder(initial_folder)
    if start:
        options["initialdir"] = start
    chosen = filedialog.askdirectory(**options)
    return os.path.normpath(str(chosen)) if chosen else ""
