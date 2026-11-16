# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""A stand-in for the batch runner, and a bare window that shows only the Run page.

No engine is started and no file is written. :class:`DemoRunner` has the interface of
``batch.BatchRunner`` and plays a scripted batch, posting the events of ``model.py`` on the
queue it is given, so the Run page can be developed, tested and photographed anywhere.

    python3 -m tesseract_win.gui.demo --scenario normal
    python3 -m tesseract_win.gui.demo --scenario big --dark --scale 1.5
    python3 -m tesseract_win.gui.demo --scenario normal --do stop@6

Pictures of the window are taken by ``tools/ci_screenshots.py``, which needs Pillow; nothing in
this package does.

Scenarios:

- ``normal``       8 samples; one fails, one is "Done (check notes)", one has two lanes
- ``stopped``      the same batch, stopped by the user in its fourth sample
- ``vanished``     the batch ends in its sixth sample because the results folder is gone
- ``big``          120 samples
- ``interrupted``  nothing runs: the bar offers to continue a run cut short by Windows
- ``missing``      the same bar when some read files have gone missing since

Time is *virtual*: every length in a script is in seconds of a real run (a sample takes
about ten minutes) and ``--speed`` says how many of those pass per real second. The clocks
of the page follow the same virtual time, so a photograph shows believable numbers.

``--do ACTION@SECONDS`` (repeatable) presses things at a real time after the start:
``stop`` (the Stop button), ``answer:KEY`` (a button of the open question: ``keep``,
``after_current``, ``now``, ``stop_and_close``), ``close`` (the close box), ``select:NAME``,
``log``, ``continue``, ``retry``, ``keep_going``, ``offer`` (press Continue the run).
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import branding, fsutil, model
from ..model import (BatchFinished, BatchProgress, BatchStarted, EndReason, LogLine, Notice,
                     RunConfig, RunStatus, Sample, SampleFinished, SampleOutcome,
                     SampleStatusChanged, Stage, StageProgress, StopMode, Working)
from . import texts

__all__ = [
    "SCENARIOS", "Plan", "Scenario", "scenario", "VirtualClock", "DemoRunner", "factory",
    "DemoApp", "InterruptedDemo", "open_demo", "main",
]

SCENARIOS = ("normal", "stopped", "vanished", "big", "interrupted", "missing")

#: The seven steps of the assembler in words (audit B.5), with the share of the time each takes.
ASSEMBLY_STEPS: Tuple[Tuple[str, float, Tuple[str, ...]], ...] = (
    ("reading the reads", 0.07, ("[1/7] loading reads",)),
    ("correcting read errors", 0.12, ("[2/7] correcting read errors",)),
    ("building the assembly graph", 0.40, ("[3/7] building the assembly graph",)),
    ("resolving repeats with the read pairs", 0.14, ("[4/7] resolving repeats with the read pairs",)),
    ("closing gaps", 0.09, ("[5/7] closing gaps",)),
    ("polishing the sequence", 0.14, ("[6/7] polishing the sequence",)),
    ("writing the results", 0.04, ("[7/7] writing output",)),
)
KMERS = (21, 33, 55, 77, 99)
_COUNTED = (RunStatus.DONE, RunStatus.DONE_NOTES, RunStatus.FAILED, RunStatus.SKIPPED, RunStatus.STOPPED,
            RunStatus.NOT_STARTED, RunStatus.LEFT_OUT)


# --------------------------------------------------------------------------- scripts

@dataclass
class Plan:
    """How one sample goes. Lengths are in virtual seconds."""
    status: RunStatus = RunStatus.DONE
    trim_seconds: float = 85.0
    check_seconds: float = 40.0
    assemble_seconds: float = 470.0
    summarise_seconds: float = 8.0
    fail_at: Optional[Stage] = None              # the stage in which a FAILED sample ends
    note: str = ""
    notes: Tuple[str, ...] = ()
    next_step: str = ""
    contigs: Optional[int] = None
    total_length: Optional[int] = None
    largest: Optional[int] = None
    n50: Optional[int] = None
    depth: Optional[int] = None
    thin_retry: bool = False

    @property
    def seconds(self) -> float:
        return self.trim_seconds + self.check_seconds + self.assemble_seconds + self.summarise_seconds


