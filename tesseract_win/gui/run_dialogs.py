# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The questions of the Run page: Stop, closing the window, continuing an interrupted run.

Nothing here touches the runner. Each function shows something and answers; the Run page
acts on the answer.

- :func:`ask_stop`: the Stop question of audit row A-8. It names the running sample and the
  minutes that would be lost. Keep running is the default, and Esc, the close box and Enter
  all mean Keep running. The run goes on underneath while it is open, so the question
  *follows the run*: if the sample it names finishes, its words move on to the next sample,
  and if the whole run ends it closes itself with "keep".
- :func:`ask_close`: the same kind of question when the window is closed during a run.
- :func:`interrupted_offer` and :func:`show_interrupted_bar`: the bar that offers to continue
  a run that was stopped, or cut short by Windows, a crash or a power cut (rows 8.9 to 8.11),
  and what it says when read files have gone missing since.

The words not yet in ``texts.py`` are in the marked block below.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .. import model
from ..model import RunStatus, StopMode
from . import texts
from . import widgets as w

__all__ = [
    "RunState", "FollowingQuestion", "stop_question", "close_question", "ask_stop", "ask_close",
    "InterruptedOffer", "interrupted_offer", "show_interrupted_bar", "when_words",
    "missing_sentence", "INTERRUPTED_KIND", "MISSING_KIND", "MISSING_READS_NOTE",
]

# =========================================================================================
# WORDS THAT ARE NOT IN texts.py YET (to be moved there by its owner; listed in my report)
# =========================================================================================
#: The first button of the Stop question once "Stop after <sample>" has been chosen.
WAIT_FOR = "Wait for {sample}"
#: Body of that second question: the first sentence; the rest is the usual Stop question.
ALREADY_STOPPING = "The run already stops when {sample} has finished."
INTERRUPTED_BY_WINDOWS = "The last run was interrupted{when} because Windows shut down or restarted."
INTERRUPTED_UNEXPECTEDLY = "The last run did not finish: it ended unexpectedly{when}."
INTERRUPTED_STOPPED = "The last run in this folder was stopped{when} before all samples were done."
INTERRUPTED_PROGRESS = "{finished} of {total} {are} finished."
MISSING_READS_ONE = "1 sample was left out because its read files are no longer where they were."
MISSING_READS_MANY = "{n} samples were left out because their read files are no longer where they were."
MISSING_READS_WILL_ONE = ("1 sample will be left out, because its read files are no longer where "
                          "they were.")
MISSING_READS_WILL_MANY = ("{n} samples will be left out, because their read files are no longer "
                           "where they were.")
#: The note of such a row in the table.
MISSING_READS_NOTE = ("The read files are no longer where they were. Put them back, or add the "
                      "sample again from where the files are now.")
CANNOT_CONTINUE = ("It cannot be continued from here: the read files of the samples that are left "
                   "are no longer where they were. Put them back or reconnect their drive, then "
                   "choose this results folder again.")
_ONE_ARE_KEPT = "The 1 finished sample are kept"       # what texts.close_question says today
_ONE_IS_KEPT = "The 1 finished sample is kept"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# =========================================================================================

#: Kinds of the notice bars made here.
INTERRUPTED_KIND = "interrupted"
MISSING_KIND = "missing_reads"


# --------------------------------------------------------------------------- the running sample

@dataclass(frozen=True)
class RunState:
    """What a question needs to know about the run at this moment."""
    sample: str
    status: RunStatus
    seconds: float               # how long the sample has been running
    finished: int                # samples already finished (their results are kept)


StateSource = Callable[[], Optional[RunState]]


def stop_question(state: RunState, *, pending_after: bool = False) -> texts.Question:
    """The Stop question for ``state``.

    Normally the three choices of ``texts.stop_question``. With ``pending_after`` ("Stop
    after <sample>" was already chosen, the button now reads Stop now) there are two: wait
    for the sample, or stop at once. Keys: ``"keep"``, ``"after_current"``, ``"now"``.
    """
    question = texts.stop_question(state.sample, state.status, state.seconds, state.finished)
    if not pending_after:
        return question
    keep = replace(question.choice("keep"), label=WAIT_FOR.format(sample=state.sample))
    body = "%s\n%s" % (ALREADY_STOPPING.format(sample=state.sample), question.body)
    return replace(question, body=body, choices=(keep, question.choice(StopMode.NOW.value)))


