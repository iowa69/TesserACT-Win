# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""One sample from its read files to a finished result: decision D8, steps 1 to 7.

Everything happens inside the attempt folder of :func:`results.begin_attempt`
(``<sample>/_in_progress``). Only a complete, successful attempt is moved into place, so a
stop, a failure or a crash at any moment leaves an earlier result of the sample exactly
as it was.

1. join the lanes (bytes appended in lane order);
2. if the sample is to be trimmed: check the raw pair, then run scepter;
3. the read check on the reads that will be assembled (``assembly/preflight.txt``);
4. ``tesseract-asm``; 5. the second attempt for very thin data;
6. the summary (``SUMMARY.txt``, ``assembly_stats.tsv``);
7. move into place, copy the contigs to ``assemblies``, write the status file last.

A stop request is obeyed at any moment before step 6; steps 6 and 7 take seconds and are
allowed to finish. :func:`run_sample` never raises: whatever goes wrong becomes an outcome
with a plain note, and the traceback goes to the program's log.

The caller gets events through ``post`` (ready-made texts, a few per second at most) and,
through a :class:`SampleTrace`, the facts it needs to decide whether the PC rather than
the sample was at fault (decision D9). This module never touches Tk and starts engines
only through the one :class:`osapi.ProcessGroup` it is given.
"""

from __future__ import annotations

import logging
import os
import time
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import engine_proc, fsutil, messages, osapi, postflight, readcheck, results, settings_map
from .messages import Explained
from .model import (EnginePaths, Event, LogLine, RunConfig, RunStatus, Sample, SampleOutcome,
                    SampleStatusChanged, Stage, StageProgress, Working)

__all__ = ["run_sample", "SampleTrace", "settings_text", "engine_version_line", "IDLE_TIMEOUT"]

log = logging.getLogger(__name__)

#: An engine that uses no processor time at all for this long is ended (audit row 7.8).
IDLE_TIMEOUT = 1800.0
_COPY_CHUNK = 1 << 20
_STAGE_GAP = 0.25            # seconds between two percentage lines
_LOG_GAP = 0.35              # seconds between two LogLine events
_LOG_LINES = 200             # most lines one LogLine event carries

_monotonic = time.monotonic


@dataclass
class SampleTrace:
    """What the batch needs to know about a run beside its outcome. Filled by :func:`run_sample`."""
    code: str = ""                   #: how it failed, for the program ("" when it did not fail)
    about_data: bool = False         #: the failure lies in the sample's read files
    environment: str = ""            #: not empty: the PC's failure ended the sample (a messages code)
    environment_path: str = ""       #: the file or folder that failure is about
    explained: Optional[Explained] = None    #: the failure in words
    reached: Optional[RunStatus] = None      #: the last stage that was begun


class _Stop(Exception):
    """The user asked to stop now."""


class _Failure(Exception):
    def __init__(self, explained: Explained, log_path: str = "") -> None:
        super().__init__(explained.note)
        self.explained = explained
        self.log_path = log_path


def settings_text(config: RunConfig, trimmed: bool) -> str:
    """How a result was made, for its status file: ``"not trimmed; mode Careful"``."""
    return "; ".join(settings_map.differences(config.trim, config.assembly, trimmed=trimmed)) \
        or "standard settings"


_VERSION_LINES: Dict[Tuple[str, float], str] = {}


def engine_version_line(asm_exe: str) -> str:
    """The assembler's own version line for ``SUMMARY.txt`` ("" when it does not answer). Asked once."""
    try:
        key = (asm_exe, os.path.getmtime(asm_exe))
    except OSError:
        return ""
    if key not in _VERSION_LINES:
        try:
            _VERSION_LINES[key] = postflight.engine_version_line(engine_proc.engine_version(asm_exe))
        except engine_proc.EngineStartError as error:
            log.warning("the assembler did not give its version: %s", error)
            return ""
    return _VERSION_LINES[key]