@dataclass
class Scenario:
    name: str
    config: RunConfig
    plans: Dict[str, Plan]
    left_out: List[Tuple[str, str]] = field(default_factory=list)
    #: ("stop", index, share of the sample done) or ("environment", index, share, message)
    ending: Optional[Tuple[Any, ...]] = None
    notices: Dict[int, Notice] = field(default_factory=dict)     # posted when sample <index> starts
    speed: float = 60.0
    start_delay: float = 20.0                 # virtual seconds before the first sample begins
    interrupted: Optional["InterruptedDemo"] = None


@dataclass
class InterruptedDemo:
    """What ``batch.interrupted_run`` would return (same field names)."""
    config: RunConfig
    finished: int
    total: int
    when: str
    cause: str
    missing: List[str] = field(default_factory=list)


def _reads(folder: str, name: str, number: int, lanes: int = 1) -> List[Tuple[str, str]]:
    return [(os.path.join(folder, "%s_S%d_L%03d_R1_001.fastq.gz" % (name, number, lane)),
             os.path.join(folder, "%s_S%d_L%03d_R2_001.fastq.gz" % (name, number, lane)))
            for lane in range(1, lanes + 1)]


def _folders() -> Tuple[str, str]:
    """Where the pretend reads and results are: Windows paths on Windows, plain ones elsewhere."""
    if os.name == "nt":
        return "D:\\Sequencing\\run_2026_11", "D:\\Results\\Klebsiella_Nov"
    return "/data/sequencing/run_2026_11", "/data/results/Klebsiella_Nov"


def _good_plan(index: int) -> Plan:
    """A believable finished assembly; the numbers differ from sample to sample."""
    wobble = (index * 7919) % 97
    contigs = 58 + wobble % 41
    total = 5_310_000 + ((index * 104_729) % 260_000)
    n50 = 168_000 + ((index * 15_485_863) % 96_000)
    return Plan(contigs=contigs, total_length=total, largest=n50 * 2 + 31_000, n50=n50, depth=68 + wobble % 40,
                trim_seconds=70.0 + wobble % 35, check_seconds=34.0 + wobble % 14,
                assemble_seconds=400.0 + (wobble * 3) % 210)


def _batch(count: int, prefix: str = "KP_") -> Tuple[RunConfig, Dict[str, Plan]]:
    reads, results = _folders()
    samples: List[Sample] = []
    plans: Dict[str, Plan] = {}
    for index in range(1, count + 1):
        name = "%s%03d" % (prefix, index)
        samples.append(Sample(name=name, lanes=_reads(reads, name, index), size_bytes=640 * 1024 * 1024))
        plans[name] = _good_plan(index)
    return RunConfig(results_dir=results, samples=samples), plans


def _normal() -> Scenario:
    config, plans = _batch(8)
    reads, _results = _folders()
    plans["KP_003"] = Plan(
        status=RunStatus.FAILED, fail_at=Stage.CHECK_READS, trim_seconds=40.0, check_seconds=22.0,
        note="A read file is incomplete or damaged (KP_003_S3_L001_R2_001.fastq.gz). It may have been "
             "cut short while copying or downloading.",
        next_step="Copy it again from the original, then try again.")
    config.samples[4] = replace(config.samples[4], lanes=_reads(reads, "KP_005", 5, lanes=2))
    plans["KP_006"] = replace(
        _good_plan(6), status=RunStatus.DONE_NOTES, contigs=312, total_length=5_391_027, n50=38_412,
        largest=141_208, depth=22,
        note="Low depth (about 22x): expect a fragmented assembly.",
        notes=("Low depth (about 22x): expect a fragmented assembly.",
               "The assembly is in many pieces (312 contigs). More sequencing would help."))
    left_out = [("KP_009", "No Read 2 file was found for this sample.")]
    return Scenario("normal", config, plans, left_out=left_out)


def _stopped() -> Scenario:
    made = _normal()
    made.name = "stopped"
    made.ending = ("stop", 3, 0.55)
    made.notices = {1: Notice(kind="battery", text=texts.ON_BATTERY, level="warn")}
    return made