class FollowingQuestion:
    """A question about the run that stays true while the run goes on underneath it.

    ``build(state)`` makes the ``texts.Question`` for a moment of the run; ``source()`` gives
    that moment, or None once nothing is running. :meth:`refresh` (the Run page calls it on
    its clock tick) rewrites the words and the button labels in place, and closes the
    question with its cancel key when the run has ended by itself.
    """

    def __init__(self, parent: Any, source: StateSource,
                 build: Callable[[RunState], texts.Question]):
        self.parent = parent
        self.source = source
        self.build = build
        self.dialog: Optional[w.Dialog] = None
        self.question: Optional[texts.Question] = None
        self.ended_by_itself = False

    @property
    def is_open(self) -> bool:
        return self.dialog is not None and bool(_exists(self.dialog))

    def show(self, on_open: Optional[Callable[["FollowingQuestion"], Any]] = None) -> str:
        """Open, wait for an answer (the window's event loop keeps running), close.

        Returns the key of the chosen button; the cancel key when nothing runs any more.
        """
        state = self.source()
        if state is None:
            self.ended_by_itself = True
            return "keep"
        question = self.question = self.build(state)
        self.dialog = w.Dialog(self.parent, title=question.title, body=question.body,
                               choices=question.choices, default=question.default,
                               cancel=question.cancel, level="warn")
        if on_open is not None:
            on_open(self)
        try:
            return self.dialog.show()
        finally:
            self.dialog = None

    def refresh(self) -> None:
        if not self.is_open:
            return
        dialog = self.dialog
        assert dialog is not None
        state = self.source()
        if state is None:                              # the run ended while the question was open
            self.ended_by_itself = True
            dialog.invoke(dialog.cancel)
            return
        question = self.build(state)
        if self.question is not None and question == self.question:
            return
        self.question = question
        dialog.body.configure(text=question.body)
        for choice in question.choices:
            made = dialog.buttons.get(choice.key)
            if made is not None and str(made.cget("text")) != choice.label:
                made.configure(text=choice.label)

    def invoke(self, key: str) -> None:
        """Press one of the buttons from the program (tests, the demonstration)."""
        if self.is_open and self.dialog is not None:
            self.dialog.invoke(key)

    def lift(self) -> None:
        if self.is_open and self.dialog is not None:
            self.dialog.lift()
            self.dialog.focus_set()