def _is_gzip(path: str) -> bool:
    with open(path, "rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def run_sample(sample: Sample, config: RunConfig, engines: EnginePaths, *,
               group: osapi.ProcessGroup, stop_event: threading.Event,
               post: Callable[[Event], Any],
               check_surroundings: Optional[Callable[[], Optional[Explained]]] = None,
               trace: Optional[SampleTrace] = None, engine_version: Optional[str] = None,
               run_log: Optional[Callable[[str], Any]] = None,
               idle_timeout: float = IDLE_TIMEOUT) -> SampleOutcome:
    """Trim (if asked), check and assemble one sample; put the result in place; say how it went.

    ``post(event)`` receives ``SampleStatusChanged`` (the running statuses only: the caller
    announces the final one), ``StageProgress``, ``Working`` and ``LogLine`` events; a
    ``LogLine`` may carry several lines. ``stop_event`` set means "stop now": the attempt is
    given up and the outcome is STOPPED. ``check_surroundings()`` is asked when something
    failed; if it names a failure of the PC (an :class:`messages.Explained` whose
    ``environment`` is set), the sample is not blamed: its outcome is STOPPED with that
    reason and ``trace.environment`` is set. ``engine_version`` is the assembler's version
    line (None: asked from the engine). ``run_log(text)`` receives one-line remarks for the
    run log. Call it from the thread that will wait: the engines are started here.
    """
    run = _SampleRun(sample, config, engines, group, stop_event, post, check_surroundings,
                     trace if trace is not None else SampleTrace(), engine_version, run_log,
                     idle_timeout)
    return run.run()