def _vanished() -> Scenario:
    made = _normal()
    made.name = "vanished"
    drive = "E:\\Results\\Klebsiella_Nov" if os.name == "nt" else "/media/usb/Klebsiella_Nov"
    made.config = replace(made.config, results_dir=drive)
    made.ending = ("environment", 5, 0.4,
                   "Stopped: the results folder can no longer be reached (%s). Reconnect the drive, "
                   "then press Continue. The {finished} are safe." % drive)
    return made


def _big() -> Scenario:
    config, plans = _batch(120)
    for index in (17, 58, 96):
        name = "KP_%03d" % index
        plans[name] = replace(plans[name], status=RunStatus.DONE_NOTES, contigs=268 + index, n50=41_000 + index * 37,
                              depth=24, note="Low depth (about 24x): expect a fragmented assembly.")
    plans["KP_041"] = Plan(
        status=RunStatus.FAILED, fail_at=Stage.CHECK_READS, trim_seconds=45.0, check_seconds=20.0,
        note="Read 1 and Read 2 do not match: one file has fewer reads or they are in a different order.",
        next_step="Use the two original files from the sequencer.")
    plans["KP_083"] = Plan(
        status=RunStatus.FAILED, fail_at=Stage.ASSEMBLE, assemble_seconds=120.0,
        note="Too little data to assemble: no contigs were produced.",
        next_step="This sample needs more sequencing.")
    return Scenario("big", config, plans, speed=600.0)


def _interrupted(missing: bool) -> Scenario:
    made = _normal()
    made.name = "missing" if missing else "interrupted"
    left = replace(made.config, samples=made.config.samples[5:])
    made.interrupted = InterruptedDemo(
        config=left, finished=5, total=8, when=time.strftime("%Y-%m-%dT03:12:40"), cause="windows",
        missing=["KP_007"] if missing else [])
    made.left_out = []
    return made


def scenario(name: str) -> Scenario:
    """A fresh copy of the script called ``name`` (one of :data:`SCENARIOS`)."""
    makers: Dict[str, Callable[[], Scenario]] = {
        "normal": _normal, "stopped": _stopped, "vanished": _vanished, "big": _big,
        "interrupted": lambda: _interrupted(False), "missing": lambda: _interrupted(True),
    }
    if name not in makers:
        raise KeyError("no such scenario: %r (choose from %s)" % (name, ", ".join(SCENARIOS)))
    return makers[name]()


# --------------------------------------------------------------------------- virtual time

class VirtualClock:
    """Seconds of the pretend run: ``speed`` of them pass in one real second."""

    def __init__(self, speed: float = 60.0):
        self.speed = max(0.001, float(speed))
        self._start = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self._start) * self.speed

    __call__ = now


# --------------------------------------------------------------------------- the stand-in

class _Stopped(Exception):
    """Raised inside the script when Stop now arrives."""


