# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The batch: one thread that takes the samples one at a time and remembers where it stands.

:class:`BatchRunner` is the only thing the window starts. It posts the event classes of
``model.py`` on a queue (``BatchStarted`` first, ``BatchFinished`` last; every text in them
is ready to show) and never touches Tk. Around :func:`sample_run.run_sample` it keeps:

* decision D9: the surroundings are checked before every sample and again when a sample
  fails. The PC's failure (results folder gone or full, an engine missing or blocked, the
  read files of two samples in a row gone, three samples in a row failing the same way
  within a minute of starting) ends the batch with ``EndReason.ENVIRONMENT`` and the reason
  in words; a failure that belongs to one sample's data never does;
* decision D10: ``last_run.json`` rewritten at every change of state, and the folder lock
  held for the whole run;
* decisions D2 and D3: finished results are skipped or redone according to
  ``config.existing``; a finished result of the same name made from other reads leaves the
  sample out with the reason;
* ``run_log.txt``, the two summaries after every sample and at the end, a rough time left,
  and the notices (battery, memory, "the PC was asleep").

Keeping the PC awake is bound to a thread on Windows, so the window does that itself when
it receives ``BatchStarted`` and ``BatchFinished``.

:func:`preflight_batch`, :func:`interrupted_run` and :func:`failed_config` are what the
window asks before Start, at launch and for "Try the failed samples again".
"""

from __future__ import annotations

import dataclasses
import getpass
import logging
import os
import platform
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import (appdirs, branding, engine_proc, fsutil, messages, osapi, results, sample_run,
               settings_map, summary_report)
from .messages import Explained
from .model import (BatchFinished, BatchProgress, BatchStarted, EndReason, EnginePaths, Event,
                    Existing, Notice, RunConfig, RunStatus, Sample, SampleFinished, SampleOutcome,
                    SampleStatusChanged, StopMode)

__all__ = ["BatchRunner", "InterruptedRun", "preflight_batch", "interrupted_run", "failed_config",
           "space_needed", "DISK_BASE_BYTES", "MIN_FREE_BYTES", "QUICK_FAILURE_SECONDS",
           "SAME_FAILURES_IN_A_ROW", "LOW_MEMORY_BYTES", "SMALL_PC_BYTES", "LOW_BATTERY_PERCENT"]

log = logging.getLogger(__name__)

#: Free space a sample needs while it is worked on: three times its read files plus this
#: (the rule of the read check, so the batch stops before a sample is doomed to fail it).
DISK_BASE_BYTES = 2 * 1024 ** 3
#: Less free space than this in the results folder counts as "the drive is full".
MIN_FREE_BYTES = 100 * 1024 ** 2
#: Decision D9: this many samples in a row failing the same way, each within this many
#: seconds of starting, are the PC's failure.
SAME_FAILURES_IN_A_ROW = 3
QUICK_FAILURE_SECONDS = 60.0
#: Audit row 6.5: less free memory than this, or a PC with no more than SMALL_PC_BYTES, gets a notice.
LOW_MEMORY_BYTES = 4 * 1024 ** 3
SMALL_PC_BYTES = 8 * 1024 ** 3
LOW_BATTERY_PERCENT = 20

_CLOCK_POLL = 5.0            # seconds between two looks at the clock and the battery
_SLEEP_GAP = 60.0            # a longer silence of the clock watcher means the PC slept
_BATTERY_EVERY = 6           # looks at the clock between two looks at the battery

# Replaced in the tests to imitate a PC that slept.
_monotonic = time.monotonic
_wall = time.time

_FINISHED = (RunStatus.DONE, RunStatus.DONE_NOTES)


# ============================================================================ what the window asks

@dataclass
class InterruptedRun:
    """A run in a results folder that did not reach its end, ready to be continued."""
    config: RunConfig                #: only the samples still to do
    finished: int                    #: samples of that run that are finished (done or skipped)
    total: int                       #: samples of that run
    when: str                        #: ISO time of its last sign of life
    cause: str                       #: "stopped" | "windows" | "unknown"
    missing: List[str] = field(default_factory=list)   #: names whose read files are gone


def space_needed(sample: Sample) -> int:
    """Free bytes the results folder needs while this sample is worked on."""
    return 3 * _sample_bytes(sample) + DISK_BASE_BYTES


def _sample_bytes(sample: Sample) -> int:
    total = 0
    for pair in sample.lanes:
        for path in pair:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
    return total or max(0, int(sample.size_bytes or 0))


def _missing_read(sample: Sample) -> str:
    """The first read file of the sample that is not there ("" when all are)."""
    for pair in sample.lanes:
        for path in pair:
            if not os.path.isfile(path):
                return path
    return "" if sample.lanes else sample.name


_START_CHECKED: Dict[Tuple[Any, ...], float] = {}
_START_CHECK_LASTS = 60.0    # seconds for which "it started" is taken as still true


def _engine_start_problem(engines: EnginePaths, need_trimmer: bool = True) -> Optional[Tuple[str, str]]:
    """``(code, file)`` when an engine is missing or will not start; None when both answer.

    Each engine is really started with ``--version``: only a start shows that antivirus or
    policy blocks it. A good answer is taken as still true for a minute as long as the file
    has not changed, so that the check before Start and the one of the run cost one start.
    """
    for exe in (engines.tesseract_asm,) + ((engines.scepter,) if need_trimmer else ()):
        if not exe or not os.path.isfile(exe):
            return "engine_missing", exe
        try:
            status = os.stat(exe)
        except OSError:
            return "engine_missing", exe
        key = (exe, status.st_mtime_ns, status.st_size, status.st_mode)
        checked = _START_CHECKED.get(key)
        if checked is not None and 0 <= _monotonic() - checked < _START_CHECK_LASTS:
            continue
        try:
            engine_proc.engine_version(exe)
        except engine_proc.EngineStartError as error:
            log.warning("an engine does not start: %s", error)
            _START_CHECKED.pop(key, None)
            return ("engine_blocked" if error.kind is osapi.ExitKind.BLOCKED else "engine_broken"), exe
        _START_CHECKED[key] = _monotonic()
    return None


def preflight_batch(config: RunConfig, engines: Optional[EnginePaths] = None) -> List[str]:
    """What prevents this run from starting, as plain sentences. An empty list: nothing does.

    Checked: there are samples; both engines are there and start; the results folder can be
    written to, was not made by a newer version and is not in use by another run; there is
    enough free space for the largest sample. Nothing is changed except that the results
    folder is created if it does not exist.
    """
    problems: List[str] = []
    samples = [s for s in config.samples if s.ready]
    if not samples:
        problems.append(messages.start_problem("no_samples"))
    if engines is None:
        try:
            engines = appdirs.find_engines()
        except appdirs.EngineMissing as error:
            problems.append(messages.start_problem(
                "engine_missing", os.path.join(error.folder, error.missing[0])))
    if engines is not None:
        found = _engine_start_problem(engines, any(s.trim for s in samples) or not samples)
        if found is not None:
            problems.append(messages.start_problem(found[0], found[1]))
    folder = config.results_dir
    if not folder or not fsutil.is_writable_dir(folder):
        problems.append(messages.start_problem("results_unwritable", folder))
        return problems
    try:
        newer = results.made_by_newer_version(folder)
    except Exception:
        log.exception("could not tell which version made %s", folder)
        newer = False
    if newer:
        problems.append(messages.start_problem("newer_version", folder))
    holder = _holder(folder)
    if holder:
        problems.append(messages.start_problem("folder_in_use", folder, detail=_holder_words(holder)))
    free = fsutil.free_bytes(folder)
    if free is not None and samples:
        need = max(space_needed(s) for s in samples)
        if free < need:
            problems.append(messages.start_problem(
                "disk_low", folder, detail="about %s needed, %s free"
                % (fsutil.format_bytes(need), fsutil.format_bytes(free))))
    return problems


def _holder(folder: str) -> str:
    try:
        return osapi.FolderLock.holder_text(folder)
    except Exception:
        log.exception("could not ask who holds %s", folder)
        return ""


def _holder_words(holder: str) -> str:
    return "" if holder == osapi.UNKNOWN_HOLDER else " ".join(holder.split())[:120]


def _load(results_dir: str) -> Optional[results.LastRun]:
    try:
        return results.load_last_run(results_dir)
    except results.NewerFormat:
        return None
    except Exception:
        log.exception("could not read the memory of the last run in %s", results_dir)
        return None


def _finished_record(results_dir: str, name: str, started_at: str) -> Optional[results.SampleRecord]:
    """The finished result of the sample that is in place and was made after ``started_at``, if any.

    Times are kept to the second. A result stamped with the very second the batch started is
    taken for an earlier one: a sample takes longer than that, and the mistake the other way
    round would leave a sample that waits for its redo out of "Continue".
    """
    try:
        record = results.read_status(results_dir, name)
    except Exception:
        return None
    if record is not None and record.assembled and started_at and record.finished_at > started_at:
        return record
    return None


def _finished_since(results_dir: str, name: str, started_at: str) -> bool:
    """A finished result of the sample is in place and was made after ``started_at``."""
    return _finished_record(results_dir, name, started_at) is not None


def interrupted_run(results_dir: str) -> Optional[InterruptedRun]:
    """The run in that folder that never recorded its end, or that was stopped; None when there is none.

    More exactly: the remembered batch still has samples that were stopped, not started or in
    the middle of a stage. A run that is going on right now (its folder lock is held) is not
    an interrupted one.
    """
    last = _load(results_dir)
    if last is None or _holder(results_dir):
        return None
    # A run that reached its end is looked at too: "Try the failed samples again" ends as
    # completed while the samples an earlier Stop left behind are still waiting in the memory.
    pending: List[Sample] = []
    missing: List[str] = []
    finished = 0
    for sample in last.config.samples:
        status = last.status_of(sample.name)
        waiting = status.is_running or status in (RunStatus.WAITING, RunStatus.STOPPED, RunStatus.NOT_STARTED)
        if waiting and _finished_since(results_dir, sample.name, last.started_at):
            finished += 1                                # finished a moment before the end was recorded
        elif not waiting:
            finished += 1 if status in _FINISHED + (RunStatus.SKIPPED,) else 0
        elif _missing_read(sample):
            missing.append(sample.name)
        else:
            pending.append(sample)
    if not pending and not missing:
        return None
    config = RunConfig(results_dir=results_dir, samples=pending, trim=last.config.trim,
                       assembly=last.config.assembly, existing=last.config.existing)
    cause = "windows" if last.interrupted else ("stopped" if last.ended_at else "unknown")
    return InterruptedRun(config=config, finished=finished, total=len(last.config.samples),
                          when=last.ended_at or last.saved_at or last.started_at, cause=cause,
                          missing=missing)


def failed_config(results_dir: str) -> Optional[RunConfig]:
    """The samples that failed in the last run there, ready to run again; None when none did.

    ``existing`` is Overwrite: asking for a failed sample again means running it, also when
    an earlier good result of it is still in place (which is replaced only by a new good one).
    """
    last = _load(results_dir)
    if last is None:
        return None
    failed = results.failed_samples(last)
    if not failed:
        return None
    return RunConfig(results_dir=results_dir, samples=failed, trim=last.config.trim,
                     assembly=last.config.assembly, existing=Existing.OVERWRITE)


# ============================================================================ the runner

class BatchRunner(threading.Thread):
    """Runs the samples of ``config`` one at a time and reports through ``events``.

    ``engines``: None means :func:`appdirs.find_engines`. ``left_out``: ``(sample name,
    reason)`` rows that will not run, for the summaries and the counts. ``resume``: True for
    Continue and for "Try the failed samples again": the memory of the earlier batch in
    ``last_run.json`` is kept and added to, and a sample that batch finished is not redone.

    ``BatchProgress.position`` and ``.total`` count the rows of ``config.samples`` (skipped
    and left-out rows included), so they match ``BatchStarted``.
    """

    def __init__(self, config: RunConfig, events: "queue.Queue[Event]", *,
                 engines: Optional[EnginePaths] = None,
                 left_out: Sequence[Tuple[str, str]] = (), resume: bool = False) -> None:
        super().__init__(name="tesseract-win-batch", daemon=True)
        self.config = config
        self.events = events
        self.engines = engines
        self.resume = bool(resume)
        self._left_out: List[Tuple[str, str]] = [(str(n), str(r)) for n, r in left_out]
        self._stop_now = threading.Event()
        self._after_current = False
        self._ended = threading.Event()
        self._lock = threading.RLock()
        self._group: Optional[osapi.ProcessGroup] = None
        self._folder_lock: Optional[osapi.FolderLock] = None
        self._current: Tuple[str, float] = ("", 0.0)
        self._states: Dict[str, Tuple[RunStatus, str]] = {}
        self._outcomes: Dict[str, SampleOutcome] = {}
        self._memory_config: RunConfig = config
        self._memory_states: Dict[str, Tuple[RunStatus, str]] = {}
        self._memory_written = False
        self._started_at = ""
        self._ended_at: Optional[str] = None
        self._end_reason: Optional[EndReason] = None
        self._interrupted: Optional[str] = None
        self._happenings: List[str] = []
        self._timed: List[Tuple[int, float]] = []
        self._began = 0.0
        self._folder_usable = False
        self._folder_identity: Optional[Tuple[int, int]] = None
        self._watcher: Optional[threading.Thread] = None
        self._summary_trouble: List[str] = []
        self._message = ""

    # ------------------------------------------------------------------ what the window may call

    def request_stop(self, mode: StopMode) -> None:
        """NOW: end the engines at once, the running sample is lost. AFTER_CURRENT: let it finish."""
        if mode is StopMode.NOW:
            self._stop_now.set()
            group = self._group
            if group is not None:
                try:
                    group.kill()
                except Exception:
                    log.exception("ending the engines failed")
        else:
            self._after_current = True

    def cancel_stop(self) -> None:
        """Undo a pending AFTER_CURRENT ("Keep going after all"). A stop NOW cannot be undone."""
        self._after_current = False

    def stop_pending(self) -> Optional[StopMode]:
        if self._stop_now.is_set():
            return StopMode.NOW
        return StopMode.AFTER_CURRENT if self._after_current else None

    def current(self) -> Tuple[str, float]:
        """``(name of the sample now running or "", seconds it has been running)``."""
        name, began = self._current
        return (name, max(0.0, _monotonic() - began)) if name else ("", 0.0)

    def note_interruption(self, cause: str = "shutdown") -> None:
        """Record in ``last_run.json`` that Windows is ending the session. Any thread may call it."""
        self._interrupted = str(cause or "shutdown")
        self._write_log("Windows gave notice that the session is ending (%s)." % self._interrupted)
        self._save()

    # ------------------------------------------------------------------ small tools

    def _emit(self, event: Event) -> None:
        self.events.put(event)

    def _from_sample(self, event: Event) -> None:
        """An event of the running sample: its stages go into ``last_run.json`` too (decision D10)."""
        if isinstance(event, SampleStatusChanged) and event.status.is_running:
            with self._lock:
                self._states[event.name] = (event.status, "")
                self._memory_states[event.name] = (event.status, "")
            self._save()
        self._emit(event)

    def _write_log(self, text: str) -> None:
        """One line into ``run_log.txt``. Never raises, and never brings back a folder that is gone."""
        if not self._folder_usable:
            return
        folder = self.config.results_dir
        try:
            if not os.path.isdir(folder):
                return
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                with open(results.run_log_path(folder), "a", encoding="utf-8", newline="\n") as handle:
                    for line in str(text).splitlines() or [""]:
                        handle.write("%s  %s\n" % (stamp, line))
        except OSError as error:
            log.debug("the run log could not be written: %s", error)

    def _folder_alive(self) -> bool:
        """Is the results folder still the one this run began in?

        Not when it cannot be reached, when ``last_run.json`` has vanished from it, or (where
        the system gives folders a lasting number) when another folder now has its name.
        """
        folder = self.config.results_dir
        if not os.path.isdir(folder):
            return False
        if self._folder_identity is not None and _identity(folder) != self._folder_identity:
            return False
        return not self._memory_written or os.path.isfile(results.last_run_path(folder))

    def _set_state(self, name: str, status: RunStatus, note: str = "", *, announce: bool = True) -> None:
        with self._lock:
            self._states[name] = (status, note)
            self._memory_states[name] = (status, note)
        if announce:
            self._emit(SampleStatusChanged(name=name, status=status, note=note))

    def _save(self) -> None:
        """Rewrite ``last_run.json`` (decision D10). A folder that has gone is not brought back."""
        if not self._folder_usable or not self._folder_alive():
            return
        try:
            with self._lock:
                results.save_last_run(
                    self.config.results_dir, self._memory_config, dict(self._memory_states),
                    started_at=self._started_at, ended_at=self._ended_at,
                    end_reason=self._end_reason, interrupted=self._interrupted)
                self._memory_written = True
        except Exception:
            log.exception("last_run.json could not be written")

    def _counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        with self._lock:
            for status, _note in self._states.values():
                counts[status.value] = counts.get(status.value, 0) + 1
        if self._left_out:
            counts[RunStatus.LEFT_OUT.value] = counts.get(RunStatus.LEFT_OUT.value, 0) + len(self._left_out)
        return counts

    def _left_out_rows(self) -> List[Tuple[str, str]]:
        with self._lock:
            own = [(name, note) for name, (status, note) in self._states.items()
                   if status is RunStatus.LEFT_OUT]
        return self._left_out + own

    def _settings_line(self) -> str:
        return "Settings: %s." % settings_map.describe(
            self.config.trim, self.config.assembly, any(s.trim for s in self.config.samples))

    def _write_summary(self, current: str = "") -> None:
        if not self._folder_usable or not self._folder_alive():
            return
        with self._lock:
            outcomes = list(self._outcomes.values())
            happenings = list(self._happenings)
        info = {
            "started": self._started_at, "finished": self._ended_at,
            "seconds": max(0.0, _monotonic() - self._began),
            "end_reason": self._end_reason, "message": self._message,
            "settings": self._settings_line(), "events": happenings,
            "samples": [s.name for s in self.config.samples], "current": current,
        }
        try:
            self._summary_trouble = list(summary_report.write_run_summary(
                self.config.results_dir, this_run=outcomes, left_out=self._left_out_rows(),
                run_info=info))
        except Exception:
            log.exception("the summaries could not be written")

    def _happened(self, text: str) -> None:
        with self._lock:
            self._happenings.append(text)
        self._write_log(text)

    def _finished_count(self) -> int:
        with self._lock:
            return sum(1 for status, _n in self._states.values() if status in _FINISHED)

    # ------------------------------------------------------------------ the surroundings (D9)

    def _surroundings(self, sample: Optional[Sample] = None) -> Optional[Explained]:
        """What is wrong with the PC or the results folder; None when nothing is."""
        folder = self.config.results_dir
        engines = self.engines
        if not self._folder_alive():
            return messages.explain_environment("results_gone", folder)
        free = fsutil.free_bytes(folder)
        if free is not None and free < MIN_FREE_BYTES:
            return messages.explain_environment("disk_full", folder)
        if not fsutil.is_writable_dir(folder):
            # Gone in the very moment of the test is "gone", not "read-only".
            return messages.explain_environment(
                "results_unwritable" if self._folder_alive() else "results_gone", folder)
        if engines is not None:
            wanted = [engines.tesseract_asm]
            if any(s.trim for s in self.config.samples):
                wanted.append(engines.scepter)
            for exe in wanted:
                if not os.path.isfile(exe):
                    return messages.explain_environment("engine_missing", exe)
        if sample is not None and free is not None and free < space_needed(sample):
            return messages.explain_environment(
                "disk_low", folder, detail="about %s needed, %s free"
                % (fsutil.format_bytes(space_needed(sample)), fsutil.format_bytes(free)))
        return None

    # ------------------------------------------------------------------ notices

    def _notice(self, kind: str, text: str, level: str = "warn") -> None:
        self._emit(Notice(kind=kind, text=text, level=level))
        if text:
            self._write_log(text)

    def _battery_notice(self, before: str) -> str:
        """Posts the battery bar when its state changed; returns the state ("", "on", "low")."""
        try:
            on_battery, percent, _has = osapi.power_status()
        except Exception:
            return before
        state = ""
        if on_battery:
            state = "low" if percent is not None and percent <= LOW_BATTERY_PERCENT else "on"
        if state != before:
            if state == "low":
                self._notice("battery", messages.battery_low(int(percent or 0)), "bad")
            elif state == "on":
                self._notice("battery", messages.ON_BATTERY, "warn")
            else:
                self._notice("battery", "", "info")      # the charger is back: the bar goes
        return state

    def _memory_notice(self) -> None:
        try:
            free, total = osapi.available_memory_bytes(), osapi.total_memory_bytes()
        except Exception:
            return
        if free is None:
            return
        if free < LOW_MEMORY_BYTES or (total is not None and total <= SMALL_PC_BYTES):
            self._notice("memory", messages.low_memory(free), "warn")

    def _watch(self, battery: str) -> None:
        """Runs beside the batch: notices a PC that slept, and the charger coming and going."""
        last = _wall()
        rounds = 0
        while not self._ended.wait(_CLOCK_POLL):
            now = _wall()
            if now - last > _SLEEP_GAP + _CLOCK_POLL:
                text = messages.slept(last, now)
                self._emit(Notice(kind="slept", text=text, level="info"))
                self._happened(text)
            last = now
            rounds += 1
            if rounds % _BATTERY_EVERY == 0:
                battery = self._battery_notice(battery)

    # ------------------------------------------------------------------ the thread

    def run(self) -> None:
        self._began = _monotonic()
        self._started_at = fsutil.now_iso()
        names = [s.name for s in self.config.samples]
        with self._lock:
            for name in names:
                self._states[name] = (RunStatus.WAITING, "")
        self._emit(BatchStarted(total=len(names), names=list(names)))
        reason, message = EndReason.ENVIRONMENT, ""
        try:
            self._group = osapi.ProcessGroup()
            reason, message = self._run_batch()
        except BaseException:                            # whatever it is: BatchFinished must follow
            log.exception("the batch ended with an unexpected error")
            self._write_log("Unexpected error inside the program; the details are in the program's log.")
            reason = EndReason.ENVIRONMENT
            message = messages.environment_stop("internal", finished=self._finished_count())
        try:
            self._finish(reason, message)
        except BaseException:
            log.exception("the end of the batch could not be recorded")
        finally:
            self._ended.set()
            if self._watcher is not None:
                self._watcher.join(10)                   # so that no notice can follow BatchFinished
            for closing in (self._close_group, self._release_lock):
                try:
                    closing()
                except Exception:
                    log.exception("clean-up after the batch failed")
            self._current = ("", 0.0)
            counts = self._counts()
            seconds = max(0.0, _monotonic() - self._began)
            words = self._message or message or messages.batch_message(reason, counts, seconds=seconds)
            self._emit(BatchFinished(reason=reason, message=words, counts=counts, seconds=seconds))

    def _close_group(self) -> None:
        if self._group is not None:
            self._group.close()

    def _release_lock(self) -> None:
        if self._folder_lock is not None:
            self._folder_lock.release()

    def _environment_end(self, code: str, path: str = "", detail: str = "") -> Tuple[EndReason, str]:
        if not path and code in ("results_gone", "results_unwritable", "disk_full", "disk_low"):
            path = self.config.results_dir
        return EndReason.ENVIRONMENT, messages.environment_stop(
            code, path, finished=self._finished_count(), detail=detail)

    def _prepare(self) -> Optional[Tuple[EndReason, str]]:
        """Everything before the first sample. Returns the ending when the run cannot begin."""
        config, folder = self.config, self.config.results_dir
        need_trimmer = any(s.trim for s in config.samples)
        if self.engines is None:
            try:
                self.engines = appdirs.find_engines()
            except appdirs.EngineMissing as error:
                return self._environment_end("engine_missing", os.path.join(error.folder, error.missing[0]))
        found = _engine_start_problem(self.engines, need_trimmer)
        if found is not None:
            return self._environment_end(found[0], found[1])
        if not folder or not fsutil.is_writable_dir(folder):
            return self._environment_end("results_unwritable", folder)
        lock = osapi.FolderLock()
        who = "%s on %s, since %s" % (_user_name(), platform.node() or "this PC", self._started_at)
        if not lock.acquire(folder, who):
            return self._environment_end("folder_in_use", folder, _holder_words(_holder(folder)))
        self._folder_lock = lock
        try:
            if results.made_by_newer_version(folder):
                return self._environment_end("newer_version", folder)
            recovered = results.recover_interrupted(folder)
        except results.NewerFormat:
            return self._environment_end("newer_version", folder)
        self._folder_usable = True
        self._folder_identity = _identity(folder)
        self._write_log("==== %s ====" % branding.versions_line())
        self._write_log("Run started: %s. Results folder: %s" % (
            messages.count_words(len(config.samples), "sample"), folder))
        self._write_log(self._settings_line())
        self._write_log("Engines: %s | %s | detection sketch: %s" % (
            self.engines.tesseract_asm, self.engines.scepter, self.engines.detect_sketch or "none"))
        self._write_log("System: %s; Windows build %s; code page %s; %s logical processors" % (
            platform.platform(), osapi.windows_build() or "-", osapi.ansi_code_page() or "-",
            os.cpu_count() or "?"))
        if lock.degraded:
            self._write_log("The results folder could not be locked; the run goes on without the lock.")
        for name in recovered:
            self._write_log("%s: an unfinished swap of an earlier run was undone; the earlier state is back." % name)
        return None

    def _load_memory(self) -> None:
        """For Continue: keep what ``last_run.json`` knows about the samples that do not run now."""
        self._memory_config = self.config
        self._memory_states = {}
        if not self.resume:
            return
        last = _load(self.config.results_dir)
        if last is None:
            return
        mine = {s.name: s for s in self.config.samples}
        merged = [mine.pop(s.name, s) for s in last.config.samples]
        merged += list(mine.values())                    # samples the remembered batch did not have
        self._memory_config = RunConfig(results_dir=self.config.results_dir, samples=merged,
                                        trim=self.config.trim, assembly=self.config.assembly,
                                        existing=self.config.existing)
        running_now = {s.name for s in self.config.samples}
        for sample in last.config.samples:
            status = last.status_of(sample.name)
            note = last.notes.get(sample.name, "")
            if status.is_running or status in (RunStatus.WAITING, RunStatus.STOPPED, RunStatus.NOT_STARTED):
                record = None
                if sample.name not in running_now:
                    record = _finished_record(self.config.results_dir, sample.name, last.started_at)
                if record is not None:
                    # It was finished a moment before that batch died, and nobody wrote it down:
                    # without this it would be offered for "Continue" for ever.
                    status, note = record.status, record.outcome.note or ""
                elif status.is_running or status is RunStatus.WAITING:
                    status = RunStatus.NOT_STARTED       # it never got its end
            self._memory_states[sample.name] = (status, note)

    def _plan(self) -> List[Sample]:
        """Decisions D2 and D3: which samples run. The others get their final state here."""
        config, folder = self.config, self.config.results_dir

        def fingerprint(sample: Sample) -> str:
            return settings_map.settings_fingerprint(config.trim, config.assembly, bool(sample.trim))

        clashes = set(results.name_clashes(folder, config.samples))
        existing = results.existing_results(folder, config.samples, fingerprint)
        remembered = dict(self._memory_states)
        runnable: List[Sample] = []
        seen: Dict[str, str] = {}
        for sample in config.samples:
            name = sample.name
            reason = ""
            if name in seen.values():
                continue                                 # the same row twice: it runs once
            twin = seen.get(name.casefold())
            seen.setdefault(name.casefold(), name)
            if not sample.ready:
                reason = sample.note or "This sample was left out on the Samples page."
            elif _not_a_folder_name(name):
                reason = messages.left_out_bad_name(name)
            elif twin is not None:
                # "kp1" and "KP1" are one folder on Windows: the second would replace the first.
                reason = messages.left_out_name_twice(name, twin)
            elif results.is_reserved_name(name):
                reason = messages.left_out_reserved_name(name)
            elif name in clashes:
                reason = messages.left_out_same_name(name)
            else:
                foreign = results.foreign_content(folder, name)
                if foreign:
                    reason = messages.left_out_foreign_folder(name, foreign)
            if reason:
                self._set_state(name, RunStatus.LEFT_OUT, reason)
                self._write_log("%s: left out. %s" % (name, reason))
                continue
            found = existing.get(name)
            finished_before = self.resume and remembered.get(name, (None, ""))[0] in _FINISHED
            if found is not None and (config.existing is Existing.SKIP or finished_before):
                note = messages.skipped_note(found.finished_at, found.same_settings)
                record = results.read_status(folder, name)
                base = record.outcome if record is not None else SampleOutcome(name=name, status=RunStatus.SKIPPED)
                outcome = dataclasses.replace(base, status=RunStatus.SKIPPED, note=note, next_step="",
                                              kept_earlier_result=False)
                with self._lock:
                    self._outcomes[name] = outcome
                self._set_state(name, RunStatus.SKIPPED, note)
                self._emit(SampleFinished(outcome=outcome))
                self._write_log("%s: skipped. %s" % (name, note))
                continue
            self._set_state(name, RunStatus.WAITING, announce=False)
            runnable.append(sample)
        return runnable

    def _eta(self, remaining: Sequence[Sample]) -> Optional[float]:
        """A rough time left: the time per byte of read files so far, times what is still to do."""
        if not self._timed or not remaining:
            return None if not self._timed else 0.0
        done_bytes = sum(size for size, _s in self._timed)
        done_seconds = sum(seconds for _b, seconds in self._timed)
        left_bytes = sum(_sample_bytes(s) for s in remaining)
        if done_bytes > 0 and left_bytes > 0:
            return done_seconds / done_bytes * left_bytes
        return done_seconds / len(self._timed) * len(remaining)

    def _progress(self, position: int, remaining: Sequence[Sample]) -> None:
        counts = self._counts()
        self._emit(BatchProgress(
            position=position, total=len(self.config.samples),
            done=counts.get(RunStatus.DONE.value, 0) + counts.get(RunStatus.DONE_NOTES.value, 0),
            failed=counts.get(RunStatus.FAILED.value, 0), eta_seconds=self._eta(remaining)))

    def _record(self, sample: Sample, outcome: SampleOutcome, size: int) -> None:
        """A sample has ended: remember it, tell the window, rewrite the memory and the summaries."""
        with self._lock:
            self._outcomes[sample.name] = outcome
        if outcome.status in _FINISHED:
            self._timed.append((size, max(0.0, float(outcome.seconds or 0.0))))
        if outcome.thin_retry and outcome.status in _FINISHED:
            with self._lock:
                self._happenings.append("%s had very little data and was assembled with the settings "
                                        "for thin data." % sample.name)
        self._set_state(sample.name, outcome.status, outcome.note)
        self._emit(SampleFinished(outcome=outcome))
        words = {RunStatus.DONE: "done", RunStatus.DONE_NOTES: "done (check notes)",
                 RunStatus.FAILED: "FAILED", RunStatus.STOPPED: "stopped"}.get(outcome.status, outcome.status.value)
        line = "%s: %s in %s." % (sample.name, words, fsutil.format_duration(outcome.seconds))
        if outcome.status in _FINISHED and outcome.contigs is not None:
            line += " %s, %s bases, N50 %s." % (
                messages.count_words(outcome.contigs, "contig"), "{:,}".format(outcome.total_length or 0),
                "{:,}".format(outcome.n50 or 0))
        for text in ([outcome.note] if outcome.note else []) + \
                ([outcome.next_step] if outcome.next_step else []) + list(outcome.notes[1:]):
            line += "\n    %s" % text
        self._write_log(line)
        self._save()
        self._write_summary()

    def _fail_without_attempt(self, sample: Sample, explained: Explained) -> SampleOutcome:
        """A sample that cannot even begin (its read files are gone): Failed, recorded in its folder."""
        folder = self.config.results_dir
        kept = False
        try:
            kept = results.is_assembled(folder, sample.name)
        except Exception:
            pass
        note = messages.with_earlier_kept(explained.note) if kept else explained.note
        outcome = SampleOutcome(name=sample.name, status=RunStatus.FAILED, note=note,
                                next_step=explained.next_step, trimmed=bool(sample.trim),
                                finished_at=fsutil.now_iso(), kept_earlier_result=kept,
                                folder=results.sample_dir(folder, sample.name))
        try:
            results.write_status(folder, outcome, reads=results.reads_of(sample),
                                 settings_fingerprint=settings_map.settings_fingerprint(
                                     self.config.trim, self.config.assembly, bool(sample.trim)),
                                 settings_text=sample_run.settings_text(self.config, bool(sample.trim)),
                                 trimmed=bool(sample.trim))
        except Exception:
            log.exception("%s: the failure could not be recorded", sample.name)
        return outcome

    def _run_batch(self) -> Tuple[EndReason, str]:
        ending = self._prepare()
        if ending is not None:
            return ending
        config = self.config
        try:
            self._load_memory()
            runnable = self._plan()
        except results.NewerFormat:
            return self._environment_end("newer_version", config.results_dir)
        self._save()
        self._write_summary()
        self._memory_notice()
        battery = self._battery_notice("")
        self._watcher = threading.Thread(target=self._watch, args=(battery,), name="tesseract-win-clock",
                                         daemon=True)
        self._watcher.start()

        version = sample_run.engine_version_line(self.engines.tesseract_asm)
        order = {s.name: i + 1 for i, s in enumerate(config.samples)}
        streak: List[str] = []
        for index, sample in enumerate(runnable):
            if self._stop_now.is_set() or self._after_current:
                return EndReason.STOPPED, ""
            problem = self._surroundings(sample)
            if problem is not None:
                self._write_log("The run cannot go on: %s" % problem.note)
                return self._environment_end(problem.environment, problem.path,
                                             _detail_of(problem))
            remaining = runnable[index:]
            self._current = (sample.name, _monotonic())
            self._progress(order[sample.name], remaining)
            self._write_log("%s: started (%s of read files%s)." % (
                sample.name, fsutil.format_bytes(_sample_bytes(sample)),
                ", to be trimmed" if sample.trim else ", not trimmed"))
            size = _sample_bytes(sample)
            trace = sample_run.SampleTrace()
            gone = _missing_read(sample)
            if gone:
                explained = messages.reads_gone(gone)
                trace.code, trace.explained = explained.code, explained
                outcome = self._fail_without_attempt(sample, explained)
            else:
                self._write_summary(current=sample.name)
                outcome = sample_run.run_sample(
                    sample, config, self.engines, group=self._group, stop_event=self._stop_now,
                    post=self._from_sample, check_surroundings=self._surroundings, trace=trace,
                    engine_version=version, run_log=self._write_log)
            self._current = ("", 0.0)
            self._record(sample, outcome, size)
            self._progress(order[sample.name], runnable[index + 1:])

            if trace.environment:
                self._write_log("The run cannot go on: %s" % (trace.explained.note if trace.explained else trace.environment))
                return self._environment_end(trace.environment, trace.environment_path,
                                             _detail_of(trace.explained))
            if outcome.status is RunStatus.STOPPED:
                return EndReason.STOPPED, ""
            if trace.code == "reads_gone" and index + 1 < len(runnable):
                also_gone = _missing_read(runnable[index + 1])
                if also_gone:                            # audit row 9.10: the drive of the reads is gone
                    return self._environment_end("reads_gone", also_gone)
            quick = float(outcome.seconds or 0.0) <= QUICK_FAILURE_SECONDS
            if outcome.status is RunStatus.FAILED and quick and trace.code and not trace.about_data \
                    and trace.code != "reads_gone":
                streak.append(trace.code)
            else:
                streak = []
            if len(streak) >= SAME_FAILURES_IN_A_ROW and len(set(streak[-SAME_FAILURES_IN_A_ROW:])) == 1:
                return self._environment_end("same_failure")
        if self._stop_now.is_set() and any(
                status is RunStatus.WAITING for status, _n in self._states.values()):
            return EndReason.STOPPED, ""
        return EndReason.COMPLETED, ""

    def _finish(self, reason: EndReason, message: str) -> None:
        """Samples still waiting become Not started; the end is recorded; the summaries are final."""
        with self._lock:
            waiting = [name for name, (status, _n) in self._states.items()
                       if status is RunStatus.WAITING or status.is_running]
        for name in waiting:
            self._set_state(name, RunStatus.NOT_STARTED)
        self._ended_at = fsutil.now_iso()
        self._end_reason = reason
        counts = self._counts()
        self._message = message or messages.batch_message(
            reason, counts, seconds=_monotonic() - self._began, finished_at=_wall())
        self._write_log(self._message)
        self._save()
        self._write_summary()
        if self._summary_trouble and self._folder_alive():
            self._notice("info", messages.summary_not_updated(self._summary_trouble), "warn")


def _not_a_folder_name(name: str) -> bool:
    """A sample name that would not be one folder inside the results folder (rule R7)."""
    return (not name or name in (".", "..") or name != name.rstrip(". ")
            or any(mark in name for mark in ("/", "\\", ":", "\x00")))


def _identity(folder: str) -> Optional[Tuple[int, int]]:
    """A number that stays with a folder for as long as it exists; None where there is no such thing.

    Used on POSIX only. On Windows a folder that holds an open file (the lock) cannot be
    renamed or deleted, and file numbers of FAT and network drives are not to be trusted.
    """
    if os.name == "nt":
        return None
    try:
        status = os.stat(folder)
    except OSError:
        return None
    return (status.st_dev, status.st_ino) if status.st_ino else None


def _detail_of(explained: Optional[Explained]) -> str:
    """The figures in brackets at the end of an environment sentence ("about 61 GB needed, 12 GB free")."""
    if explained is None or explained.environment != "disk_low":
        return ""
    note = explained.note
    start, end = note.rfind("("), note.rfind(")")
    return note[start + 1:end] if 0 <= start < end else ""


def _user_name() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "an unknown user"