class _SampleRun:
    def __init__(self, sample: Sample, config: RunConfig, engines: EnginePaths,
                 group: osapi.ProcessGroup, stop_event: threading.Event,
                 post: Callable[[Event], Any],
                 check_surroundings: Optional[Callable[[], Optional[Explained]]],
                 trace: SampleTrace, engine_version: Optional[str],
                 run_log: Optional[Callable[[str], Any]], idle_timeout: float) -> None:
        self.sample, self.config, self.engines = sample, config, engines
        self.group, self.stop_event = group, stop_event
        self._post_event, self.check_surroundings = post, check_surroundings
        self.trace, self.engine_version, self._run_log = trace, engine_version, run_log
        self.idle_timeout = idle_timeout
        self.name = sample.name
        self.results_dir = config.results_dir
        self.began = _monotonic()
        self.warnings: List[Explained] = []
        self.depth: Optional[int] = None
        self.retried = False
        self.earlier: Optional[results.SampleRecord] = None
        self._last_stage: Tuple[Any, str] = (None, "")
        self._stage_at = 0.0
        self._log_buffer: List[str] = []
        self._log_dropped = False
        self._log_at = 0.0

    # ------------------------------------------------------------------ events

    def _post(self, event: Event) -> None:
        try:
            self._post_event(event)
        except Exception:                                # a listener must never break a run
            log.exception("posting an event failed")

    def _status(self, status: RunStatus) -> None:
        self._flush_log()
        self.trace.reached = status
        self._last_stage = (None, "")
        self._post(SampleStatusChanged(name=self.name, status=status))

    def _stage(self, stage: Stage, text: str, fraction: Optional[float] = None) -> None:
        if (stage, text) == self._last_stage or not text:
            return
        now = _monotonic()
        if fraction is not None and fraction < 1.0 and now - self._stage_at < _STAGE_GAP \
                and self._last_stage[0] is stage:
            return
        self._last_stage, self._stage_at = (stage, text), now
        self._post(StageProgress(name=self.name, stage=stage, text=text, fraction=fraction))

    def _log(self, line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        self._log_buffer.append(text)
        if len(self._log_buffer) > _LOG_LINES:
            del self._log_buffer[0]
            self._log_dropped = True
        if _monotonic() - self._log_at >= _LOG_GAP:
            self._flush_log()

    def _flush_log(self) -> None:
        if self._log_buffer:
            lines = (["..."] if self._log_dropped else []) + self._log_buffer
            self._log_buffer, self._log_dropped = [], False
            self._log_at = _monotonic()
            self._post(LogLine(name=self.name, text="\n".join(lines)))

    def _remark(self, text: str) -> None:
        if self._run_log is not None:
            try:
                self._run_log("%s: %s" % (self.name, text))
            except Exception:
                log.exception("writing to the run log failed")

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise _Stop()

    # ------------------------------------------------------------------ the run

    def run(self) -> SampleOutcome:
        try:
            try:
                outcome = self._attempt()
            except _Stop:
                outcome = self._stopped(None)
            except _Failure as failure:
                outcome = self._failed(failure.explained, failure.log_path)
            except readcheck.Cancelled:
                outcome = self._stopped(None)
            except results.NewerFormat:
                outcome = self._failed(messages.explain_environment("newer_version", self.results_dir), "")
            except OSError as error:
                log.exception("%s: a file operation failed", self.name)
                self._remark("a file operation failed: %s" % error)
                outcome = self._failed(self._explain_os_error(error), "")
            except Exception:
                log.exception("%s: unexpected error", self.name)
                self._remark("unexpected error inside the program (the details are in the program's log)")
                outcome = self._failed(messages.internal_error(), "")
        except Exception:                                # the clean-up itself failed
            log.exception("%s: could not record how the sample ended", self.name)
            explained = messages.internal_error()
            self.trace.code, self.trace.explained = explained.code, explained
            outcome = SampleOutcome(name=self.name, status=RunStatus.FAILED, note=explained.note,
                                    next_step=explained.next_step, trimmed=bool(self.sample.trim),
                                    seconds=_monotonic() - self.began, finished_at=fsutil.now_iso(),
                                    folder=results.sample_dir(self.results_dir, self.name))
        self._flush_log()
        return outcome

    def _explain_os_error(self, error: OSError) -> Explained:
        for pair in self.sample.lanes:
            for path in pair:
                if not os.path.isfile(path):
                    return messages.reads_gone(path)
        return messages.internal_error()

    def _attempt(self) -> SampleOutcome:
        sample, name, rd = self.sample, self.name, self.results_dir
        self._check_stop()
        if not sample.lanes:
            raise _Failure(messages.reads_gone())
        self.earlier = results.read_status(rd, name)
        try:
            work = results.begin_attempt(rd, name)
        except OSError as error:
            log.warning("%s: the attempt folder could not be prepared: %s", name, error)
            raise _Failure(Explained(
                "The sample's folder in the results folder could not be prepared: something in it "
                "could not be replaced.",
                "Close any program that has files of this sample open, then try the sample again.",
                "folder_busy")) from error
        asm_dir = results.in_progress_assembly_dir(rd, name)
        os.makedirs(asm_dir, exist_ok=True)

        read1, read2 = self._join_lanes(work)                     # step 1
        if sample.trim:                                           # step 2
            read1, read2 = self._trim(work, read1, read2)
        if not getattr(sample, "assemble", True):                 # trimming was all that was asked for
            if not sample.trim:
                raise _Failure(Explained("Nothing was ticked for this sample: neither trimming nor assembly.",
                                         "Tick Trim, Assemble or both on the Samples page.", "nothing_to_do"))
            self._check_stop()
            return self._finish_trimmed_only(work)
        shown1, shown2 = self._shown(work, read1), self._shown(work, read2)
        check = self._check_reads(read1, read2, shown1, shown2, asm_dir)      # step 3
        result_args = self._assemble(work, asm_dir, shown1, shown2, check)    # steps 4 and 5

        self._check_stop()                                        # the last moment a stop is obeyed
        return self._finish(asm_dir, shown1, shown2, result_args)             # steps 6 and 7

    @staticmethod
    def _shown(work: str, path: str) -> str:
        """A read file as the engine is given it: relative inside the attempt folder, else as it is."""
        try:
            relative = os.path.relpath(path, work)
        except ValueError:                               # another drive on Windows
            return path
        return path if relative.startswith("..") else relative

    # ------------------------------------------------------------------ step 1

    def _join_lanes(self, work: str) -> Tuple[str, str]:
        lanes = self.sample.lanes
        if len(lanes) == 1:
            return os.path.abspath(lanes[0][0]), os.path.abspath(lanes[0][1])
        self._status(RunStatus.JOINING_LANES)
        self._stage(Stage.JOIN_LANES, messages.joining_text(0.0), 0.0)
        files = [path for pair in lanes for path in pair]
        try:
            packed = [_is_gzip(path) for path in files]
        except OSError as error:
            raise _Failure(messages.reads_gone(getattr(error, "filename", "") or "")) from error
        if any(packed) and not all(packed):
            raise _Failure(Explained(
                "The lanes of this sample are a mix of compressed and uncompressed files, which "
                "cannot be joined.", "Compress all of the files or none of them, then add the sample again.",
                "lanes_mixed", about_data=True))
        joined = results.in_progress_joined_dir(self.results_dir, self.name)
        os.makedirs(joined, exist_ok=True)
        suffix = ".fastq.gz" if all(packed) else ".fastq"
        total = max(1, sum(_size(path) for path in files))
        done = 0
        outputs = []
        for index in (0, 1):
            target_path = os.path.join(joined, "%s_R%d%s" % (self.name, index + 1, suffix))
            outputs.append(target_path)
            with open(target_path, "wb") as target:
                last = b"\n"
                for pair in lanes:
                    try:
                        source = open(pair[index], "rb")
                    except OSError as error:
                        raise _Failure(messages.reads_gone(pair[index])) from error
                    with source:
                        if not all(packed) and last != b"\n":
                            target.write(b"\n")          # a plain file that does not end its last line
                        while True:
                            self._check_stop()
                            try:
                                chunk = source.read(_COPY_CHUNK)
                            except OSError as error:
                                raise _Failure(messages.reads_gone(pair[index])) from error
                            if not chunk:
                                break
                            target.write(chunk)
                            last = chunk[-1:]
                            done += len(chunk)
                            self._stage(Stage.JOIN_LANES, messages.joining_text(done / total), done / total)
        self._remark("joined %d lanes" % len(lanes))
        return outputs[0], outputs[1]

    # ------------------------------------------------------------------ step 2

    def _trim(self, work: str, read1: str, read2: str) -> Tuple[str, str]:
        name = self.name
        self._status(RunStatus.TRIMMING)
        self._stage(Stage.TRIM, messages.pair_check_text(0.0), 0.0)
        try:
            pair = readcheck.check_input_pair(
                read1, read2, cancel=self.stop_event,
                progress=lambda f: self._stage(Stage.TRIM, messages.pair_check_text(f), f))
        except readcheck.Cancelled:
            raise _Stop() from None
        except OSError as error:
            raise _Failure(messages.reads_gone(getattr(error, "filename", "") or read1)) from error
        if not pair.ok:
            for problem in pair.problems:
                self._log("Read files: %s" % problem.message)
            raise _Failure(messages.explain_input_problem(pair.problems[0]))

        trim_dir = results.in_progress_trimming_dir(self.results_dir, name)
        os.makedirs(trim_dir, exist_ok=True)
        files = settings_map.trimming_files(name)
        argv = settings_map.scepter_command(
            self.engines.scepter, self.config.trim, read1=read1, read2=read2,
            out1=files.read1, out2=files.read2, json_report=files.json_report,
            html_report=files.html_report, title=name, threads=max(1, int(self.config.assembly.threads)))
        log_path = os.path.join(trim_dir, files.log)
        self._stage(Stage.TRIM, messages.STARTING_TRIMMER)
        result = self._engine(engine_proc.ENGINE_TRIMMER, argv, trim_dir, log_path)
        if not result.ok:
            raise _Failure(self._engine_failure(engine_proc.ENGINE_TRIMMER, result, self.engines.scepter), log_path)
        missing = [f for f in (files.read1, files.read2, files.json_report, files.html_report)
                   if not os.path.isfile(os.path.join(trim_dir, f))]
        if missing:
            raise _Failure(messages.explain_trimming_result("files_missing", ", ".join(missing)), log_path)
        report = fsutil.read_json(os.path.join(trim_dir, files.json_report))
        try:
            left = int(report["summary"]["after_filtering"]["total_reads"])
        except (KeyError, TypeError, ValueError):
            raise _Failure(messages.explain_trimming_result("report_unreadable"), log_path) from None
        if left <= 0:
            raise _Failure(messages.explain_trimming_result("no_reads_left"), log_path)
        self._remark("trimmed in %s; %s reads kept" % (fsutil.format_duration(result.seconds),
                                                       "{:,}".format(left)))
        return os.path.join(trim_dir, files.read1), os.path.join(trim_dir, files.read2)

    # ------------------------------------------------------------------ engines

    def _engine(self, engine: str, argv: Sequence[str], cwd: str, log_path: str, *,
                retry: bool = False) -> engine_proc.EngineResult:
        trimmer = engine == engine_proc.ENGINE_TRIMMER

        def on_line(line: str) -> None:
            progress = engine_proc.parse_progress(engine, line)
            if trimmer:
                if progress is not None:
                    if progress.fraction is not None:
                        self._stage(Stage.TRIM, messages.trimming_text(progress.fraction), progress.fraction)
                    return                               # the bar that is rewritten in place is not a log line
            elif progress is not None:
                self._stage(Stage.ASSEMBLE, messages.step_text(progress, retry=retry))
            self._log(line)

        def on_working(busy: float, memory: int) -> None:
            self._post(Working(name=self.name, busy_threads=busy, memory_bytes=memory))

        environment = settings_map.engine_environment(os.environ, self.engines.detect_sketch)
        result = engine_proc.run_engine(
            argv, cwd=cwd, env=environment, log_path=log_path, group=self.group,
            stop_event=self.stop_event, on_line=on_line, on_working=on_working,
            low_priority=True, idle_timeout=self.idle_timeout)
        self._flush_log()
        if result.stopped or (self.stop_event.is_set() and not result.ok):
            raise _Stop()
        if not result.ok:
            log.warning("%s: %s ended with code %r (%s)%s", self.name, engine, result.returncode,
                        result.kind.value, ", after no activity" if result.hung else "")
            self._remark("%s ended with code %r (%s); its last error line: %s" % (
                engine, result.returncode, result.kind.value,
                engine_proc.last_error_line(result.log_tail) or "none"))
        return result

    def _engine_failure(self, engine: str, result: engine_proc.EngineResult, exe: str) -> Explained:
        explained = messages.explain_engine_end(
            engine, result.kind, log_tail=result.log_tail, hung=result.hung, exe=exe,
            idle_minutes=max(1, int(self.idle_timeout // 60)))
        if explained is None:
            return messages.internal_error()
        if explained.environment == "engine_missing" and os.path.isfile(exe):
            # The file is there and still did not start: damaged, or a part it needs is missing.
            return messages.explain_environment("engine_broken", exe)
        return explained

    # ------------------------------------------------------------------ step 3

    def _check_reads(self, read1: str, read2: str, shown1: str, shown2: str,
                     asm_dir: str) -> readcheck.ReadCheck:
        self._status(RunStatus.CHECKING_READS)
        self._stage(Stage.CHECK_READS, messages.checking_text(0.0), 0.0)
        try:
            check = readcheck.check_reads(
                read1, read2, out_dir=asm_dir,
                genome_size_mb=settings_map.expected_genome_size_mb(self.config.assembly),
                progress=lambda f: self._stage(Stage.CHECK_READS, messages.checking_text(f), f),
                cancel=self.stop_event, available_memory_bytes=osapi.available_memory_bytes(),
                shown_read1=shown1, shown_read2=shown2, shown_out=os.path.basename(asm_dir))
        except readcheck.Cancelled:
            raise _Stop() from None
        self.depth = check.stats.depth if check.stats.ran else None
        for line in check.lines:
            if line.verdict != "OK":
                self._log("Read check: %s" % line.text.strip())
        trimmed = bool(self.sample.trim)
        fails = messages.worst_first(messages.explain_read_check(line, trimmed=trimmed)
                                     for line in check.lines if line.verdict == "FAIL")
        # Little free memory is a remark about the PC at that moment, not about this result: it
        # stays in preflight.txt and the batch shows its own notice; it does not mark the sample.
        warns = messages.worst_first(messages.explain_read_check(line, trimmed=trimmed)
                                     for line in check.lines
                                     if line.verdict == "WARN" and line.name != "memory")
        of_the_pc = [found for found in fails if found.environment]
        if of_the_pc:
            raise _Failure(of_the_pc[0], check.preflight_path)
        if fails and not self.config.assembly.force:
            raise _Failure(fails[0], check.preflight_path)
        self.warnings += fails + warns                   # with "assemble anyway", a failure is a note
        return check

    # ------------------------------------------------------------------ steps 4 and 5

    def _assemble(self, work: str, asm_dir: str, shown1: str, shown2: str,
                  check: readcheck.ReadCheck) -> List[str]:
        """Runs the assembler (twice for very thin data). Returns the extra arguments it was given."""
        assembly = self.config.assembly
        out_name = os.path.basename(asm_dir)
        log_path = os.path.join(asm_dir, "assembly.log")
        expected = settings_map.expected_genome_size_mb(assembly)

        def command(kmers: Optional[str] = None) -> Tuple[List[str], List[str]]:
            plain = settings_map.tesseract_command("", assembly, read1="", read2="", out_dir_name="")
            argv = settings_map.tesseract_command(self.engines.tesseract_asm, assembly, read1=shown1,
                                                  read2=shown2, out_dir_name=out_name, kmers=kmers)
            first_extra = plain.index("-t") + 2          # what follows "-t N" are the extra arguments
            return argv, argv[first_extra:]

        self._status(RunStatus.ASSEMBLING)
        self._stage(Stage.ASSEMBLE, messages.STARTING_ASSEMBLER)
        argv, extra = command()
        result = self._engine(engine_proc.ENGINE_ASSEMBLER, argv, work, log_path)
        if result.ok:
            decision = postflight.thin_retry_needed(asm_dir, exit_code=0, engine_args=extra,
                                                    genome_size_mb=expected,
                                                    stats_ran=check.has_statistics)
            if decision.needed:
                self._check_stop()
                self._log(decision.message)
                self._remark("very little data: second attempt with the settings for thin data")
                postflight.keep_first_attempt(asm_dir)
                self._last_stage = (None, "")
                self._stage(Stage.ASSEMBLE, messages.THIN_DATA_RETRY)
                argv, extra = command(settings_map.THIN_DATA_KMERS)
                result = self._engine(engine_proc.ENGINE_ASSEMBLER, argv, work, log_path, retry=True)
                self.retried = True
        if not result.ok:
            try:                                         # SUMMARY.txt of the failure, for the record
                postflight.summarise(asm_dir, read1=shown1, read2=shown2,
                                     exit_code=result.returncode if result.returncode is not None else 1,
                                     engine_version=self._version(), genome_size_mb=expected,
                                     engine_args=extra, retried=self.retried, shown_out=out_name)
            except Exception:
                log.exception("%s: the summary of the failed attempt could not be written", self.name)
            raise _Failure(self._engine_failure(engine_proc.ENGINE_ASSEMBLER, result,
                                                self.engines.tesseract_asm), log_path)
        self._remark("assembled in %s; most memory in use %s" % (
            fsutil.format_duration(result.seconds), fsutil.format_bytes(result.peak_memory_bytes)))
        return extra

    def _version(self) -> str:
        if self.engine_version is None:
            self.engine_version = engine_version_line(self.engines.tesseract_asm)
        return self.engine_version

    # ------------------------------------------------------------------ steps 6 and 7

    def _finish(self, asm_dir: str, shown1: str, shown2: str, extra: List[str]) -> SampleOutcome:
        sample, config, name, rd = self.sample, self.config, self.name, self.results_dir
        log_path = os.path.join(asm_dir, "assembly.log")
        if not os.path.isdir(asm_dir):                   # never write a summary into a folder that has gone
            raise FileNotFoundError(2, "the attempt folder has vanished", asm_dir)
        self._status(RunStatus.SUMMARISING)
        self._stage(Stage.SUMMARISE, messages.WRITING_SUMMARY)
        summary = postflight.summarise(
            asm_dir, read1=shown1, read2=shown2, exit_code=0, engine_version=self._version(),
            genome_size_mb=settings_map.expected_genome_size_mb(config.assembly), engine_args=extra,
            retried=self.retried, shown_out=os.path.basename(asm_dir))
        for line in summary.lines:
            if line.verdict != "OK":
                self._log("Summary: %s" % line.text.strip())
        fails = messages.worst_first(messages.explain_summary(line, summary.facts)
                                     for line in summary.lines if line.verdict == "FAIL")
        if fails:
            raise _Failure(fails[0], log_path)
        self.warnings += messages.worst_first(messages.explain_summary(line, summary.facts)
                                              for line in summary.lines if line.verdict == "WARN")
        warnings = messages.worst_first(self.warnings)
        stats = summary.contigs
        outcome = SampleOutcome(
            name=name, status=RunStatus.DONE, trimmed=bool(sample.trim),
            contigs=stats.contigs if stats else None, total_length=stats.total_length if stats else None,
            largest=stats.largest if stats else None, n50=stats.n50 if stats else None,
            depth=self.depth, thin_retry=self.retried,
            folder=results.sample_dir(rd, name))

        try:
            commit = results.commit_attempt(rd, name, keep_trimmed_reads=bool(config.trim.keep_trimmed_reads))
        except results.AttemptIncomplete:
            raise _Failure(messages.too_little_data(), log_path) from None
        if commit.in_place:
            results.collect(rd, name)
            report = results.report_html_path(rd, name)
            outcome.log = os.path.join(results.assembly_dir(rd, name), "assembly.log")
        else:
            warnings.insert(0, Explained(messages.new_result_note(name, commit.reason)))
            outcome.folder = commit.folder
            report = os.path.join(commit.folder, os.path.basename(asm_dir), "report.html")
            outcome.log = os.path.join(commit.folder, os.path.basename(asm_dir), "assembly.log")
            self._remark("the old result could not be replaced (%s); the new one is in %s"
                         % (commit.reason, commit.folder))
        if warnings:
            # The first warning is the note of the row (with its advice as the next step); the
            # list repeats it word for word, so that a reader of both can leave one out, and
            # gives every further warning together with its advice.
            outcome.status = RunStatus.DONE_NOTES
            outcome.note, outcome.next_step = warnings[0].note, warnings[0].next_step
            outcome.notes = [warnings[0].note] + [found.text for found in warnings[1:]]
        outcome.report_html = report if os.path.isfile(report) else ""
        outcome.seconds = _monotonic() - self.began
        outcome.finished_at = fsutil.now_iso()
        results.write_status(rd, outcome, reads=results.reads_of(sample),
                             settings_fingerprint=settings_map.settings_fingerprint(
                                 config.trim, config.assembly, bool(sample.trim)),
                             settings_text=settings_text(config, bool(sample.trim)),
                             trimmed=bool(sample.trim))
        return outcome

    def _finish_trimmed_only(self, work: str) -> SampleOutcome:
        """Assembly not ticked: keep the trimmed reads and their report, and nothing else."""
        sample, config, name, rd = self.sample, self.config, self.name, self.results_dir
        self._status(RunStatus.SUMMARISING)
        self._stage(Stage.SUMMARISE, "Saving the trimmed reads")
        new = os.path.join(work, results.TRIMMING)
        final = results.trimming_dir(rd, name)
        if not os.path.isdir(new):
            raise FileNotFoundError(2, "the attempt folder has vanished", new)
        fsutil.remove_tree(final)
        fsutil.move_dir(new, final)
        fsutil.remove_tree(work)
        report = os.path.join(final, "trimming_report.html")
        outcome = SampleOutcome(
            name=name, status=RunStatus.DONE, trimmed=True,
            note="Trimmed only: Assemble was not ticked for this sample.",
            folder=results.sample_dir(rd, name),
            report_html=report if os.path.isfile(report) else "",
            log=os.path.join(final, "trimming.log"),
            seconds=_monotonic() - self.began, finished_at=fsutil.now_iso())
        results.write_status(rd, outcome, reads=results.reads_of(sample),
                             settings_fingerprint=settings_map.settings_fingerprint(
                                 config.trim, config.assembly, True),
                             settings_text=settings_text(config, True), trimmed=True)
        return outcome

    # ------------------------------------------------------------------ endings that are not "done"

    def _earlier_kept(self) -> Tuple[bool, str]:
        """Is a finished result of this sample (still) in place, and when was it made?"""
        try:
            record = results.read_status(self.results_dir, self.name)
        except Exception:
            return False, ""
        if record is not None and record.assembled:
            return True, record.finished_at
        return False, ""

    def _failed_before_the_same_way(self, note: str) -> bool:
        earlier = self.earlier
        if earlier is None or not note:
            return False
        before = ""
        if earlier.status is RunStatus.FAILED:
            before = earlier.outcome.note or ""
        elif isinstance(earlier.last_attempt, dict):
            before = str(earlier.last_attempt.get("note") or "")
        return before.startswith(note)

    def _failed(self, explained: Explained, log_path: str) -> SampleOutcome:
        rd, name = self.results_dir, self.name
        if self.check_surroundings is not None:
            # Asked also when the failure already looks like the PC's: an engine that "could not
            # be started" because its working folder is gone is a results folder that is gone.
            try:
                found = self.check_surroundings()
            except Exception:
                log.exception("checking the surroundings failed")
                found = None
            if found is not None and found.environment:
                explained = found
        if explained.environment:
            return self._stopped(explained)

        kept = results.abandon_attempt(rd, name, keep_logs=True)
        kept_log = results.kept_log_path(rd, name, log_path) if log_path else ""
        if not kept_log and kept:
            preferred = [p for p in kept if p.endswith(".log")]
            kept_log = (preferred or kept)[-1]
        earlier_kept, earlier_when = self._earlier_kept()
        note = explained.note
        if self._failed_before_the_same_way(note):
            note = messages.second_time(note)
        if earlier_kept:
            note = messages.with_earlier_kept(note, earlier_when)
        warnings = messages.worst_first(self.warnings)
        outcome = SampleOutcome(
            name=name, status=RunStatus.FAILED, note=note, notes=[found.text for found in warnings],
            next_step=explained.next_step, trimmed=bool(self.sample.trim), depth=self.depth,
            thin_retry=self.retried, seconds=_monotonic() - self.began, finished_at=fsutil.now_iso(),
            folder=results.sample_dir(rd, name), log=kept_log, kept_earlier_result=earlier_kept)
        self.trace.code, self.trace.about_data, self.trace.explained = (
            explained.code or "failed", explained.about_data, explained)
        try:
            results.write_status(rd, outcome, reads=results.reads_of(self.sample),
                                 settings_fingerprint=settings_map.settings_fingerprint(
                                     self.config.trim, self.config.assembly, bool(self.sample.trim)),
                                 settings_text=settings_text(self.config, bool(self.sample.trim)),
                                 trimmed=bool(self.sample.trim))
        except Exception:
            log.exception("%s: the failure could not be recorded in the sample's folder", name)
        return outcome

    def _stopped(self, environment: Optional[Explained]) -> SampleOutcome:
        rd, name = self.results_dir, self.name
        try:
            results.abandon_attempt(rd, name, keep_logs=environment is not None)
        except Exception:
            log.exception("%s: the unfinished attempt could not be cleared away", name)
        earlier_kept, _when = self._earlier_kept()
        if environment is None:
            note, next_step = messages.stopped_note(earlier_kept), ""
        else:
            note, next_step = messages.stopped_by_environment(environment, earlier_kept), environment.next_step
            self.trace.environment = environment.environment
            self.trace.environment_path = environment.path
            self.trace.explained = environment
        outcome = SampleOutcome(
            name=name, status=RunStatus.STOPPED, note=note, next_step=next_step,
            trimmed=bool(self.sample.trim), seconds=_monotonic() - self.began,
            finished_at=fsutil.now_iso(), folder=results.sample_dir(rd, name),
            kept_earlier_result=earlier_kept)
        try:                                             # undoes a swap that was under way; writes nothing
            results.write_status(rd, outcome, reads=[], settings_fingerprint="", settings_text="",
                                 trimmed=bool(self.sample.trim))
        except Exception:
            log.exception("%s: an unfinished swap could not be undone", name)
        return outcome