class DemoRunner(threading.Thread):
    """Plays a :class:`Scenario` with the interface of ``batch.BatchRunner``.

    ``requests`` records every ``request_stop`` (the ``StopMode``) and every ``cancel_stop``
    (the string ``"cancel"``) it received, for the tests.
    """

    def __init__(self, config: RunConfig, events: "queue.Queue[model.Event]", *,
                 engines: Optional[model.EnginePaths] = None,
                 left_out: Sequence[Tuple[str, str]] = (), resume: bool = False,
                 script: Optional[Scenario] = None, clock: Optional[VirtualClock] = None):
        super().__init__(name="demo-runner", daemon=True)
        self.config = config
        self.events = events
        self.left_out = list(left_out)
        self.resume = resume
        self.script = script or Scenario("plain", config, {})
        self.clock = clock or VirtualClock(self.script.speed)
        self.requests: List[Any] = []
        self._lock = threading.Lock()
        self._stop_mode: Optional[StopMode] = None
        self._current = ""
        self._current_started = 0.0
        self._last_working = 0.0

    # ------------------------------------------------------------------ the BatchRunner interface
    def request_stop(self, mode: StopMode) -> None:
        with self._lock:
            self.requests.append(mode)
            if self._stop_mode is not StopMode.NOW:
                self._stop_mode = mode

    def cancel_stop(self) -> None:
        with self._lock:
            self.requests.append("cancel")
            if self._stop_mode is StopMode.AFTER_CURRENT:
                self._stop_mode = None

    def stop_pending(self) -> Optional[StopMode]:
        with self._lock:
            return self._stop_mode

    def current(self) -> Tuple[str, float]:
        with self._lock:
            if not self._current:
                return "", 0.0
            return self._current, max(0.0, self.clock.now() - self._current_started)

    # ------------------------------------------------------------------ playing
    def _post(self, event: model.Event) -> None:
        self.events.put(event)

    def _wait(self, name: str, virtual: float, threads: float = 0.0, memory: float = 0.0) -> None:
        """Let ``virtual`` seconds pass, giving proof of life about once a real second."""
        end = self.clock.now() + max(0.0, virtual)
        first = True
        while True:
            if self.stop_pending() is StopMode.NOW:
                raise _Stopped()
            now_real = time.monotonic()
            if threads and (first or now_real - self._last_working >= 0.8):
                self._last_working = now_real
                wave = (int(now_real * 3) % 5) / 10.0
                self._post(Working(name=name, busy_threads=max(0.4, threads - wave),
                                   memory_bytes=int(memory * (1024 ** 3))))
            first = False
            left = (end - self.clock.now()) / self.clock.speed
            if left <= 0:
                return
            time.sleep(min(0.04, left))

    def _status(self, name: str, status: RunStatus, note: str = "") -> None:
        self._post(SampleStatusChanged(name=name, status=status, note=note))

    def _play_sample(self, sample: Sample, plan: Plan, cut: Optional[float]) -> SampleOutcome:
        """Post the events of one sample. ``cut`` (0..1): raise ``_Stopped`` at that share."""
        name = sample.name
        threads = float(self.config.assembly.threads)
        results = self.config.results_dir
        folder = os.path.join(results, name)
        started = self.clock.now()
        budget = None if cut is None else plan.seconds * cut

        def spend(virtual: float, busy: float, memory: float) -> None:
            nonlocal budget
            if budget is not None:
                if virtual >= budget:
                    self._wait(name, budget, busy, memory)
                    raise _Stopped()
                budget -= virtual
            self._wait(name, virtual, busy, memory)

        def failed(stage: Stage) -> SampleOutcome:
            seen = self.script.__dict__.setdefault("failed_before", set())
            again = " This is the second time. Trying again will not help unless something changes." \
                if name in seen else ""
            seen.add(name)
            return SampleOutcome(
                name=name, status=RunStatus.FAILED, note=plan.note, next_step=plan.next_step + again,
                trimmed=sample.trim, seconds=self.clock.now() - started, finished_at=fsutil.now_iso(),
                folder=folder, log=os.path.join(folder, "last_failed_attempt",
                                                "trimming.log" if stage is Stage.TRIM else "assembly.log"))

        if len(sample.lanes) > 1:
            self._status(name, RunStatus.JOINING_LANES)
            self._post(StageProgress(name=name, stage=Stage.JOIN_LANES,
                                     text="Joining %d lanes into one pair of files" % len(sample.lanes)))
            spend(12.0, 1.0, 0.1)
        if sample.trim:
            self._status(name, RunStatus.TRIMMING)
            self._post(StageProgress(name=name, stage=Stage.TRIM, text="Trimming: reading the reads"))
            self._post(LogLine(name=name, text="scepter: detecting adapters in the first 1,000,000 reads"))
            spend(plan.trim_seconds * 0.3, min(threads, 16.0), 0.6)
            self._post(StageProgress(name=name, stage=Stage.TRIM, text="Trimming: cleaning the reads"))
            self._post(LogLine(name=name, text="scepter: filtering with %d threads" % min(int(threads), 16)))
            spend(plan.trim_seconds * 0.7, min(threads, 16.0), 0.9)
            if plan.fail_at is Stage.TRIM:
                return failed(Stage.TRIM)
        self._status(name, RunStatus.CHECKING_READS)
        for percent in (0, 21, 43, 62, 84, 100):
            self._post(StageProgress(name=name, stage=Stage.CHECK_READS,
                                     text="Checking reads: %d %%" % percent, fraction=percent / 100.0))
            if percent < 100:
                spend(plan.check_seconds / 5.0, 1.0, 0.2)
            if plan.fail_at is Stage.CHECK_READS and percent >= 62:
                self._post(LogLine(name=name, text="preflight: FAIL gzip  unexpected end of file"))
                return failed(Stage.CHECK_READS)
        self._status(name, RunStatus.ASSEMBLING)
        for number, (words, share, lines) in enumerate(ASSEMBLY_STEPS, start=1):
            length = plan.assemble_seconds * share
            memory = (0.8, 1.9, 3.1, 3.4, 2.8, 2.2, 1.1)[number - 1]
            if number == 3:
                for kmer in KMERS:
                    self._post(StageProgress(name=name, stage=Stage.ASSEMBLE,
                                             text="Step 3 of 7: %s (k = %d)" % (words, kmer)))
                    self._post(LogLine(name=name, text="%s (k = %d)" % (lines[0], kmer)))
                    spend(length / len(KMERS), threads, memory)
                self._post(LogLine(name=name, text="  graph: 8,412 nodes, 9,120 edges"))
            else:
                self._post(StageProgress(name=name, stage=Stage.ASSEMBLE,
                                         text="Step %d of 7: %s" % (number, words)))
                for line in lines:
                    self._post(LogLine(name=name, text=line))
                spend(length, threads if number != 7 else 1.0, memory)
            if plan.fail_at is Stage.ASSEMBLE and number == 3:
                self._post(LogLine(name=name, text="error: no contigs were produced"))
                return failed(Stage.ASSEMBLE)
        self._status(name, RunStatus.SUMMARISING)
        self._post(StageProgress(name=name, stage=Stage.SUMMARISE, text="Writing the summary and copying the assembly"))
        self._wait(name, plan.summarise_seconds, 1.0, 0.2)       # these seconds always finish (row 8.4)
        return SampleOutcome(
            name=name, status=plan.status, note=plan.note, notes=list(plan.notes), trimmed=sample.trim,
            contigs=plan.contigs, total_length=plan.total_length, largest=plan.largest, n50=plan.n50,
            depth=plan.depth, thin_retry=plan.thin_retry, seconds=self.clock.now() - started,
            finished_at=fsutil.now_iso(), folder=folder,
            report_html=os.path.join(folder, "assembly", "report.html"))

    def run(self) -> None:                                               # noqa: C901
        script = self.script
        samples = list(self.config.samples)
        names = [sample.name for sample in samples]
        started = self.clock.now()
        states: Dict[str, RunStatus] = {name: RunStatus.WAITING for name in names}
        self._post(BatchStarted(total=len(names), names=list(names)))
        self._post(BatchProgress(position=0, total=len(names)))
        ending = None if self.resume else script.ending
        reason = EndReason.COMPLETED
        message = ""
        spent: List[float] = []
        try:
            self._wait("", script.start_delay)            # the real runner checks the folder first
        except _Stopped:
            samples = []
            reason = EndReason.STOPPED
        for index, sample in enumerate(samples):
            name = sample.name
            plan = script.plans.get(name) or _good_plan(index + 1)
            if not self.resume and index in script.notices:
                self._post(script.notices[index])
            with self._lock:
                self._current, self._current_started = name, self.clock.now()
            done = sum(1 for s in states.values() if s in (RunStatus.DONE, RunStatus.DONE_NOTES))
            bad = sum(1 for s in states.values() if s is RunStatus.FAILED)
            eta = None
            if spent:
                eta = sum(spent) / len(spent) * (len(samples) - index)
            self._post(BatchProgress(position=index + 1, total=len(names), done=done, failed=bad, eta_seconds=eta))
            cut = float(ending[2]) if ending is not None and ending[1] == index else None
            try:
                outcome = self._play_sample(sample, plan, cut)
            except _Stopped:
                with self._lock:
                    self._current = ""
                states[name] = RunStatus.STOPPED
                if ending is not None and ending[0] == "environment" and self.stop_pending() is not StopMode.NOW:
                    reason = EndReason.ENVIRONMENT
                    finished = sum(1 for s in states.values() if s in (RunStatus.DONE, RunStatus.DONE_NOTES))
                    message = str(ending[3]).format(finished=texts.count(finished, "finished sample"))
                    note = "Not finished: the results folder could not be reached."
                else:
                    reason = EndReason.STOPPED
                    note = "Stopped before it had finished. It starts from the beginning next time."
                self._post(SampleFinished(outcome=SampleOutcome(
                    name=name, status=RunStatus.STOPPED, note=note, finished_at=fsutil.now_iso(),
                    folder=os.path.join(self.config.results_dir, name))))
                self._status(name, RunStatus.STOPPED, note)
                break
            with self._lock:
                self._current = ""
            states[name] = outcome.status
            spent.append(outcome.seconds)
            self._post(SampleFinished(outcome=outcome))
            self._status(name, outcome.status, outcome.note)
            if self.stop_pending() is not None and index < len(samples) - 1:
                reason = EndReason.STOPPED
                break
        for name in names:
            if states[name] is RunStatus.WAITING:
                states[name] = RunStatus.NOT_STARTED
                self._status(name, RunStatus.NOT_STARTED)
        counts: Dict[str, int] = {}
        for status in states.values():
            counts[status.value] = counts.get(status.value, 0) + 1
        if self.left_out:
            counts[RunStatus.LEFT_OUT.value] = len(self.left_out)
        seconds = self.clock.now() - started
        done = counts.get("done", 0) + counts.get("done_notes", 0)
        if not message:                                   # the same sentences as the real runner
            if reason is EndReason.COMPLETED:
                parts = ["%d %s" % (counts[status.value], texts.RUN_STATUS_LABEL[status].lower())
                         for status in _COUNTED if counts.get(status.value)]
                message = "Finished at %s. It took %s. %s." % (
                    time.strftime("%H:%M"), _took(seconds), ", ".join(parts) or "No sample was run")
            else:
                parts = ["%d finished" % done] + [
                    "%d %s" % (counts[status.value], word)
                    for status, word in ((RunStatus.FAILED, "failed"), (RunStatus.STOPPED, "stopped"),
                                         (RunStatus.NOT_STARTED, "not started")) if counts.get(status.value)]
                message = "Stopped. %s. %s" % (", ".join(parts), texts.NOTHING_LOST)
        self._post(BatchProgress(position=len(names), total=len(names), done=done,
                                 failed=counts.get("failed", 0), eta_seconds=0.0))
        self._post(BatchFinished(reason=reason, message=message, counts=counts, seconds=seconds))


