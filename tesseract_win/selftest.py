# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The program checks itself: ``TesserACT-Win.exe --self-test [--full]``.

Quick part: both engines are found and answer with the pinned versions, the detection
sketch is there, and the optional drag-and-drop package is reported (never a failure).
Full part: the example reads are trimmed and assembled by a real batch, with no window,
and the result must be the known one: two contigs, both circular, 185,200 bases.

The report is plain UTF-8 text with one ``PASS`` or ``FAIL`` line per check. It goes to
the report file and, when the program has one, to standard output; inside the windowed
Windows program there is none. Nothing here imports Tk or opens a window.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import queue
import shutil
import sys
import tempfile
import time
import traceback
from typing import Callable, List, Optional, Tuple

from . import appdirs, branding, engine_proc, fsutil, results, versions
from .model import (AssemblySettings, BatchFinished, EndReason, EnginePaths, Existing, RunConfig,
                    RunStatus, Sample, SampleFinished, SampleOutcome, TrimSettings)

__all__ = ["run_selftest", "write_to_stream", "EXAMPLE_NAME", "EXPECTED_CONTIGS", "EXPECTED_BASES",
           "EXAMPLE_GENOME_MB"]

log = logging.getLogger(__name__)

EXAMPLE_NAME = "Example_isolate"
EXPECTED_CONTIGS = 2
EXPECTED_BASES = 185_200
#: The made-up isolate is 185 kb; told to the read check so that its depth is judged fairly.
EXAMPLE_GENOME_MB = 0.2
_THREADS = 4
_TIMEOUT = 1800.0            # seconds the full part may take before it is called a failure


class _Report:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.failed = 0

    def text(self, line: str = "") -> None:
        self.lines.append(line)

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        if not ok:
            self.failed += 1
        self.lines.append("%s  %s%s" % ("PASS" if ok else "FAIL", what, (": %s" % detail) if detail else ""))
        return ok

    def run(self, what: str, test: Callable[[], Tuple[bool, str]]) -> bool:
        """One check that must not take the others down with it."""
        try:
            ok, detail = test()
        except Exception as error:                       # a check that breaks is a check that failed
            log.exception("self-test: %s", what)
            ok, detail = False, "%s: %s" % (type(error).__name__, error)
        return self.check(ok, what, detail)


def _write(report: _Report, report_path: Optional[str]) -> None:
    text = "\n".join(report.lines) + "\n"
    if report_path:
        try:
            fsutil.atomic_write_text(report_path, text)
        except OSError as error:
            log.error("the self-test report could not be written to %s: %s", report_path, error)
            report.failed += 1
    write_to_stream(sys.stdout, text)                    # None inside the windowed program


def write_to_stream(stream: object, text: str) -> bool:
    """Write ``text`` to a console stream that may be missing, closed or unable to show every character.

    A Windows console or a redirected output often has a code page that cannot hold a path
    with accents or non-Latin letters: such characters are replaced, the rest is written.
    """
    if stream is None:
        return False
    try:
        try:
            stream.write(text)                           # type: ignore[attr-defined]
        except UnicodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(text.encode(encoding, "backslashreplace").decode(encoding))  # type: ignore[attr-defined]
        stream.flush()                                   # type: ignore[attr-defined]
        return True
    except (OSError, ValueError, UnicodeError, LookupError, AttributeError):
        return False


# --------------------------------------------------------------------------- quick part

def _version_check(exe: str, wanted: str) -> Tuple[bool, str]:
    line = engine_proc.engine_version(exe)
    return (wanted in line.split() or line.endswith(wanted)), "%s (expected version %s)" % (line, wanted)


def _drag_and_drop() -> str:
    """What can be said about the optional package without importing Tk."""
    try:
        spec = importlib.util.find_spec("tkinterdnd2")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return "not installed; the Add buttons are used instead"
    folders = list(spec.submodule_search_locations or [])
    library = any(os.path.isdir(os.path.join(folder, "tkdnd")) for folder in folders)
    return "installed" + ("" if library else ", but its tkdnd folder is missing; the Add buttons are used instead")


def _quick(report: _Report) -> Optional[EnginePaths]:
    engines: Optional[EnginePaths] = None
    try:
        engines = appdirs.find_engines()
        report.check(True, "engines found", appdirs.engines_dir())
    except appdirs.EngineMissing as error:
        report.check(False, "engines found", "missing in %s: %s" % (error.folder, ", ".join(error.missing)))
        return None
    report.run("assembler answers", lambda: _version_check(engines.tesseract_asm, versions.TESSERACT_VERSION))
    report.run("trimmer answers", lambda: _version_check(engines.scepter, versions.SCEPTER_VERSION))
    report.check(bool(engines.detect_sketch), "detection sketch present",
                 engines.detect_sketch or "om2detect.sketch is not in %s" % appdirs.engines_dir())
    report.check(True, "drag and drop", _drag_and_drop())
    return engines


# --------------------------------------------------------------------------- full part

def _example_sample() -> Sample:
    folder = appdirs.example_dir()
    read1 = os.path.join(folder, EXAMPLE_NAME + "_R1.fastq.gz")
    read2 = os.path.join(folder, EXAMPLE_NAME + "_R2.fastq.gz")
    for path in (read1, read2):
        if not os.path.isfile(path):
            raise FileNotFoundError("the example read file is missing: %s" % path)
    return Sample(name=EXAMPLE_NAME, lanes=[(read1, read2)], trim=True,
                  size_bytes=os.path.getsize(read1) + os.path.getsize(read2))


