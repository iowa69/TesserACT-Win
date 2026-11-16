# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Step 4, the Run page: what is happening now, what has finished, and how to stop.

The page starts a runner (``batch.BatchRunner``, or a stand-in given to :meth:`RunPage.begin`)
and from then on only *reads*: the runner posts the events of ``model.py`` on a queue, and
the page empties that queue from ``after`` on the main thread, a bounded number of events
per tick, so the window stays responsive with a chatty engine and a hundred samples. The
runner thread never touches Tk, and the page never does the runner's work.

From top to bottom (decision D7, audit B.3 and B.5):

- notice bars: an interrupted run that can be continued, battery, memory, a sleep
- the header: "Sample 13 of 40: KP_013"; "Stopping after KP_013..." with a way back
- a card: a bar that counts samples, two clocks that tick every second and the rough time
  left, the stage chips, the step line, the "Working" line with the sentence that silence
  is normal, the advice about locking versus sleeping, the settings in use
- the table, which follows the running sample and fills each row in as it finishes
- a strip for the selected row: its note in full, Open report and Open folder (Open log
  and Open folder for a failure); a double-click on a row does the same as the first button
- Stop at the bottom left, Show log beside it, Open results folder and Open summary at
  the bottom right

When the batch ends the header and the card become the result panel: the outcome in one
line, the count of every status, where things are, and Continue, Try the failed samples
again, New run and Back to the samples as they apply.

In a short window the table keeps its rows and the rest gives way, one step at a time
(:meth:`RunPage._fit`): the opening sentence; then the settings line and the strip of a row
that is not selected; then the advice about sleep; last the stage chips. Stop and the Open
buttons stand in the bottom bar of the shell, where Back, Next and Start are on the other
steps, so they can never be pushed out of the window.

Everything that only makes words or decides which buttons apply is a plain function at the
top of this module, so it can be tested without a display.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import ttk
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .. import fsutil, model, osapi, results, settings_map, summary_report
from ..model import EndReason, RunConfig, RunStatus, Sample, SampleOutcome, Stage, StopMode
from . import run_dialogs, texts
from . import theme as _theme
from . import widgets as w

__all__ = [
    "RunPage", "PHASES", "COUNT_ORDER", "PENDING", "time_left_now", "clock_line", "tally_text",
    "position_text", "took_words", "split_message", "strip_counts", "end_message", "end_level", "tally",
    "count_words", "where_text", "failed_hint", "title_text", "end_actions", "retry_label",
    "stop_button", "row_values", "full_note", "strip_content", "memory_words", "working_text",
    "stage_states",
]

log = logging.getLogger(__name__)

# =========================================================================================
# WORDS THAT ARE NOT IN texts.py YET (to be moved there by its owner; listed in my report)
# =========================================================================================
IDLE_TEXT = ("Nothing is running. Add your samples on the Samples step and press Start: this "
             "page then shows the run.")
STOPPING_NOW = "Stopping..."
STOPPING_NOW_STEP = "Ending the work on {sample}. This takes a few seconds."
BETWEEN_SAMPLES = "Getting the next sample ready"
TALLY = "{done} done, {failed} failed."
POSITION = "Sample {position} of {total}."
FINISHED_AT = "Finished at {time}. It took {duration}."
STOPPED_AT = "Stopped at {time}."
TOOK_UNDER_A_MINUTE = "less than a minute"
WHERE = ("Your assemblies are in {folder} ({files}, one per sample). The summary of all samples "
         "is run_summary.html.")
WHERE_NONE = "No sample was assembled this time. The results folder is {folder}."
FAILED_HINT_ONE = "1 sample failed. Select its row to see why and what to do."
FAILED_HINT_MANY = "{n} samples failed. Select a row to see why and what to do."
TITLE_FINISHED = "Finished: {done} done"
TITLE_STOPPED = "Stopped: {done} of {total} done"
TITLE_FAILED = ", {failed} failed"
STRIP_PLAIN = "{sample}: {status}."
STRIP_NUMBERS = "{contigs}, {total} bases in total, N50 {n50}."
KEPT_EARLIER = "The earlier assembly of this sample was kept."
OPENED_FOLDER_INSTEAD = "{name} could not be opened, so the folder that holds it was opened instead."
NOT_OPENED = ("{name} could not be opened. It may not have been written yet, or it was moved, or "
              "its drive is no longer connected.")
START_FAULT = ("The run could not start because of a fault inside TesserACT-Win. Nothing was "
               "changed. Please send a problem report (About, Save a problem report).")
RUNNER_GONE = ("Stopped: the run ended unexpectedly because of a fault inside TesserACT-Win. "
               "Press Continue to carry on with the samples that are left. If it happens again, "
               "please send a problem report (About, Save a problem report).")
CANNOT_START = "The run cannot start yet."
SUMMARY_WRITTEN = "The summary has been written again."
SUMMARY_STILL_OPEN = ("The summary could still not be written. Close the program that has it open "
                      "(Excel, for example), then press the button again.")
WINDOW_PAUSED_LOG = ("[TesserACT-Win] Nothing was shown from {start} to {end}: the PC was asleep, "
                     "or the window was busy.")
# =========================================================================================

#: The life of the page.
PHASES = ("idle", "starting", "running", "stopping_after", "stopping_now", "ended")
_RUNNING_PHASES = ("starting", "running", "stopping_after", "stopping_now")

#: The order in which the statuses are counted at the end.
COUNT_ORDER: Tuple[RunStatus, ...] = (
    RunStatus.DONE, RunStatus.DONE_NOTES, RunStatus.FAILED, RunStatus.SKIPPED,
    RunStatus.STOPPED, RunStatus.NOT_STARTED, RunStatus.LEFT_OUT)
#: What Continue runs.
PENDING: Tuple[RunStatus, ...] = (
    RunStatus.STOPPED, RunStatus.NOT_STARTED, RunStatus.WAITING, RunStatus.JOINING_LANES,
    RunStatus.TRIMMING, RunStatus.CHECKING_READS, RunStatus.ASSEMBLING, RunStatus.SUMMARISING)
_ASSEMBLED = (RunStatus.DONE, RunStatus.DONE_NOTES, RunStatus.SKIPPED)
#: A sample that was dealt with: the bar that counts samples moves on for these.
_ENDED = (RunStatus.DONE, RunStatus.DONE_NOTES, RunStatus.FAILED, RunStatus.SKIPPED)
_DIM = (RunStatus.WAITING, RunStatus.NOT_STARTED, RunStatus.LEFT_OUT)

_STAGE_ORDER: Tuple[Stage, ...] = (Stage.JOIN_LANES, Stage.TRIM, Stage.CHECK_READS,
                                   Stage.ASSEMBLE, Stage.SUMMARISE)
_STAGE_OF: Dict[RunStatus, Stage] = {
    RunStatus.JOINING_LANES: Stage.JOIN_LANES, RunStatus.TRIMMING: Stage.TRIM,
    RunStatus.CHECKING_READS: Stage.CHECK_READS, RunStatus.ASSEMBLING: Stage.ASSEMBLE,
    RunStatus.SUMMARISING: Stage.SUMMARISE,
}
_LEVEL_COLOUR = {"info": "accent", "ok": "ok", "warn": "warn", "bad": "bad"}
_KIND_COLOUR = {"ok": "ok", "warn": "warn", "bad": "bad", "accent": "accent", "muted": "border_strong"}


# =========================================================================== words and decisions
# No Tk below this line until the class: everything here can be tested without a display.

def time_left_now(eta_seconds: Optional[float], received_at: float, now: float) -> Optional[float]:
    """The rough time left at ``now``, from an estimate that arrived at ``received_at``.

    The runner sends an estimate when a sample starts and when one ends; between two of
    them the page counts it down, never below zero. None stays None.
    """
    if eta_seconds is None:
        return None
    return max(0.0, float(eta_seconds) - max(0.0, now - received_at))


def clock_line(sample_seconds: Optional[float], run_seconds: float, left: Optional[float]) -> str:
    """ "This sample 06:41. Whole run 2 h 14 min. About 4 hours left (rough estimate)." """
    sample = fsutil.format_duration(sample_seconds or 0.0)
    return "%s %s" % (texts.clocks(sample, fsutil.format_duration(run_seconds)), texts.time_left(left))


def tally_text(done: int, failed: int) -> str:
    """ "34 done, 2 failed." """
    return TALLY.format(done=texts.number(done), failed=texts.number(failed))


def position_text(position: int, total: int) -> str:
    """ "Sample 37 of 120." """
    return POSITION.format(position=texts.number(position), total=texts.number(total))