def _exists(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except Exception:
        return False


def ask_stop(parent: Any, source: StateSource, *, pending_after: bool = False,
             on_open: Optional[Callable[[FollowingQuestion], Any]] = None) -> str:
    """Ask the Stop question. Returns ``"keep"``, ``"after_current"`` or ``"now"``.

    ``"keep"`` is also the answer of Esc, Enter, the close box, and of a run that ended
    while the question was open.
    """
    asked = FollowingQuestion(parent, source, lambda state: stop_question(state, pending_after=pending_after))
    answer = asked.show(on_open)
    return "keep" if asked.ended_by_itself else answer


def close_question(state: RunState) -> texts.Question:
    """The question on closing the window during a run (audit row 8.6).

    Keys: ``"keep"`` (the default, also Esc) and ``"stop_and_close"``.
    """
    question = texts.close_question(state.sample, state.status, state.seconds, state.finished)
    # texts.close_question says "The 1 finished sample are kept": put right here until it is.
    return replace(question, body=question.body.replace(_ONE_ARE_KEPT, _ONE_IS_KEPT))


def ask_close(parent: Any, source: StateSource, *,
              on_open: Optional[Callable[[FollowingQuestion], Any]] = None) -> bool:
    """Ask before the window is closed during a run. True means: stop the run and close."""
    asked = FollowingQuestion(parent, source, close_question)
    answer = asked.show(on_open)
    return answer == "stop_and_close" and not asked.ended_by_itself


# --------------------------------------------------------------------------- an interrupted run

def when_words(iso: str, about: bool = False) -> str:
    """ "17 Nov at 03:12" from "2026-11-17T03:12:10" ("17 Nov at about 03:12" with ``about``).

    Written by hand, never through the locale. Empty when the time cannot be read.
    """
    try:
        date, _, clock = str(iso).strip().partition("T")
        _year, month, day = (int(part) for part in date.split("-")[:3])
        hour, minute = (int(part) for part in clock.split(":")[:2])
        if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
            return ""
    except (ValueError, TypeError):
        return ""
    return "%d %s at %s%02d:%02d" % (day, _MONTHS[month - 1], "about " if about else "", hour, minute)


def missing_sentence(count: int, *, done: bool = True) -> str:
    """ "8 samples were left out because their read files are no longer where they were." """
    if count <= 0:
        return ""
    if done:
        return MISSING_READS_ONE if count == 1 else MISSING_READS_MANY.format(n=texts.number(count))
    return MISSING_READS_WILL_ONE if count == 1 else MISSING_READS_WILL_MANY.format(n=texts.number(count))


@dataclass
class InterruptedOffer:
    """What the bar says and what Continue would run."""
    text: str
    level: str                                   # "info", or "warn" when nothing can be continued
    config: Optional[model.RunConfig]            # the samples that can still be run; None: none
    left_out: List[Tuple[str, str]]              # (sample, reason) for those whose reads are gone
    can_continue: bool


def interrupted_offer(info: Any) -> InterruptedOffer:
    """The words for an interrupted run, from what ``batch.interrupted_run`` found.

    ``info`` has ``config`` (the samples still to do), ``finished``, ``total``, ``when`` (ISO
    time), ``cause`` (``"stopped"``, ``"windows"`` or ``"unknown"``) and ``missing`` (names
    whose read files are gone). Samples named in ``missing`` are never run.
    """
    cause = str(getattr(info, "cause", "unknown") or "unknown")
    when = when_words(getattr(info, "when", "") or "", about=cause not in ("stopped", "windows"))
    on_when = " on %s" % when if when else ""
    if cause == "windows":
        first = INTERRUPTED_BY_WINDOWS.format(when=on_when)
    elif cause == "stopped":
        first = INTERRUPTED_STOPPED.format(when=on_when)
    else:
        first = INTERRUPTED_UNEXPECTEDLY.format(when=on_when)
    finished = max(0, int(getattr(info, "finished", 0) or 0))
    total = max(finished, int(getattr(info, "total", 0) or 0))
    parts = [first]
    if total > 0:
        parts.append(INTERRUPTED_PROGRESS.format(
            finished=texts.number(finished), total=texts.count(total, "sample"),
            are="is" if finished == 1 else "are"))
    parts.append(texts.NOTHING_LOST)

    missing = [str(name) for name in (getattr(info, "missing", None) or ())]
    gone = set(missing)
    config = getattr(info, "config", None)
    runnable: Optional[model.RunConfig] = None
    if config is not None:
        samples = [sample for sample in config.samples if sample.name not in gone]
        if samples:
            runnable = replace(config, samples=samples)
    left_out = [(name, MISSING_READS_NOTE) for name in missing]
    if runnable is None:
        if missing:
            parts.append(CANNOT_CONTINUE)
        return InterruptedOffer(" ".join(parts), "warn", None, left_out, False)
    if missing:
        parts.append(missing_sentence(len(missing), done=False))
    return InterruptedOffer(" ".join(parts), "info", runnable, left_out, True)


def show_interrupted_bar(bars: w.NoticeBar, offer: InterruptedOffer,
                         on_continue: Callable[[], Any],
                         on_not_now: Optional[Callable[[], Any]] = None) -> None:
    """Put the offer on a notice bar: [Continue the run] [Not now].

    When nothing can be continued the bar only tells, and can be dismissed.
    """
    def not_now() -> None:
        bars.dismiss(INTERRUPTED_KIND)
        if on_not_now is not None:
            on_not_now()

    if not offer.can_continue:
        bars.show(INTERRUPTED_KIND, offer.text, offer.level, dismissible=True,
                  on_dismiss=lambda _kind: on_not_now() if on_not_now is not None else None)
        return
    actions: Sequence[Tuple[str, Callable[[], Any]]] = (
        (texts.BUTTON["continue_run"], on_continue), (texts.BUTTON["not_now"], not_now))
    bars.show(INTERRUPTED_KIND, offer.text, offer.level, actions=actions, dismissible=False)