def _contig_names(path: str) -> List[Tuple[str, int]]:
    """``(name, length)`` of every record of a FASTA file."""
    records: List[Tuple[str, int]] = []
    with open(path, "r", encoding="ascii", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith(">"):
                records.append((line[1:].split()[0] if len(line) > 1 else "", 0))
            elif records:
                name, length = records[-1]
                records[-1] = (name, length + len(line))
    return records


def _full(report: _Report, engines: EnginePaths, work_dir: str) -> None:
    from .batch import BatchRunner                       # here: the quick part needs none of it

    try:
        sample = _example_sample()
    except OSError as error:
        report.check(False, "example reads present", str(error))
        return
    report.check(True, "example reads present", appdirs.example_dir())

    folder = os.path.join(work_dir, "results")
    config = RunConfig(
        results_dir=folder, samples=[sample], trim=TrimSettings(),
        assembly=AssemblySettings(know_genome_size=True, genome_size_mb=EXAMPLE_GENOME_MB,
                                  threads=max(1, min(_THREADS, os.cpu_count() or 1))),
        existing=Existing.OVERWRITE)
    events: "queue.Queue" = queue.Queue()
    runner = BatchRunner(config, events, engines=engines)
    began = time.monotonic()
    runner.start()
    outcome: Optional[SampleOutcome] = None
    finished: Optional[BatchFinished] = None
    while finished is None and time.monotonic() - began < _TIMEOUT:
        try:
            event = events.get(timeout=1.0)
        except queue.Empty:
            if not runner.is_alive() and events.empty():
                break
            continue
        if isinstance(event, SampleFinished) and event.outcome is not None:
            outcome = event.outcome
        elif isinstance(event, BatchFinished):
            finished = event
    if finished is None:
        from .model import StopMode
        runner.request_stop(StopMode.NOW)
        runner.join(60)                                  # the work folder may be deleted after this
        report.check(False, "the example run ended", "no end after %d seconds" % int(time.monotonic() - began))
        return
    runner.join(30)
    report.check(finished.reason is EndReason.COMPLETED, "the example run ended", finished.message)

    # Done, and nothing less: a note on the example ("Done (check notes)") means that this
    # build does not give the known result, or that the result could not be put in place.
    done = outcome is not None and outcome.status is RunStatus.DONE
    detail = "no result" if outcome is None else outcome.status.value
    if outcome is not None and (outcome.note or outcome.next_step):
        detail += " (%s)" % " ".join(t for t in (outcome.note, outcome.next_step) if t)
    report.check(done, "the example sample is done", "%s, in %s" % (detail, fsutil.format_duration(
        outcome.seconds if outcome is not None else 0)))

    fasta = results.collected_fasta(folder, EXAMPLE_NAME)

    def contigs() -> Tuple[bool, str]:
        records = _contig_names(results.contigs_path(folder, EXAMPLE_NAME))
        return len(records) == EXPECTED_CONTIGS, "%d (expected %d)" % (len(records), EXPECTED_CONTIGS)

    def circular() -> Tuple[bool, str]:
        records = _contig_names(results.contigs_path(folder, EXAMPLE_NAME))
        return (bool(records) and all(name.endswith("_circular") for name, _l in records),
                ", ".join(name for name, _l in records) or "no contigs")

    def bases() -> Tuple[bool, str]:
        total = sum(length for _n, length in _contig_names(results.contigs_path(folder, EXAMPLE_NAME)))
        return total == EXPECTED_BASES, "{:,} (expected {:,})".format(total, EXPECTED_BASES)

    report.run("the assembly has two contigs", contigs)
    report.run("both contigs are circular", circular)
    report.run("the assembly has 185,200 bases", bases)
    for what, path in (("run_summary.html written", results.summary_html_path(folder)),
                       ("run_summary.tsv written", results.summary_tsv_path(folder)),
                       ("assemblies/%s.fasta written" % EXAMPLE_NAME, fasta)):
        present = os.path.isfile(path) and os.path.getsize(path) > 0
        report.check(present, what, path if present else "missing: %s" % path)


def run_selftest(full: bool = False, report_path: Optional[str] = None,
                 work_dir: Optional[str] = None) -> int:
    """Run the self-test; 0 when every check passed, 1 otherwise. Never raises.

    ``full``: also trim and assemble the example. ``report_path``: where the report is
    written (also printed when the program has a standard output). ``work_dir``: where the
    full part works; left out, a temporary folder is used and deleted afterwards.
    """
    report = _Report()
    report.text("%s self-test%s" % (branding.versions_line(), " (full)" if full else ""))
    report.text("%s; Python %s; %s" % (fsutil.now_iso(), sys.version.split()[0], sys.platform))
    temporary = ""
    try:
        engines = _quick(report)
        if full:
            if engines is None:
                report.check(False, "the example run ended", "not tried: the engines are missing")
            else:
                if not work_dir:
                    work_dir = temporary = tempfile.mkdtemp(prefix="tesseract-win-selftest-")
                os.makedirs(work_dir, exist_ok=True)
                _full(report, engines, work_dir)
    except Exception:
        log.exception("the self-test broke")
        report.check(False, "the self-test ran to its end", traceback.format_exc().strip().splitlines()[-1])
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)
    report.text("")
    report.text("RESULT: %s" % ("PASS" if report.failed == 0 else "FAIL (%d)" % report.failed))
    _write(report, report_path)
    return 0 if report.failed == 0 else 1