def _took(seconds: float) -> str:
    """ "9 h 14 min", "14 min", "less than a minute" """
    whole = int(max(0.0, seconds) // 60)
    if whole < 1:
        return "less than a minute"
    if whole < 60:
        return "%d min" % whole
    return "%d h %02d min" % (whole // 60, whole % 60)


def factory(script: Scenario, clock: Optional[VirtualClock] = None,
            made: Optional[List[DemoRunner]] = None) -> Callable[..., DemoRunner]:
    """A ``runner_factory`` for ``RunPage.begin`` that plays ``script``.

    Every runner it makes is appended to ``made`` (when given), so a test can ask the
    stand-in afterwards which stop requests it received.
    """
    def make(config: RunConfig, events: "queue.Queue[model.Event]", *,
             left_out: Sequence[Tuple[str, str]] = (), resume: bool = False, **_ignored: Any) -> DemoRunner:
        runner = DemoRunner(config, events, left_out=left_out, resume=resume, script=script, clock=clock)
        if made is not None:
            made.append(runner)
        return runner
    return make


# --------------------------------------------------------------------------- a bare window

class DemoApp:
    """The few things the Run page asks of the shell (``gui.app.App``), without a shell."""

    def __init__(self, root: Any, look: Any, help_line: Any, script: Scenario,
                 clock: Optional[VirtualClock] = None):
        self.root = root
        self.theme = look
        self.help_line = help_line
        self.engines: Optional[model.EnginePaths] = None
        self.script = script
        self.clock = clock or VirtualClock(script.speed)
        self.runners: List[DemoRunner] = []
        self.calls: List[Tuple[Any, ...]] = []
        self.run_page: Any = None

    def start_run(self, config: RunConfig, left_out: Sequence[Tuple[str, str]], *, resume: bool = False) -> None:
        self.calls.append(("start_run", [s.name for s in config.samples], list(left_out), resume))
        self.run_page.begin(config, left_out, resume=resume,
                            runner_factory=factory(self.script, self.clock, self.runners))

    def run_ended(self) -> None:
        self.calls.append(("run_ended",))

    def show_step(self, index: int) -> None:
        self.calls.append(("show_step", index))

    def new_run(self) -> None:
        self.calls.append(("new_run",))

    def set_running_title(self, text: str) -> None:
        self.calls.append(("title", text))
        try:
            from .app import running_title
            self.root.title(running_title(text))
        except Exception:
            pass


def open_demo(root: Any, name: str, *, speed: Optional[float] = None, start: bool = True) -> Tuple[DemoApp, Any]:
    """Build the Run page in ``root`` (the theme must have been set up) and start ``name``.

    Returns the stand-in shell and the page.
    """
    from . import page_run, theme
    from . import widgets as w

    look = theme.current(root)
    script = scenario(name)
    if speed:
        script.speed = float(speed)
    outer = w.Frame(root, ground="page", padding=(22, 16, 22, 10))
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(0, weight=1)
    help_line = w.HelpLine(outer, lines=1)
    app = DemoApp(root, look, help_line, script)
    page = page_run.RunPage(outer, app)
    page.clock = app.clock.now
    page.preflight = lambda _config: []
    page.interrupted_finder = lambda _folder: script.interrupted
    page.grid(row=0, column=0, sticky="nsew")
    help_line.grid(row=1, column=0, sticky="ew", pady=(look.px(8), 0))
    app.run_page = page
    if script.interrupted is not None:
        page.offer_interrupted(script.config.results_dir)
    elif start:
        app.start_run(script.config, script.left_out)
    return app, page


def _do(page: Any, action: str) -> None:
    verb, _, argument = action.partition(":")
    if verb == "stop":
        page.stop_button.invoke()
    elif verb == "answer":
        if page.open_question is not None:
            page.open_question.invoke(argument)
    elif verb == "close":
        if page.confirm_close():
            page.winfo_toplevel().destroy()
    elif verb == "select":
        page.select_sample(argument)
    elif verb == "log":
        page.log_toggle.invoke()
    elif verb == "continue":
        page.end_buttons["continue"].invoke()
    elif verb == "retry":
        page.end_buttons["retry_failed"].invoke()
    elif verb == "keep_going":
        page.keep_going_button.invoke()
    elif verb == "offer":
        page.notices.button_of("interrupted", 0).invoke()
    else:
        raise SystemExit("unknown action: %s" % action)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import tkinter as tk

    from .. import osapi
    from . import theme
    from . import widgets as w

    parser = argparse.ArgumentParser(prog="python3 -m tesseract_win.gui.demo",
                                     description="The Run page alone, fed by a scripted batch.")
    parser.add_argument("--scenario", default="normal", choices=SCENARIOS)
    parser.add_argument("--dark", action="store_true", help="the dark palette")
    parser.add_argument("--scale", type=float, default=None, help="screen scaling, 1.0 = 96 dpi (e.g. 1.5)")
    parser.add_argument("--speed", type=float, default=None,
                        help="seconds of the pretend run per real second (default: the scenario's)")
    parser.add_argument("--size", default=None, help="window size in pixels, WIDTHxHEIGHT")
    parser.add_argument("--do", action="append", default=[], metavar="ACTION@SECONDS",
                        help="press something at a time after the start (see the module text)")
    args = parser.parse_args(argv)

    root = tk.Tk()
    look = theme.init(root, is_dark=args.dark, scale=args.scale)
    root.title(branding.window_title())
    try:
        root.iconphoto(True, *look.icons.window_icons())
    except tk.TclError:
        pass
    w.set_work_area_provider(osapi.work_area_at)
    w.Dialog.created_hooks.append(lambda window: osapi.set_dark_titlebar(window, look.dark))
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    if args.size:
        width, height = (int(v) for v in args.size.lower().split("x"))
    else:
        width, height = min(screen_w, look.px(1120)), min(screen_h, look.px(780))
    root.geometry("%dx%d+0+0" % (width, height))
    root.minsize(min(width, look.px(900)), min(height, look.px(600)))
    root.update()
    osapi.set_dark_titlebar(root, look.dark)
    _app, page = open_demo(root, args.scenario, speed=args.speed)

    def closing() -> None:
        if page.confirm_close():
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", closing)
    for item in args.do:
        action, _, when = item.rpartition("@")
        root.after(int(float(when) * 1000), lambda a=action: _do(page, a))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