def took_words(seconds: float) -> str:
    """ "9 h 14 min", "14 min", "less than a minute" """
    whole = int(max(0.0, seconds) // 60)
    if whole < 1:
        return TOOK_UNDER_A_MINUTE
    if whole < 60:
        return "%d min" % whole
    return "%d h %02d min" % (whole // 60, whole % 60)


def split_message(message: str) -> Tuple[str, str]:
    """The first sentence, and the rest: the heading of the result panel and its body."""
    text = " ".join(str(message or "").split())
    cut = text.find(". ")
    if cut < 0:
        return text, ""
    return text[:cut + 1], text[cut + 2:]


_COUNT_ITEM = re.compile(r"^\d[\d,]*\s+(.+)$")


def strip_counts(message: str) -> str:
    """``message`` without the sentences that only count samples ("36 done, 2 failed.").

    The runner's closing line carries the counts in words; the result panel shows the count
    of every status itself, with its colour, so the sentence would say it twice.
    """
    known = {words.lower() for words in texts.RUN_STATUS_LABEL.values()} | {"finished", "skipped"}
    kept: List[str] = []
    for sentence in re.split(r"(?<=\.)\s+", " ".join(str(message or "").split())):
        items = [item.strip() for item in sentence.rstrip(".").split(", ")]
        matches = [_COUNT_ITEM.match(item) for item in items]
        if sentence and all(match is not None and match.group(1) in known for match in matches):
            continue
        kept.append(sentence)
    return " ".join(part for part in kept if part)


def end_message(reason: EndReason, message: str, seconds: float, clock_time: str) -> str:
    """The outcome in words: the runner's own line, or one made here when it sent none."""
    if message and message.strip():
        return message.strip()
    if reason is EndReason.COMPLETED:
        return FINISHED_AT.format(time=clock_time, duration=took_words(seconds))
    if reason is EndReason.STOPPED:
        return "%s %s" % (STOPPED_AT.format(time=clock_time), texts.NOTHING_LOST)
    return "%s. %s" % (texts.END_REASON_LABEL[reason], texts.NOTHING_LOST)


def end_level(reason: EndReason, counts: Mapping[str, int]) -> str:
    """The colour of the result panel: ok, warn (some failed), info (stopped), bad (the PC)."""
    if reason is EndReason.ENVIRONMENT:
        return "bad"
    if reason is EndReason.STOPPED:
        return "info"
    return "warn" if counts.get(RunStatus.FAILED.value, 0) > 0 else "ok"


def tally(statuses: Iterable[RunStatus]) -> Dict[str, int]:
    """Status value -> number of samples, for every status that occurs."""
    counts: Dict[str, int] = {}
    for status in statuses:
        counts[status.value] = counts.get(status.value, 0) + 1
    return counts


def count_words(counts: Mapping[str, int]) -> List[Tuple[RunStatus, str]]:
    """Every status that has samples, in a fixed order: ``(DONE, "36 done")``, ...

    Statuses outside :data:`COUNT_ORDER` (a sample still marked as running or waiting)
    are counted too, so that every sample of the list is accounted for.
    """
    out: List[Tuple[RunStatus, str]] = []
    rest = [status for status in RunStatus if status not in COUNT_ORDER]
    for status in list(COUNT_ORDER) + rest:
        number = int(counts.get(status.value, 0) or 0)
        if number > 0:
            out.append((status, "%s %s" % (texts.number(number), texts.RUN_STATUS_LABEL[status].lower())))
    return out


def where_text(results_dir: str, assembled: int) -> str:
    """Where the assemblies are, with their number (audit row 10.2)."""
    if assembled <= 0:
        return WHERE_NONE.format(folder=results_dir)
    folder = results.assemblies_dir(results_dir)
    if "\\" in results_dir and "/" not in results_dir:          # a Windows path, wherever it is shown
        folder = "%s\\%s" % (results_dir.rstrip("\\"), os.path.basename(folder))
    return WHERE.format(folder=folder, files=texts.count(assembled, "file"))


def failed_hint(failed: int) -> str:
    if failed <= 0:
        return ""
    return FAILED_HINT_ONE if failed == 1 else FAILED_HINT_MANY.format(n=texts.number(failed))


def title_text(reason: EndReason, counts: Mapping[str, int]) -> str:
    """For the title bar, which also shows on the taskbar: "Finished: 38 done, 2 failed"."""
    done = counts.get(RunStatus.DONE.value, 0) + counts.get(RunStatus.DONE_NOTES.value, 0)
    failed = counts.get(RunStatus.FAILED.value, 0)
    if reason is EndReason.COMPLETED:
        text = TITLE_FINISHED.format(done=texts.number(done))
    else:
        total = sum(n for key, n in counts.items() if key != RunStatus.LEFT_OUT.value)
        text = TITLE_STOPPED.format(done=texts.number(done), total=texts.number(total))
    if failed > 0:
        text += TITLE_FAILED.format(failed=texts.number(failed))
    return text


def end_actions(reason: EndReason, counts: Mapping[str, int], can_new_run: bool = True) -> List[str]:
    """Which buttons the result panel carries, in order.

    ``continue`` when samples were stopped or never started; ``retry_failed`` when some
    failed; then ``new_run`` and ``back_to_samples``.
    """
    actions: List[str] = []
    if sum(counts.get(status.value, 0) for status in PENDING) > 0:
        actions.append("continue")
    if counts.get(RunStatus.FAILED.value, 0) > 0:
        actions.append("retry_failed")
    if can_new_run:
        actions.append("new_run")
    actions.append("back_to_samples")
    return actions


def retry_label(failed: int) -> str:
    """ "Try the failed sample again" / "Try the 2 failed samples again" """
    if failed == 1:
        return texts.BUTTON["retry_failed_one"]
    return texts.BUTTON["retry_failed_many"].format(n=texts.number(failed))


def stop_button(phase: str, has_sample: bool = True) -> Tuple[str, bool]:
    """The words on the Stop button and whether it can be pressed, for each phase.

    It reads "Stop now" once "Stop after <sample>" has been chosen (audit row 8.3).
    """
    if phase == "running":
        return texts.BUTTON["stop"], has_sample
    if phase == "stopping_after":
        return texts.BUTTON["stop_now"], has_sample
    if phase == "stopping_now":
        return texts.BUTTON["stop_now"], False
    return texts.BUTTON["stop"], False


def full_note(outcome: Optional[SampleOutcome], note: str = "") -> str:
    """Everything worth reading about a sample: the reason, the next step, every warning."""
    if outcome is None:
        return note.strip()
    parts: List[str] = []
    for part in [outcome.note or note, outcome.next_step] + list(outcome.notes or []):
        part = (part or "").strip()
        if part and part not in parts and not any(part in earlier for earlier in parts):
            parts.append(part)
    if outcome.kept_earlier_result and KEPT_EARLIER not in parts:
        parts.append(KEPT_EARLIER)
    return " ".join(parts)


def row_values(name: str, status: RunStatus, outcome: Optional[SampleOutcome] = None,
               note: str = "") -> Dict[str, str]:
    """The cells of a row of the table. Numbers appear once the sample has an outcome."""
    values = {"sample": name, "status": texts.RUN_STATUS_LABEL[status], "contigs": "",
              "total_length": "", "n50": "", "notes": note or ""}
    if outcome is not None:
        if outcome.contigs is not None:
            values["contigs"] = texts.number(outcome.contigs)
        if outcome.total_length is not None:
            values["total_length"] = texts.number(outcome.total_length)
        if outcome.n50 is not None:
            values["n50"] = texts.number(outcome.n50)
        first = " ".join(part for part in ((outcome.note or note or "").strip(),
                                           (outcome.next_step or "").strip()) if part)
        values["notes"] = first or (KEPT_EARLIER if outcome.kept_earlier_result else "")
    return values


def strip_content(name: str, status: RunStatus, outcome: Optional[SampleOutcome] = None,
                  note: str = "") -> Tuple[str, List[str]]:
    """The strip under the table for a selected row: its words and its buttons.

    Buttons are keys of ``texts.BUTTON``: Open report and Open folder for a finished
    sample, Open log and Open folder for a failure, none while there is nothing to open.
    """
    said = full_note(outcome, note)
    if not said:
        said = STRIP_PLAIN.format(sample=name, status=texts.RUN_STATUS_LABEL[status])
        if outcome is not None and outcome.contigs is not None and outcome.total_length is not None \
                and outcome.n50 is not None:
            said += " " + STRIP_NUMBERS.format(contigs=texts.count(outcome.contigs, "contig"),
                                               total=texts.number(outcome.total_length),
                                               n50=texts.number(outcome.n50))
    else:
        said = "%s: %s" % (name, said)
    if status in _ASSEMBLED:
        buttons = ["open_report", "open_folder"]
    elif status is RunStatus.FAILED:
        buttons = ["open_log", "open_folder"]
    else:
        buttons = []
    return said, buttons


def memory_words(memory_bytes: int) -> str:
    """ "3.1 GB" """
    return fsutil.format_bytes(max(0, int(memory_bytes)))


def working_text(busy_threads: Optional[float], memory_bytes: int = 0, brief: bool = False) -> str:
    """The line under the step: proof of life, then the sentence that silence is normal.

    ``brief`` (a short window): the proof of life alone, once there is one.
    """
    if busy_threads is None:
        return texts.SILENCE_IS_NORMAL
    working = texts.working_line(busy_threads, memory_words(memory_bytes))
    return working + "." if brief else "%s. %s" % (working, texts.SILENCE_IS_NORMAL)


def stage_states(current: Optional[Stage], *, trimmed: bool = True, lanes: int = 1,
                 final: Optional[RunStatus] = None) -> Dict[str, str]:
    """The state of every chip while ``current`` is the stage in progress.

    Stages before it are done, stages after it wait; Trim is "skipped" for a sample that is
    not trimmed, and so is Join lanes for a sample with one lane. With ``final`` the sample
    has ended: everything done for a finished one, the current stage failed for a failure.
    """
    states: Dict[str, str] = {}
    all_done = final in (RunStatus.DONE, RunStatus.DONE_NOTES)
    before = current is not None
    for stage in _STAGE_ORDER:
        skipped = (stage is Stage.TRIM and not trimmed) or (stage is Stage.JOIN_LANES and lanes < 2)
        if stage is current:
            before = False
            if skipped:
                state = "skipped"
            elif all_done:
                state = "done"
            elif final is RunStatus.FAILED:
                state = "failed"
            elif final is not None:
                state = "waiting"
            else:
                state = "running"
        elif skipped:
            state = "skipped"
        elif all_done or before:
            state = "done"
        else:
            state = "waiting"
        states[stage.value] = state
    return states


# =========================================================================== the page

@dataclass
class _Row:
    row_id: str
    name: str
    status: RunStatus = RunStatus.WAITING
    note: str = ""
    outcome: Optional[SampleOutcome] = None


class RunPage(ttk.Frame):
    """The Run page. One per window; made once and reused for every batch."""

    POLL_MS = 100                    # how often the queue is emptied
    MAX_EVENTS_PER_TICK = 400        # at most this many events per tick ...
    TICK_BUDGET_SECONDS = 0.04       # ... and at most this long
    BATTERY_EVERY_SECONDS = 30.0
    LOW_BATTERY_PERCENT = 20
    SLEEP_GAP_SECONDS = 60.0
    CLOSE_WAIT_SECONDS = 20.0
    FIT_DELAY_MS = 60

    def __init__(self, parent: Any, app: Any):
        look = self._look = _theme.current(parent)
        self.ground = "page"
        super().__init__(parent, style=look.style_name("TFrame", "page"))
        self.app = app
        self.help_line = getattr(app, "help_line", None)
        #: the clock of the run in seconds (the demonstration replaces it with a faster one)
        self.clock: Callable[[], float] = time.monotonic
        #: ``preflight(config)`` returns the problems that prevent Continue; None: ask ``batch``
        self.preflight: Optional[Callable[[RunConfig], Sequence[str]]] = None
        #: ``interrupted_finder(results_dir)``; None: ask ``batch.interrupted_run``
        self.interrupted_finder: Optional[Callable[[str], Any]] = None
        #: the Stop or close question while one is open (a ``run_dialogs.FollowingQuestion``)
        self.open_question: Optional[run_dialogs.FollowingQuestion] = None

        self.phase = "idle"
        self.end_reason: Optional[EndReason] = None
        self.end_counts: Dict[str, int] = {}
        self._config: Optional[RunConfig] = None
        self._runner: Any = None
        self._events: "queue.Queue[model.Event]" = queue.Queue()
        self._rows: Dict[str, _Row] = {}
        self._order: List[str] = []
        self._samples: Dict[str, Sample] = {}
        self._left_out: List[Tuple[str, str]] = []
        self._batch_names: List[str] = []
        #: rows of an earlier batch that stay in the table when a run is continued, and how many
        #: of them had ended: the header, the bar and the counts go over the whole list
        self._earlier: List[str] = []
        self._earlier_ended = 0
        self._finals: set = set()
        self._current = ""
        self._current_stage: Optional[Stage] = None
        self._sample_started = 0.0
        self._run_started = 0.0
        self._eta: Optional[Tuple[float, float]] = None
        self._working: Optional[Tuple[float, int]] = None
        self._pending_stage: Optional[model.StageProgress] = None
        self._pending_working: Optional[model.Working] = None
        self._log_buffer: List[str] = []
        self._job: Optional[str] = None
        self._last_wall = 0.0
        self._last_battery = 0.0
        self._battery_flashed = False
        self._dismissed: Dict[str, str] = {}
        self._runner_gone_since: Optional[float] = None
        self._clock_text = ""
        self._log_shown = False
        self._row_serial = 0
        self._offer: Optional[run_dialogs.InterruptedOffer] = None
        self._offer_folder = ""
        self._busy = False
        self._last_state: Optional[run_dialogs.RunState] = None
        self._runner_tells: set = set()               # kinds of notice the runner looks after itself
        self._started_iso = self._ended_iso = ""
        self._end_event: Optional[model.BatchFinished] = None
        #: 0 roomy; 1 the opening sentence is left out; 2 also the settings line, the place of
        #: the assemblies, and the strip while no row is selected; 3 also the advice about
        #: sleep and the two links of the result panel; 4 also the stage chips and the hint
        #: about failures (a short window, see _fit)
        self.compact = 0
        self._outcome: Tuple[str, str] = ("", "")
        self._hint = ""
        self._fit_job: Optional[str] = None
        self._fit_changes: List[float] = []           # when the layout last changed (never flip for ever)
        self._fit_height = 0
        self._build()
        self._apply_phase()
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.bind("<Configure>", lambda _e: self._fit_later(), add="+")
        self.bind("<Map>", lambda _e: self._fit_later(), add="+")

    # ------------------------------------------------------------------ building
    def _build(self) -> None:
        look = self._look
        px = look.px
        tip = self.help_line
        self.columnconfigure(0, weight=1)

        self.notices = w.NoticeBar(self)
        self.notices.grid(row=0, column=0, sticky="ew")

        # ---- the header
        self.head = w.Frame(self)
        self.head.grid(row=1, column=0, sticky="ew")
        self.head.columnconfigure(2, weight=1)
        self.title_label = w.label(self.head, texts.PAGE_TITLE["run"], "title")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.keep_going_button = w.button(self.head, texts.BUTTON["keep_going"], self._keep_going)
        self.keep_going_button.grid(row=0, column=1, sticky="w", padx=(px(16), 0))
        self.keep_going_button.grid_remove()
        self.intro = w.WrapLabel(self.head, texts.PAGE_INTRO["run"], "soft", wrap=820)
        self.intro.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(px(4), 0))

        # ---- the card: what is happening now, or how it ended
        self.card = w.Card(self, padding=(18, 12, 18, 12))
        self.card.grid(row=2, column=0, sticky="ew", pady=(px(12), 0))
        self.card.columnconfigure(0, weight=1)
        self._build_now()
        self._build_result()

        # ---- the table
        names = texts.RUN_COLUMNS
        hover = texts.HOVER
        self.table = w.CheckTable(self, [
            w.Column("sample", names["sample"], width=150, min_width=90, shorten="middle",
                     hover=hover["run_sample_column"]),
            w.Column("status", names["status"], width=210, min_width=120, status=True,
                     hover=hover["run_status_column"]),
            w.Column("contigs", names["contigs"], width=80, anchor="e", hover=hover["contigs_column"]),
            w.Column("total_length", names["total_length"], width=112, anchor="e",
                     hover=hover["total_length_column"]),
            w.Column("n50", names["n50"], width=92, anchor="e", hover=hover["n50_column"]),
            w.Column("notes", names["notes"], width=280, min_width=120, stretch=True,
                     hover=hover["notes_column"]),
        ], checks=False, height=4, removable=False, help_line=tip,
            on_select=self._on_select, on_activate=self._on_activate)
        self.table.grid(row=3, column=0, sticky="nsew", pady=(px(12), 0))
        self.rowconfigure(3, weight=3, minsize=px(96))    # never less than its heading and two rows

        # ---- the strip for the selected row
        self.strip = w.Well(self, padding=(12, 8, 10, 8))
        self.strip.grid(row=4, column=0, sticky="ew", pady=(px(8), 0))
        self.strip.columnconfigure(0, weight=1)
        self.strip_text = w.WrapLabel(self.strip, texts.SELECT_A_ROW, "soft", wrap=640)
        self.strip_text.grid(row=0, column=0, sticky="ew", pady=px(4))
        self.strip_buttons: Dict[str, ttk.Button] = {}
        for column, key in enumerate(("open_report", "open_log", "open_folder"), start=1):
            made = w.button(self.strip, texts.BUTTON[key], lambda k=key: self._strip_action(k))
            made.grid(row=0, column=column, padx=(px(8), 0), sticky="n")
            made.grid_remove()
            w.explain(made, hover[key], tip)
            self.strip_buttons[key] = made

        # ---- the log
        self.log_pane = w.LogPane(self, height=7)
        self.log_pane.grid(row=5, column=0, sticky="nsew", pady=(px(8), 0))
        self.log_pane.grid_remove()

        # ---- the bottom row: Stop on the left, the Open buttons on the right. In the window
        # it stands where Back, Next and Start are on the other steps (the shell gives the
        # place), so that it is in the same spot at every size; alone, at the foot of the page.
        host = getattr(self.app, "run_foot", None)
        if host is not None:
            foot = w.Frame(host)
            foot.pack(fill="x")
        else:
            foot = w.Frame(self)
            foot.grid(row=6, column=0, sticky="ew", pady=(px(12), 0))
        self.foot = foot
        self.stop_button = w.button(foot, texts.BUTTON["stop"], self._stop_pressed)
        self.stop_button.pack(side="left")
        w.explain(self.stop_button, hover["stop"], tip)
        self.log_toggle = w.button(foot, texts.BUTTON["show_log"], self._toggle_log, kind="tool")
        self.log_toggle.pack(side="left", padx=(px(8), 0))
        w.explain(self.log_toggle, hover["show_log"], tip)
        self.open_summary_button = w.button(foot, texts.BUTTON["open_summary"], self._open_summary)
        self.open_summary_button.pack(side="right")
        w.explain(self.open_summary_button, hover["open_summary"], tip)
        self.open_results_button = w.button(foot, texts.BUTTON["open_results"], self._open_results)
        self.open_results_button.pack(side="right", padx=(0, px(8)))
        w.explain(self.open_results_button, hover["open_results"], tip)
        self._set_open_enabled(False)

    def _build_now(self) -> None:
        look = self._look
        px = look.px
        now = self.now_frame = w.Frame(self.card)
        now.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(now, maximum=1, value=0)
        self.progress.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.clock_label = w.label(now, "", "soft")
        self.clock_label.grid(row=1, column=0, sticky="w", pady=(px(8), 0))
        self.tally_label = w.label(now, "", "soft")
        self.tally_label.grid(row=1, column=1, sticky="e", pady=(px(8), 0), padx=(px(12), 0))
        self.chips = w.StageChips(now)
        self.chips.grid(row=2, column=0, columnspan=2, sticky="w", pady=(px(10), 0))
        self.step_line = w.WorkingIndicator(now, texts.STARTING, kind="body")
        self.step_line.grid(row=3, column=0, columnspan=2, sticky="w", pady=(px(10), 0))
        self.working_label = w.WrapLabel(now, texts.SILENCE_IS_NORMAL, "soft", wrap=820)
        self.working_label.grid(row=4, column=0, columnspan=2, sticky="ew", padx=(px(24), 0), pady=(px(2), 0))
        self.now_rule = w.hairline(now)
        self.now_rule.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(px(10), px(8)))
        self.advice_label = w.WrapLabel(now, texts.ADVICE, "small", wrap=820)
        self.advice_label.grid(row=6, column=0, columnspan=2, sticky="ew")
        self.settings_label = w.label(now, "", "small")
        self.settings_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(px(2), 0))

    def _build_result(self) -> None:
        look = self._look
        px = look.px
        tip = self.help_line
        result = self.result_frame = w.Frame(self.card)
        result.columnconfigure(1, weight=1)
        self.result_icon = ttk.Label(result, style=look.style_name("TLabel", "surface"))
        self.result_icon.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, px(12)), pady=(px(3), 0))
        self.result_heading = w.WrapLabel(result, "", "heading", wrap=760)
        self.result_heading.grid(row=0, column=1, sticky="ew")
        self.result_body = w.WrapLabel(result, "", "body", wrap=760)
        self.result_body.grid(row=1, column=1, sticky="ew", pady=(px(2), 0))
        self.result_counts = w.Frame(result)
        self.result_counts.grid(row=2, column=1, sticky="ew", pady=(px(10), 0))
        self.result_counts.bind("<Configure>", lambda _e: self._flow_counts(), add="+")
        self._count_labels: List[ttk.Label] = []
        self.result_hint = w.WrapLabel(result, "", "body", wrap=760)
        self.result_hint.grid(row=3, column=1, sticky="ew", pady=(px(8), 0))
        self.result_where = w.WrapLabel(result, "", "soft", wrap=760)
        self.result_where.grid(row=4, column=1, sticky="ew", pady=(px(8), 0))
        links = self.result_links = w.Frame(result)
        links.grid(row=5, column=1, sticky="w", pady=(px(2), 0))
        self.result_open_results = w.LinkLabel(links, texts.BUTTON["open_results"], self._open_results)
        self.result_open_results.pack(side="left")
        self.result_open_summary = w.LinkLabel(links, texts.BUTTON["open_summary"], self._open_summary)
        self.result_open_summary.pack(side="left", padx=(px(18), 0))
        w.explain(self.result_open_results, texts.HOVER["open_results"], tip)
        w.explain(self.result_open_summary, texts.HOVER["open_summary"], tip)
        self.end_row = w.Frame(result)
        self.end_row.grid(row=6, column=1, sticky="ew", pady=(px(14), 0))
        self.end_buttons: Dict[str, ttk.Button] = {
            "continue": w.button(self.end_row, texts.BUTTON["continue"], self._continue, kind="default"),
            "retry_failed": w.button(self.end_row, texts.BUTTON["retry_failed_one"], self._retry_failed),
            "new_run": w.button(self.end_row, texts.BUTTON["new_run"], self._new_run),
            "back_to_samples": w.button(self.end_row, texts.BUTTON["back_to_samples"], self._back_to_samples),
        }
        for key, hover_key in (("continue", "continue"), ("retry_failed", "retry_failed"),
                               ("new_run", "new_run"), ("back_to_samples", "back_to_samples")):
            w.explain(self.end_buttons[key], texts.HOVER[hover_key], tip)
        self.shown_end_actions: List[str] = []

    # ------------------------------------------------------------------ the contract
    def begin(self, config: RunConfig, left_out: Sequence[Tuple[str, str]], *, resume: bool = False,
              runner_factory: Optional[Callable[..., Any]] = None) -> None:
        """Start a batch and show it.

        ``runner_factory(config, events, left_out=..., resume=...)`` returns an object with
        the ``BatchRunner`` interface; None means the real ``batch.BatchRunner``.
        """
        if self.is_running():
            log.warning("begin() was called while a run is in progress; ignored")
            return
        keep = bool(resume and self._config is not None and self._rows
                    and self._config.results_dir == config.results_dir)
        self._config = config
        self._events = queue.Queue()
        self._prepare_rows(config, list(left_out), keep)
        self._batch_names = [sample.name for sample in config.samples]
        self._note_earlier()
        self._finals = set()
        self._current, self._current_stage = "", None
        self._eta = None
        self._working = None
        self._pending_stage = self._pending_working = None
        self._log_buffer = []
        self._runner_gone_since = None
        self._battery_flashed = False
        self._dismissed = {}
        self._offer = None
        self._last_state = None
        self._runner_tells = set()
        self._started_iso, self._ended_iso, self._end_event = fsutil.now_iso(), "", None
        self.end_reason, self.end_counts = None, {}
        self._run_started = self._sample_started = self.clock()
        self._last_wall = time.time()
        self._last_battery = 0.0
        for kind in (run_dialogs.INTERRUPTED_KIND, run_dialogs.MISSING_KIND, "preflight", "fault",
                     "battery", "memory", "slept", "summary"):
            self.notices.dismiss(kind)
        self.log_pane.clear()
        self.chips.reset()
        self.progress.configure(maximum=max(1, self._total()), value=self._earlier_ended)
        self.step_line.set_text(texts.STARTING)
        self.step_line.start()
        self._paint_working()
        self.settings_label.configure(text=self._settings_words(config))
        self._set_open_enabled(bool(resume))
        self.phase = "starting"
        self._apply_phase()
        self._refresh_clocks(force=True)
        self._refresh_strip()
        try:
            if runner_factory is None:
                from .. import batch                     # the real runner, only when it is needed
                runner = batch.BatchRunner(config, self._events, engines=getattr(self.app, "engines", None),
                                           left_out=list(left_out), resume=resume)
            else:
                runner = runner_factory(config, self._events, left_out=list(left_out), resume=resume)
            self._runner = runner
            runner.start()
        except Exception:
            log.exception("the run could not be started")
            self._runner = None
            self._finish(model.BatchFinished(reason=EndReason.ENVIRONMENT, message=START_FAULT), started=False)
            return
        self._set_title("")
        self._schedule()

    def is_running(self) -> bool:
        return self.phase in _RUNNING_PHASES

    def confirm_close(self) -> bool:
        """The user closes the window. True means the window may close.

        With a run in progress the question of audit row 8.6 is asked; "Stop and close"
        stops the run at once and waits, without freezing the window, until it has ended.
        """
        if not self.is_running():
            return True
        if self.open_question is not None:               # a question is already waiting for an answer
            self.open_question.lift()
            return False
        if self._question_state() is None:               # nothing to lose yet: no question
            stop = True
        else:
            stop = run_dialogs.ask_close(self, self._question_state, on_open=self._question_opened)
            self.open_question = None
        if not self.is_running():                        # it ended while the question was open
            return bool(stop)
        if not stop:
            return False
        self._request_stop(StopMode.NOW)
        self._wait_for_end(self.CLOSE_WAIT_SECONDS)
        if self.is_running():                            # the runner did not answer in time
            try:
                osapi.keep_awake(False)
            except Exception:
                log.exception("keep_awake(False)")
        return True

    def offer_interrupted(self, results_dir: str) -> None:
        """Show the bar that offers to continue an interrupted run found in ``results_dir``.

        Nothing is shown when the folder holds no such run, or while a run is in progress.
        The folder is read in the background.
        """
        if self.is_running() or not results_dir:
            return

        def find() -> Any:
            finder = self.interrupted_finder
            if finder is None:
                from .. import batch
                finder = batch.interrupted_run
            return finder(results_dir)

        def found(info: Any, error: Optional[BaseException]) -> None:
            if error is not None or self.is_running():
                if error is not None:
                    log.warning("could not look for an interrupted run in %s: %r", results_dir, error)
                return
            if info is None:
                if self._offer is not None and self._offer_folder != results_dir:
                    self.notices.dismiss(run_dialogs.INTERRUPTED_KIND)     # that was about another folder
                    self._offer = None
                return
            offer = self._offer = run_dialogs.interrupted_offer(info)
            self._offer_folder = results_dir
            run_dialogs.show_interrupted_bar(self.notices, offer, self._continue_offer, self._forget_offer)
            self._fit_later()

        self._in_background(find, found)

    # ------------------------------------------------------------------ small public helpers
    @property
    def results_dir(self) -> str:
        """The results folder of the batch on this page ("" before the first one)."""
        return self._config.results_dir if self._config is not None else ""

    def note_interruption(self) -> None:
        """Windows is ending the session: let a run in progress record that (audit row 8.9)."""
        note = getattr(self._runner, "note_interruption", None)
        if self.is_running() and callable(note):
            try:
                note()
            except Exception:
                log.exception("note_interruption()")

    def has_offer(self) -> bool:
        """True while the bar that offers to continue an interrupted run is showing."""
        return self._offer is not None and run_dialogs.INTERRUPTED_KIND in self.notices.kinds()

    def select_sample(self, name: str) -> None:
        row = self._rows.get(name)
        if row is not None:
            self.table.select([row.row_id])
            self._refresh_strip()

    def status_of(self, name: str) -> RunStatus:
        return self._rows[name].status

    def statuses(self) -> Dict[str, RunStatus]:
        """Sample name -> status, in the order of the table."""
        return {name: self._rows[name].status for name in self._order}

    def reset(self) -> None:
        """Back to the empty page (New run). Ignored while a run is in progress."""
        if self.is_running():
            return
        self._config = None
        self._rows, self._order, self._samples, self._left_out = {}, [], {}, []
        self._batch_names, self._finals = [], set()
        self._earlier, self._earlier_ended = [], 0
        self.table.clear()
        self.log_pane.clear()
        self.notices.clear()
        self._offer = None
        self._set_open_enabled(False)
        self.phase = "idle"
        self.end_reason, self.end_counts = None, {}
        self._apply_phase()
        self._refresh_strip()

    # ------------------------------------------------------------------ rows
    def _new_row(self, name: str, status: RunStatus, note: str = "") -> _Row:
        self._row_serial += 1
        row = _Row("r%d" % self._row_serial, name, status, note)
        self._rows[name] = row
        self._order.append(name)
        return row

    def _prepare_rows(self, config: RunConfig, left_out: List[Tuple[str, str]], keep: bool) -> None:
        if not keep:
            self._rows, self._order, self._samples, self._left_out = {}, [], {}, []
        for sample in config.samples:
            self._samples[sample.name] = sample
            row = self._rows.get(sample.name)
            if row is None:
                self._new_row(sample.name, RunStatus.WAITING)
            else:
                row.status, row.note, row.outcome = RunStatus.WAITING, "", None
        known = {name for name, _reason in self._left_out}
        for name, reason in left_out:
            if name not in known:
                self._left_out.append((name, reason))
            row = self._rows.get(name)
            if row is None:
                self._new_row(name, RunStatus.LEFT_OUT, reason)
            elif name not in self._samples or row.status is RunStatus.LEFT_OUT:
                row.status, row.note = RunStatus.LEFT_OUT, reason
        # samples first (in their order), the left-out ones after them
        self._order.sort(key=lambda n: self._rows[n].status is RunStatus.LEFT_OUT)
        self.table.set_rows([self._table_row(self._rows[name]) for name in self._order])
        self.table.set_empty("")

    def _table_row(self, row: _Row) -> Dict[str, Any]:
        return {"id": row.row_id, "values": row_values(row.name, row.status, row.outcome, row.note),
                "status": w.status_kind(row.status), "dim": row.status in _DIM}

    def _paint_row(self, row: _Row) -> None:
        if row.row_id in self.table:
            self.table.update_row(row.row_id, row_values(row.name, row.status, row.outcome, row.note),
                                  status=w.status_kind(row.status), dim=row.status in _DIM)

    def _row(self, name: str) -> _Row:
        row = self._rows.get(name)
        if row is None:                                   # a name the page was not told about
            row = self._new_row(name, RunStatus.WAITING)
            self.table.add_row(row.row_id, row_values(name, row.status), status=w.status_kind(row.status),
                               dim=True, checked=None)
        return row

    # ------------------------------------------------------------------ the queue
    def _schedule(self) -> None:
        if self._job is None:
            self._job = self.after(self.POLL_MS, self._tick)

    def _tick(self) -> None:
        self._job = None
        try:
            self.process_events()
            self._refresh_clocks()
            self._watch_runner()
            self._watch_pc()
            if self.open_question is not None:
                self.open_question.refresh()
        except Exception:                                 # audit row 9.14: logged, the run goes on
            log.exception("a fault on the Run page")
        if self.is_running() or not self._events.empty():
            self._schedule()

    def process_events(self, limit: Optional[int] = None) -> int:
        """Take events off the queue and show them: at most ``limit`` (the per-tick bound),
        and for at most a few hundredths of a second. Returns how many were handled.

        Log lines are joined into one insert, and of several step or "working" events only
        the last is shown.
        """
        limit = self.MAX_EVENTS_PER_TICK if limit is None else max(1, int(limit))
        deadline = time.monotonic() + self.TICK_BUDGET_SECONDS
        handled = 0
        while handled < limit:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            handled += 1
            try:
                self._handle(event)
            except Exception:
                log.exception("could not show %r", event)
            if handled % 25 == 0 and time.monotonic() > deadline:
                break
        self._flush()
        return handled

    def _handle(self, event: model.Event) -> None:
        if isinstance(event, model.LogLine):
            self._log_buffer.append(event.text)
        elif isinstance(event, model.Working):
            self._pending_working = event
        elif isinstance(event, model.StageProgress):
            self._pending_stage = event
        elif isinstance(event, model.SampleStatusChanged):
            self._flush_stage()
            self._on_status(event.name, event.status, event.note)
        elif isinstance(event, model.SampleFinished):
            if event.outcome is not None:
                self._flush_stage()
                self._on_outcome(event.outcome)
        elif isinstance(event, model.BatchProgress):
            self._eta = None if event.eta_seconds is None else (float(event.eta_seconds), self.clock())
        elif isinstance(event, model.Notice):
            self._on_notice(event)
        elif isinstance(event, model.BatchStarted):
            self._on_started(event)
        elif isinstance(event, model.BatchFinished):
            self._flush()
            self._finish(event)

    def _flush(self) -> None:
        self._flush_stage()
        if self._log_buffer:
            lines, self._log_buffer = self._log_buffer, []
            self.log_pane.append(lines[-self.log_pane.max_lines:])

    def _flush_stage(self) -> None:
        stage, self._pending_stage = self._pending_stage, None
        working, self._pending_working = self._pending_working, None
        if stage is not None and stage.name == self._current and self.phase != "stopping_now":
            self._current_stage = stage.stage
            self._paint_chips()
            if stage.text:
                self.step_line.set_text(stage.text)
        if working is not None and working.name == self._current:
            self._working = (working.busy_threads, working.memory_bytes)
            self._paint_working()

    # ------------------------------------------------------------------ events
    def _on_started(self, event: model.BatchStarted) -> None:
        if event.names:
            self._batch_names = list(event.names)
            for name in event.names:
                self._row(name)
            self._note_earlier()
        total = self._total() or int(event.total or 0)
        self.progress.configure(maximum=max(1, total))
        self._run_started = self.clock()
        try:
            osapi.keep_awake(True)                        # belongs to this (the main) thread
        except Exception:
            log.exception("keep_awake(True)")
        if self.phase == "starting":
            self.phase = "running"
        self._apply_phase()

    def _on_status(self, name: str, status: RunStatus, note: str) -> None:
        row = self._row(name)
        was_final = row.status.is_final and row.status is not RunStatus.WAITING
        row.status = status
        if note:
            row.note = note
        if status.is_running:
            self._finals.discard(name)
            if name != self._current:
                self._current = name
                self._sample_started = self.clock()
                self._working = None
                self._current_stage = None
                self._paint_working()
                self._follow(name)
                self._set_title(texts.sample_header(self._position(), self._total(), name))
            self._current_stage = _STAGE_OF.get(status, self._current_stage)
            if self.phase != "stopping_now":
                self.step_line.set_text(texts.RUN_STATUS_LABEL[status])
            self.step_line.start()
            self._paint_chips()
            if self.phase == "starting":
                self.phase = "running"
            self._apply_phase()
        elif status.is_final:
            if not was_final and name in self._batch_names:
                self._finals.add(name)
            if name == self._current:
                self._paint_chips(final=status)
                self._current = ""
                if self.is_running() and self.phase != "stopping_now":
                    self.step_line.set_text(BETWEEN_SAMPLES)
            self._after_final()
        self._paint_row(row)
        if self._selected_name() == name:
            self._refresh_strip()

    def _follow(self, name: str) -> None:
        """Scroll the table to the running sample, with the next ones in view below it."""
        try:
            index = self._order.index(name)
        except ValueError:
            return
        ahead = self._order[min(len(self._order) - 1, index + 2)]
        self.table.see(self._rows[ahead].row_id)
        self.table.see(self._rows[name].row_id)

    def _on_outcome(self, outcome: SampleOutcome) -> None:
        row = self._row(outcome.name)
        row.outcome = outcome
        if outcome.note:
            row.note = outcome.note
        self._on_status(outcome.name, outcome.status, outcome.note)
        if outcome.status in _ASSEMBLED or outcome.status is RunStatus.FAILED:
            self._set_open_enabled(True)

    def _after_final(self) -> None:
        self.progress.configure(value=self._earlier_ended + len(self._finals))
        self._paint_tally()

    def _on_notice(self, notice: model.Notice) -> None:
        kind = notice.kind or "info"
        if kind in ("battery", "slept"):
            self._runner_tells.add(kind)                  # the runner watches this: the page need not
        actions: Sequence[Tuple[str, Callable[[], Any]]] = ()
        again = texts.BUTTON["write_summary_again"]
        if notice.text and again in notice.text:          # audit row 9.13: the words name this button
            kind, actions = "summary", ((again, self._write_summary_again),)
        self._show_notice(kind, notice.text, notice.level or "warn", actions=actions)

    def _show_notice(self, kind: str, text: str, level: str = "warn",
                     actions: Sequence[Tuple[str, Callable[[], Any]]] = ()) -> None:
        if not text:
            self._dismissed.pop(kind, None)
            self.notices.dismiss(kind)
            return
        if self._dismissed.get(kind) == text:             # the user closed these very words
            return
        if self.notices.text(kind) == text and self.notices.level(kind) == level:
            return
        self.notices.show(kind, text, level, actions=actions,
                          on_dismiss=lambda k, t=text: self._dismissed.__setitem__(k, t))
        self._fit_later()
        if level == "bad":
            self._flash()

    def _finish(self, event: model.BatchFinished, started: bool = True) -> None:
        """The batch has ended: the result panel, the taskbar, the title, the shell."""
        if self.phase == "ended":
            return
        reason = event.reason
        for name in self._batch_names:                    # nobody may be left "running" or "waiting"
            row = self._rows.get(name)
            if row is None or row.status.is_final and row.status is not RunStatus.WAITING:
                continue
            row.status = RunStatus.STOPPED if row.status.is_running else RunStatus.NOT_STARTED
            self._paint_row(row)
        self._current = ""
        self.step_line.stop()
        self.phase = "ended"
        self.end_reason = reason
        self._ended_iso, self._end_event = fsutil.now_iso(), event
        try:
            osapi.keep_awake(False)                       # the PC may sleep again
        except Exception:
            log.exception("keep_awake(False)")
        for kind in ("battery", "memory"):               # they were about the run; it is over
            self.notices.dismiss(kind)
        counts = self.end_counts = tally(self._rows[name].status for name in self._order)
        if not self._order and event.counts:
            counts = self.end_counts = dict(event.counts)
        seconds = float(event.seconds or 0.0) or max(0.0, self.clock() - self._run_started)
        self._show_result(reason, end_message(reason, event.message, seconds, time.strftime("%H:%M")), counts)
        self._apply_phase()
        if not self.table.selection():                    # bring the first failure into view
            for name in self._order:
                if self._rows[name].status is RunStatus.FAILED:
                    self.table.select([self._rows[name].row_id])
                    break
        self._refresh_strip()
        if any(counts.get(status.value, 0) for status in _ASSEMBLED) or counts.get(RunStatus.FAILED.value, 0):
            self._set_open_enabled(True)
        self._set_title(title_text(reason, counts) if started else "")
        self._flash()
        try:
            self.bell()                                   # one short system sound
        except tk.TclError:
            pass
        if self.open_question is not None:
            self.open_question.refresh()                  # closes it: nothing is left to stop
        try:
            self.app.run_ended()
        except Exception:
            log.exception("app.run_ended()")

    # ------------------------------------------------------------------ painting
    def _note_earlier(self) -> None:
        """Which rows of the table are not part of this batch (a run that is continued keeps
        the rows of what ran before), and how many of them have ended."""
        batch = set(self._batch_names)
        self._earlier = [name for name in self._order
                         if name not in batch and self._rows[name].status is not RunStatus.LEFT_OUT]
        self._earlier_ended = sum(1 for name in self._earlier if self._rows[name].status in _ENDED)

    def _total(self) -> int:
        """The samples of the whole list: those of this batch and those that ran before it."""
        return len(self._earlier) + len(self._batch_names)

    def _position(self) -> int:
        try:
            return self._earlier_ended + self._batch_names.index(self._current) + 1
        except ValueError:
            return min(self._total(), self._earlier_ended + len(self._finals) + 1)

    def _counts_now(self) -> Tuple[int, int]:
        done = failed = 0
        for name in list(self._finals) + self._earlier:
            status = self._rows[name].status
            if status in (RunStatus.DONE, RunStatus.DONE_NOTES):
                done += 1
            elif status is RunStatus.FAILED:
                failed += 1
        return done, failed

    def _paint_tally(self) -> None:
        words = tally_text(*self._counts_now())
        if self.phase in ("stopping_after", "stopping_now") and self._batch_names:
            words = "%s %s" % (position_text(self._position(), self._total()), words)
        self.tally_label.configure(text=words)

    def _paint_chips(self, final: Optional[RunStatus] = None) -> None:
        sample = self._samples.get(self._current)
        lanes = len(sample.lanes) if sample is not None else 1
        if self._current_stage is Stage.JOIN_LANES:
            lanes = max(2, lanes)
        trimmed = sample.trim if sample is not None else True
        if self._current_stage is Stage.TRIM:
            trimmed = True
        self.chips.show_stage(Stage.JOIN_LANES, lanes > 1)
        for key, state in stage_states(self._current_stage, trimmed=trimmed, lanes=lanes, final=final).items():
            if self.chips.state_of(key) != state:
                self.chips.set(key, state)

    def _refresh_clocks(self, force: bool = False) -> None:
        if not self.is_running() and not force:
            return
        now = self.clock()
        sample_seconds: Optional[float] = None
        if self._current:
            sample_seconds = max(0.0, now - self._sample_started)
        left = None if self._eta is None else time_left_now(self._eta[0], self._eta[1], now)
        text = clock_line(sample_seconds, max(0.0, now - self._run_started), left)
        if text != self._clock_text:
            self._clock_text = text
            self.clock_label.configure(text=text)

    def _apply_phase(self) -> None:
        """Everything that depends on the phase alone: header, card, Stop button."""
        phase = self.phase
        total = self._total()
        if phase == "ended":
            self.head.grid_remove()
            self.now_frame.grid_remove()
            self.card.grid()
            self.result_frame.grid(row=0, column=0, sticky="ew")
        elif phase == "idle":
            self.head.grid()
            self.intro.grid()
            self.card.grid_remove()
            self.title_label.configure(text=texts.PAGE_TITLE["run"])
            self.table.set_empty(IDLE_TEXT)
        else:
            self.head.grid()
            self.intro.grid()
            self.result_frame.grid_remove()
            self.card.grid()
            self.now_frame.grid(row=0, column=0, sticky="ew")
            if phase == "starting" or not (self._current or self._finals):
                title = texts.STARTING
            elif phase == "stopping_now":
                title = STOPPING_NOW
            elif phase == "stopping_after" and self._current:
                title = texts.STOPPING_AFTER.format(sample=self._current)
            elif self._current:
                title = texts.sample_header(self._position(), total, self._current)
            else:
                title = str(self.title_label.cget("text"))
            self.title_label.configure(text=title)
            self._paint_tally()
        if phase == "stopping_after":
            self.keep_going_button.grid()
        else:
            self.keep_going_button.grid_remove()
        label, enabled = stop_button(phase, bool(self._current) or self._last_state is not None)
        self.stop_button.configure(text=label)
        self.stop_button.state(["!disabled"] if enabled else ["disabled"])
        self._apply_compact()
        self._fit_later()

    # ------------------------------------------------------------------ a short window
    def _paint_working(self) -> None:
        brief = self.compact >= 3
        words = working_text(None) if self._working is None else working_text(*self._working, brief=brief)
        if str(self.working_label.cget("text")) != words:
            self.working_label.configure(text=words)

    @staticmethod
    def _show(widget: Any, visible: bool) -> None:
        if visible:
            widget.grid()
        else:
            widget.grid_remove()

    def _apply_compact(self) -> None:
        """Show or leave out the parts that give way when the window is short."""
        level = self.compact
        show = self._show
        if self.phase != "ended":
            show(self.intro, level < 1)
        show(self.settings_label, level < 2)
        show(self.result_where, level < 2)
        show(self.now_rule, level < 3)
        show(self.advice_label, level < 3)
        show(self.result_links, level < 3)
        show(self.chips, level < 4)
        show(self.result_hint, bool(self._hint) and level < 4)
        heading, body = self._outcome
        if level >= 4 and body:                           # the outcome in one line
            heading, body = "%s %s" % (heading, body), ""
        if str(self.result_heading.cget("text")) != heading:
            self.result_heading.configure(text=heading)
        if str(self.result_body.cget("text")) != body:
            self.result_body.configure(text=body)
        show(self.result_body, bool(body))
        self._paint_working()
        px = self._look.px
        # In the shortest window there is no room for the table and the log together: while
        # the log is shown it takes the table's place (Hide log brings the table back).
        swap = self._log_shown and level >= 4
        show(self.table, not swap)
        self.rowconfigure(3, weight=0 if swap else 3, minsize=0 if swap else px(96))
        # nothing to say: no empty box
        show(self.strip, bool(self._order) and not swap and not (level >= 2 and not self._selected_name()))
        close = level >= 3                                # the blocks move closer together
        for block, roomy, tight in ((self.card, 12, 6), (self.table, 12, 6), (self.strip, 8, 4)):
            if block.winfo_manager():                     # (to configure a hidden one would show it)
                block.grid_configure(pady=(px(tight if close else roomy), 0))

    def _compact_gain(self, level: int) -> int:
        """The height that going back from ``level`` to the roomier one would take."""
        px = self._look.px
        ended = self.phase == "ended"
        gain = 0
        if level == 1:
            return 0 if ended else self.intro.winfo_reqheight() + px(4)
        if level == 2:
            if ended:
                gain += self.result_where.winfo_reqheight() + px(8)
            elif self.phase != "idle":
                gain += self.settings_label.winfo_reqheight() + px(2)
            if self._order and not self._selected_name():
                gain += self.strip.winfo_reqheight() + px(8)
        elif level == 3:
            if ended:
                gain += self.result_links.winfo_reqheight() + px(2)
            elif self.phase != "idle":
                gain += self.advice_label.winfo_reqheight() + px(19)
            gain += px(16)                                # the gaps between the blocks
            if not ended and self.phase != "idle":
                if self._working is not None:             # the longer "working" line may need a second line
                    longer = working_text(*self._working)
                    try:
                        if self._look.text_width(longer, "body") > self.working_label.winfo_width() - px(4):
                            gain += self.working_label.winfo_reqheight()
                    except tk.TclError:
                        gain += self.working_label.winfo_reqheight()
        else:
            if ended:
                gain += (self.result_hint.winfo_reqheight() + px(8) if self._hint else 0) + px(24)
            elif self.phase != "idle":
                gain += self.chips.winfo_reqheight() + px(10)
            if self._log_shown:                           # the table and the strip would come back
                gain += self.table.winfo_reqheight() + px(6)
                if self._order and self._selected_name():
                    gain += self.strip.winfo_reqheight() + px(4)
        return gain

    def _fit_later(self) -> None:
        """Look at the room again shortly, once the texts have wrapped to their new widths."""
        if self._fit_job is None:
            try:
                self._fit_job = self.after(self.FIT_DELAY_MS, self._fit)
            except tk.TclError:
                pass

    def _fit(self) -> None:
        """Choose how compact the page must be for the table to keep its rows.

        One step at a time, and a step back only when there is clearly room for it, so the
        page never flips between two layouts.
        """
        self._fit_job = None
        if not self.winfo_ismapped():
            return
        have, need = self.winfo_height(), self.winfo_reqheight()
        if have <= 1:
            return
        now = time.monotonic()
        if have != self._fit_height:
            self._fit_height, self._fit_changes = have, []
        self._fit_changes = [moment for moment in self._fit_changes if now - moment < 3.0]
        if len(self._fit_changes) >= 6:                  # it will not settle at this height: leave it
            return
        level = self.compact
        # The table asks for four rows. The opening sentence, the settings line and an empty
        # strip go before it loses one; the advice about sleep goes only when it would be
        # down to two rows, and the stage chips only when it would be down to one.
        row = int(getattr(self._look, "row_height", 0) or self._look.px(28))
        give = (0, 0, row, 2 * row, 2 * row)
        if need - give[level] > have and level < 4:
            self.compact = level + 1
        elif level > 0 and need + self._compact_gain(level) - give[level - 1] + self._look.px(4) <= have:
            self.compact = level - 1
        else:
            return
        self._fit_changes.append(now)
        self._apply_compact()
        self._fit_later()

    def _settings_words(self, config: RunConfig) -> str:
        try:                                              # the same line as in the summaries
            return "Settings: %s." % settings_map.describe(
                config.trim, config.assembly, any(sample.trim for sample in config.samples))
        except Exception:
            log.exception("settings_map.describe()")
        trimming: Optional[bool] = None
        if any(sample.trim for sample in config.samples):
            trimming = config.trim.is_standard()
        return texts.settings_line(trimming, config.assembly.is_standard(), config.assembly.threads)

    def _set_title(self, text: str) -> None:
        try:
            self.app.set_running_title(text)
        except Exception:
            log.exception("app.set_running_title()")

    def _flash(self) -> None:
        try:
            osapi.flash_taskbar(getattr(self.app, "root", None) or self.winfo_toplevel())
        except Exception:
            log.exception("flash_taskbar()")

    def _set_open_enabled(self, enabled: bool) -> None:
        flag = ["!disabled"] if enabled else ["disabled"]
        self.open_results_button.state(flag)
        self.open_summary_button.state(flag)

    # ------------------------------------------------------------------ the result panel
    def _show_result(self, reason: EndReason, message: str, counts: Mapping[str, int]) -> None:
        look = self._look
        level = end_level(reason, counts)
        self._outcome = split_message(strip_counts(message))
        self.result_icon.configure(image=look.icons.glyph(level, _LEVEL_COLOUR[level], 22))
        for made in self._count_labels:
            made.destroy()
        self._count_labels = []
        for status, words in count_words(counts):
            colour = _KIND_COLOUR.get(w.status_kind(status), "border_strong")
            made = ttk.Label(self.result_counts, text=words, compound="left",
                             image=look.icons.dot(colour, size=10),
                             style=look.style_name("TLabel", "surface"))
            self._count_labels.append(made)
        self._flow_counts()
        failed = int(counts.get(RunStatus.FAILED.value, 0))
        self._hint = failed_hint(failed)
        self.result_hint.configure(text=self._hint)
        assembled = sum(int(counts.get(status.value, 0)) for status in _ASSEMBLED)
        folder = self._config.results_dir if self._config is not None else ""
        self.result_where.configure(text=where_text(folder, assembled) if folder else "")
        actions = self.shown_end_actions = end_actions(
            reason, counts, can_new_run=callable(getattr(self.app, "new_run", None)))
        self.end_buttons["retry_failed"].configure(text=retry_label(failed))
        for made in self.end_buttons.values():
            made.pack_forget()
            made.state(["!disabled"])
        for index, key in enumerate(actions):
            self.end_buttons[key].pack(side="left", padx=(0 if index == 0 else look.px(8), 0))
        self._apply_compact()
        self._fit_later()

    def _flow_counts(self) -> None:
        """Lay the counts out in a line, and on further lines when one is not enough."""
        if not self._count_labels:
            return
        look = self._look
        gap = look.px(18)
        width = self.result_counts.winfo_width()
        if width <= 1:
            width = look.px(760)
        x = row = column = 0
        for made in self._count_labels:
            need = made.winfo_reqwidth() + gap
            if column > 0 and x + need - gap > width:
                row, column, x = row + 1, 0, 0
            made.grid(row=row, column=column, sticky="w", padx=(0, gap), pady=(0, look.px(2)))
            column += 1
            x += need

    # ------------------------------------------------------------------ the selected row
    def _selected_name(self) -> str:
        chosen = self.table.selection()
        if len(chosen) != 1:
            return ""
        for name in self._order:
            if self._rows[name].row_id == chosen[0]:
                return name
        return ""

    def _on_select(self, _ids: List[str]) -> None:
        self._refresh_strip()

    def _refresh_strip(self) -> None:
        name = self._selected_name()
        if not name:
            words, buttons = (texts.SELECT_A_ROW if self._order else ""), []
            kind = "Soft.TLabel"
        else:
            row = self._rows[name]
            words, buttons = strip_content(name, row.status, row.outcome, row.note)
            kind = "TLabel"
        self.strip_text.configure(text=words, style=w.style_for(self.strip, kind))
        for key, made in self.strip_buttons.items():
            if key in buttons:
                made.grid()
            else:
                made.grid_remove()
        self._apply_compact()
        self._fit_later()

    def _on_activate(self, row_id: str) -> None:
        """Double-click or Enter on a row: the same as the first button of its strip."""
        self.table.select([row_id], see=False)
        self._refresh_strip()
        name = self._selected_name()
        if not name:
            return
        row = self._rows[name]
        _words, buttons = strip_content(name, row.status, row.outcome, row.note)
        if buttons:
            self._strip_action(buttons[0])

    def _strip_action(self, key: str) -> None:
        name = self._selected_name()
        if not name or self._config is None:
            return
        row = self._rows[name]
        outcome = row.outcome
        folder = (outcome.folder if outcome is not None and outcome.folder else
                  results.sample_dir(self._config.results_dir, name))
        if key == "open_folder":
            self._open(folder, self._config.results_dir, name)
        elif key == "open_report":
            path = (outcome.report_html if outcome is not None and outcome.report_html else
                    results.report_html_path(self._config.results_dir, name))
            self._open(path, folder, "%s: report.html" % name)
        elif key == "open_log":
            path = outcome.log if outcome is not None else ""
            self._open(path or folder, folder, texts.BUTTON["open_log"])

    def _open(self, path: str, fallback: str = "", name: str = "") -> bool:
        """Open a file or a folder; if it will not open, the folder that holds it; if that
        fails too, say so in the help line. Never an error box for a missing file."""
        try:
            if path and osapi.open_path(path):
                return True
            opened = bool(fallback) and fallback != path and osapi.open_path(fallback)
        except Exception:
            log.exception("could not open %s", path)
            opened = False
        words = (OPENED_FOLDER_INSTEAD if opened else NOT_OPENED).format(name=name or path)
        if self.help_line is not None:
            self.help_line.say(words, "info" if opened else "warn")
        return bool(opened)

    def _open_results(self) -> None:
        if self._config is not None:
            self._open(self._config.results_dir, "", texts.RESULTS_FOLDER_LABEL)

    def _open_summary(self) -> None:
        if self._config is not None:
            folder = self._config.results_dir
            self._open(results.summary_html_path(folder), folder, "run_summary.html")

    # ------------------------------------------------------------------ the log
    def _toggle_log(self) -> None:
        self._log_shown = not self._log_shown
        if self._log_shown:
            self.log_pane.grid()
            self.rowconfigure(5, weight=2)
            self.log_toggle.configure(text=texts.BUTTON["hide_log"])
            self.after_idle(self._log_to_end)             # the newest lines are the ones to read
        else:
            self.log_pane.grid_remove()
            self.rowconfigure(5, weight=0)
            self.log_toggle.configure(text=texts.BUTTON["show_log"])
        self._apply_compact()
        self._fit_later()

    def _log_to_end(self) -> None:
        try:
            self.log_pane.text.see("end")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ stopping
    def _question_state(self) -> Optional[run_dialogs.RunState]:
        """What a Stop or close question may say right now; None when nothing is running.

        In the moment between two samples the words about the sample that has just ended
        stay, so that an open question does not vanish and come back.
        """
        if not self.is_running():
            return None
        if not self._current:
            return self._last_state
        name, seconds = self._current, max(0.0, self.clock() - self._sample_started)
        try:
            live_name, live_seconds = self._runner.current()
            if live_name == name:
                seconds = float(live_seconds)
        except Exception:
            pass
        finished = sum(1 for sample in list(self._finals) + self._earlier
                       if self._rows[sample].status in (RunStatus.DONE, RunStatus.DONE_NOTES))
        self._last_state = run_dialogs.RunState(name, self._rows[name].status, seconds, finished)
        return self._last_state

    def _question_opened(self, question: run_dialogs.FollowingQuestion) -> None:
        self.open_question = question

    def _stop_pressed(self) -> None:
        if self.phase not in ("running", "stopping_after") or self.open_question is not None:
            return
        answer = run_dialogs.ask_stop(self, self._question_state,
                                      pending_after=self.phase == "stopping_after",
                                      on_open=self._question_opened)
        self.open_question = None
        if not self.is_running():                         # the run ended while the question was open
            return
        if answer == StopMode.NOW.value:
            self._request_stop(StopMode.NOW)
        elif answer == StopMode.AFTER_CURRENT.value:
            self._request_stop(StopMode.AFTER_CURRENT)

    def _request_stop(self, mode: StopMode) -> None:
        runner = self._runner
        if runner is None or not self.is_running():
            return
        try:
            runner.request_stop(mode)
        except Exception:
            log.exception("request_stop(%s)", mode)
            return
        if mode is StopMode.NOW:
            self.phase = "stopping_now"
            self.step_line.set_text(STOPPING_NOW_STEP.format(sample=self._current) if self._current
                                    else STOPPING_NOW)
        else:
            self.phase = "stopping_after"
        self._apply_phase()

    def _keep_going(self) -> None:
        """ "Keep going after all": undo a pending "Stop after <sample>"."""
        if self.phase != "stopping_after" or self._runner is None:
            return
        try:
            self._runner.cancel_stop()
        except Exception:
            log.exception("cancel_stop()")
            return
        self.phase = "running"
        self._apply_phase()

    def _wait_for_end(self, timeout: float) -> None:
        """Let the window live until the batch has ended (closing during a run)."""
        deadline = time.monotonic() + timeout
        while self.is_running() and time.monotonic() < deadline:
            self.process_events()
            self._watch_runner(patience=0.3)
            try:
                self.update()
            except tk.TclError:
                return
            time.sleep(0.02)

    def _watch_runner(self, patience: float = 2.0) -> None:
        """A runner that died without saying so must not leave the page "running" for ever."""
        runner = self._runner
        alive = getattr(runner, "is_alive", None)
        if not self.is_running() or runner is None or alive is None:
            return
        if alive() or not self._events.empty():
            self._runner_gone_since = None
            return
        now = time.monotonic()
        if self._runner_gone_since is None:
            self._runner_gone_since = now
        elif now - self._runner_gone_since >= patience:
            log.error("the runner ended without announcing the end of the batch")
            stopped = self.phase in ("stopping_now", "stopping_after")
            self._finish(model.BatchFinished(
                reason=EndReason.STOPPED if stopped else EndReason.ENVIRONMENT,
                message="" if stopped else RUNNER_GONE))

    # ------------------------------------------------------------------ the PC: battery, sleep
    def _watch_pc(self) -> None:
        if not self.is_running():
            return
        wall = time.time()
        gap = wall - self._last_wall
        if self._last_wall and gap > self.SLEEP_GAP_SECONDS:
            start = time.strftime("%H:%M", time.localtime(self._last_wall))
            end = time.strftime("%H:%M", time.localtime(wall))
            if "slept" not in self._runner_tells:
                self._dismissed.pop("slept", None)
                self._show_notice("slept", texts.slept(start, end), "info")
            self._log_buffer.append(WINDOW_PAUSED_LOG.format(start=start, end=end))
        self._last_wall = wall
        mono = time.monotonic()
        if "battery" in self._runner_tells or mono - self._last_battery < self.BATTERY_EVERY_SECONDS:
            return
        self._last_battery = mono
        try:
            on_battery, percent, has_battery = osapi.power_status()
        except Exception:
            return
        if not has_battery or on_battery is None:         # a desktop PC, or not known: say nothing
            return
        if not on_battery:
            self._battery_flashed = False
            self._show_notice("battery", "")
        elif percent is not None and percent <= self.LOW_BATTERY_PERCENT:
            self._dismissed.pop("battery", None)
            self._show_notice("battery", texts.battery_low(int(percent)), "bad")
        else:
            self._show_notice("battery", texts.ON_BATTERY, "warn")

    # ------------------------------------------------------------------ the summary files
    def _write_summary_again(self) -> None:
        """Audit row 9.13: the summary was open in another program when the run ended."""
        config, runner, event = self._config, self._runner, self._end_event
        if config is None or self._busy:
            return
        outcomes = [self._rows[name].outcome for name in self._order if self._rows[name].outcome is not None]
        info = {
            "started": self._started_iso, "finished": self._ended_iso or None,
            "seconds": float(event.seconds) if event is not None else 0.0,
            "end_reason": event.reason if event is not None else None,
            "message": event.message if event is not None else "",
            "settings": self._settings_words(config), "events": [],
            "samples": [sample.name for sample in self._samples.values()],
        }
        left_out = list(self._left_out)

        def work() -> List[str]:
            again = getattr(runner, "write_summary_again", None)
            if callable(again):                           # the runner knows more about its run
                return list(again() or [])
            return list(summary_report.write_run_summary(
                config.results_dir, this_run=outcomes, left_out=left_out, run_info=info))

        def done(still: Optional[List[str]], error: Optional[BaseException]) -> None:
            self._busy = False
            if error is not None:
                log.error("the summary could not be written again: %r", error)
            written = error is None and not still
            if written:
                self.notices.dismiss("summary")
            if self.help_line is not None:
                self.help_line.say(SUMMARY_WRITTEN if written else SUMMARY_STILL_OPEN,
                                   "info" if written else "warn")

        self._busy = True
        self._in_background(work, done)

    # ------------------------------------------------------------------ after the run
    def _subset(self, wanted: Sequence[RunStatus]) -> Optional[RunConfig]:
        if self._config is None:
            return None
        samples = [self._samples[name] for name in self._order
                   if name in self._samples and self._rows[name].status in wanted]
        return replace(self._config, samples=samples) if samples else None

    def _continue(self) -> None:
        """Continue: the samples that were stopped or never started. Nothing is asked."""
        config = self._subset(PENDING)
        if config is not None:
            self._restart(config, self._left_out)

    def _retry_failed(self) -> None:
        config = self._subset((RunStatus.FAILED,))
        if config is not None:
            self._restart(config, self._left_out)

    def _continue_offer(self) -> None:
        offer = self._offer
        if offer is None or offer.config is None:
            return
        missing = len(offer.left_out)

        def started() -> None:
            if missing and self.is_running():
                self._show_notice(run_dialogs.MISSING_KIND, run_dialogs.missing_sentence(missing), "warn")

        self._restart(offer.config, offer.left_out, then=started)

    def _forget_offer(self) -> None:
        self._offer = None

    def _restart(self, config: RunConfig, left_out: Sequence[Tuple[str, str]],
                 then: Optional[Callable[[], Any]] = None) -> None:
        """Check in the background that the run can start, then hand it to the shell."""
        if self.is_running() or self._busy:
            return
        self._busy = True
        for made in self.end_buttons.values():
            made.state(["disabled"])

        def check() -> List[str]:
            preflight = self.preflight
            if preflight is None:
                from .. import batch
                return list(batch.preflight_batch(config, getattr(self.app, "engines", None)))
            return list(preflight(config))

        def checked(problems: Optional[List[str]], error: Optional[BaseException]) -> None:
            self._busy = False
            for made in self.end_buttons.values():
                made.state(["!disabled"])
            if error is not None:
                log.error("the check before the run failed: %r", error)
                self.notices.show("fault", START_FAULT, "bad")
                return
            if problems:
                self.notices.show("preflight", " ".join([CANNOT_START] + list(problems)), "bad")
                return
            self.notices.dismiss("preflight")
            self.app.start_run(config, list(left_out), resume=True)
            if then is not None:
                then()

        self._in_background(check, checked)

    def _new_run(self) -> None:
        if self.is_running():
            return
        self.reset()
        self._set_title("")
        self.app.new_run()

    def _back_to_samples(self) -> None:
        if self.is_running():
            return
        self._set_title("")
        self.app.show_step(0)

    # ------------------------------------------------------------------ plumbing
    def _in_background(self, work: Callable[[], Any],
                       done: Callable[[Any, Optional[BaseException]], Any]) -> None:
        """Run ``work`` off the main thread (it may read a slow drive) and give its result,
        or its error, to ``done`` on the main thread."""
        box: Dict[str, Any] = {}

        def run() -> None:
            try:
                box["result"] = work()
            except BaseException as error:               # noqa: BLE001 - handed to the main thread
                box["error"] = error

        worker = threading.Thread(target=run, name="run-page-check", daemon=True)
        worker.start()

        def poll() -> None:
            try:
                if not self.winfo_exists():              # the window was closed meanwhile
                    return
                if worker.is_alive():
                    self.after(40, poll)
                    return
            except tk.TclError:
                return
            done(box.get("result"), box.get("error"))

        self.after(10, poll)

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self:
            return
        for job in (self._job, self._fit_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._job = self._fit_job = None
