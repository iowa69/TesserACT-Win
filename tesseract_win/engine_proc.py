# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Start one engine, follow what it prints, stop it for certain, and say how it ended.

An engine never gets a pipe. Both of its output streams go straight into a log file, as
the bash ``tesseract`` command does, and this module follows that file through a second
handle. An unread pipe fills after a few kilobytes and the engine then blocks for ever; a
file cannot do that, and it survives a crash of the window.

Everything returned is data: exit codes, :class:`~tesseract_win.osapi.ExitKind` values,
numbers, the engine's own last lines. The sentences for the screen are written elsewhere.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, List, Mapping, Optional, Sequence

from . import osapi
from .osapi import ExitKind, ProcessGroup

__all__ = [
    "ExitKind", "EngineStartError", "EngineResult", "Progress",
    "ENGINE_ASSEMBLER", "ENGINE_TRIMMER", "ASSEMBLER_STEPS", "ASSEMBLER_GRAPH_STEP",
    "ASSEMBLER_GRAPH_DETAIL", "TAIL_LINES",
    "engine_version", "run_engine", "parse_progress", "last_error_line",
]

log = logging.getLogger(__name__)

# Replaced in the tests to imitate a PC that slept.
_monotonic = time.monotonic
_wall = time.time

#: Lines of engine output kept for :attr:`EngineResult.log_tail`.
TAIL_LINES = 40
_TAIL_LINE_CHARS = 2000
_READ_CHUNK = 1 << 16
_MAX_LINE_BYTES = 1 << 16            # a "line" longer than this is handed out in pieces
# An engine gone wrong can print faster than anyone can read. The follower must never be
# busy with that for longer than a moment, or Stop, the hung check and the "working" line
# would all wait for it. So: a limited amount is read per round, and when the engine is
# far ahead the follower jumps to the newest part. The log file holds every byte anyway.
_ROUND_BYTES = 1 << 20               # at most this much of the log is read in one round
_MAX_BEHIND = 8 << 20                # further behind than this: jump ...
_KEEP_BEHIND = 1 << 18               # ... to this far before the end of the log
_WORK_INTERVAL = 1.0                 # seconds between two on_working calls
_JUMP_SECONDS = 20.0                 # a longer gap between two rounds means the PC slept
_KILL_WAIT = 10.0                    # seconds to wait for an engine that was told to end
_VERSION_TIMEOUT = 60.0              # the first start can be slow while antivirus scans the file


# ============================================================================ results

class EngineStartError(Exception):
    """An engine could not be started, or did not answer. ``kind`` says why, as an ExitKind.

    ``returncode`` is set when the engine started but ended badly; ``os_error`` when the
    system refused to start it (its ``winerror`` / ``errno`` are the raw facts).
    """

    def __init__(self, kind: ExitKind, exe: str, detail: str = "", *,
                 returncode: Optional[int] = None, os_error: Optional[BaseException] = None) -> None:
        super().__init__("%s: %s%s" % (exe, kind.value, (" (%s)" % detail) if detail else ""))
        self.kind = kind
        self.exe = exe
        self.detail = detail
        self.returncode = returncode
        self.os_error = os_error


@dataclass
class EngineResult:
    """How one engine run ended.

    ``returncode`` is None when the engine could not be started (``kind`` is then
    COULD_NOT_START, BLOCKED or OUT_OF_MEMORY and ``start_error`` holds the ``OSError``).
    ``stopped``: this program ended it on request; ``kind`` is STOPPED. (Kind STOPPED
    with ``stopped`` False means it was ended from outside: Windows shutting down or
    logging off, or somebody ending the process.)
    ``hung``: it used no processor time for ``idle_timeout`` seconds and was ended;
    ``kind`` is CRASH, so check ``hung`` first to tell the two apart.
    ``seconds``: how long it ran. ``peak_memory_bytes``: the most memory it held.
    ``log_tail``: the last lines it printed (at most 40), joined with newlines.
    """
    returncode: Optional[int]
    kind: ExitKind
    stopped: bool
    hung: bool
    seconds: float
    peak_memory_bytes: int
    log_tail: str
    start_error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.kind is ExitKind.OK


_ERROR_LINE = re.compile(r"^\s*error:\s*\S", re.IGNORECASE)


def last_error_line(log_tail: str) -> str:
    """The engine's own last ``error: ...`` line in ``log_tail`` (stripped), or ""."""
    for line in reversed((log_tail or "").splitlines()):
        if _ERROR_LINE.match(line):
            return line.strip()
    return ""


# ============================================================================ progress

ENGINE_ASSEMBLER = "tesseract-asm"
ENGINE_TRIMMER = "scepter"

#: tesseract-asm 1.6.2 numbers its banners "[1/7]" to "[7/7]" (``src/assembler.cpp``).
ASSEMBLER_STEPS = 7
#: The step whose lines carry a k-mer size: "[3/7] de Bruijn iterations".
ASSEMBLER_GRAPH_STEP = 3
ASSEMBLER_GRAPH_DETAIL = "de Bruijn iterations"


@dataclass(frozen=True)
class Progress:
    """Where an engine is, read from one line of its output.

    tesseract-asm: ``step`` of ``total_steps`` and ``detail``, the engine's own name for
    the step ("loading reads", "de Bruijn iterations", ...). On the lines the graph step
    prints for each k-mer size, ``k`` is that size and the rest repeats the step, so
    equal records mean "nothing new". ``fraction`` is None: the engine gives no honest
    figure.

    scepter: ``fraction`` is the share of the input read so far (0..1); ``detail`` is
    the rest of the line ("466.94K reads 92.25K reads/s"); ``step`` and ``total_steps``
    are None.
    """
    step: Optional[int] = None
    total_steps: Optional[int] = None
    detail: str = ""
    fraction: Optional[float] = None
    k: Optional[int] = None


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# "[3/7] de Bruijn iterations", "[2/7] read error correction (k=21, trust cutoff auto, ...)"
_ASM_STEP = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S.*?)\s*$")
_ASM_STEP_EXTRA = re.compile(r"\s*\(.*\)$")
# "  k=55  count 0.5s  distinct=...", "  k=127 graph 0.3s ..." (format "  k=%-3d ...")
_ASM_K = re.compile(r"^  k=(\d+)\s+(\S.*)$")
# "  [===========             ]  47.5%  466.94K reads  92.25K reads/s"
_SCEPTER_PERCENT = re.compile(r"^\s*\[[= ]*\]\s*(\d{1,3}(?:\.\d+)?)%(.*)$")
# the same line when the size of the input is not known: "  466.94K reads  92.25K reads/s"
_SCEPTER_COUNT = re.compile(r"^\s*\S+ reads\s+\S+ reads/s\s*$")


def _engine_kind(engine: str) -> str:
    name = str(engine).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if "scepter" in name:
        return ENGINE_TRIMMER
    if "tesseract" in name:
        return ENGINE_ASSEMBLER
    return ""


def parse_progress(engine: str, line: str) -> Optional[Progress]:
    """Read the progress out of one output line; None for every other line.

    ``engine`` is ``"tesseract-asm"`` or ``"scepter"`` (a path to the program is fine too).
    """
    kind = _engine_kind(engine)
    text = _ANSI.sub("", line or "")
    if kind == ENGINE_ASSEMBLER:
        match = _ASM_STEP.match(text)
        if match:
            return Progress(step=int(match.group(1)), total_steps=int(match.group(2)),
                            detail=_ASM_STEP_EXTRA.sub("", match.group(3)))
        match = _ASM_K.match(text)
        if match and not match.group(2).startswith("skipped"):
            return Progress(step=ASSEMBLER_GRAPH_STEP, total_steps=ASSEMBLER_STEPS,
                            detail=ASSEMBLER_GRAPH_DETAIL, k=int(match.group(1)))
        return None
    if kind == ENGINE_TRIMMER:
        match = _SCEPTER_PERCENT.match(text)
        if match:
            fraction = min(1.0, max(0.0, float(match.group(1)) / 100.0))
            return Progress(detail=" ".join(match.group(2).split()), fraction=fraction)
        if _SCEPTER_COUNT.match(text):
            return Progress(detail=" ".join(text.split()))
        return None
    return None


# ============================================================================ version

def _decode(data: Optional[bytes]) -> str:
    return (data or b"").decode("utf-8", "replace")


def _first_line(text: str) -> str:
    for line in text.replace("\r", "\n").split("\n"):
        if line.strip():
            return line.strip()
    return ""


def engine_version(exe: str, *, timeout: float = _VERSION_TIMEOUT) -> str:
    """The first line an engine prints for ``--version``, e.g. ``TesserACT 1.6.2``.

    Raises :class:`EngineStartError` when the engine cannot be started (missing, blocked,
    a part it needs is missing), ends with an error, prints nothing, or does not answer
    within ``timeout`` seconds. The engine gets none of the user's ``TESSERACT_*``
    variables: it checks them and a stray one is a hard error.
    """
    exe = os.fspath(exe)
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("TESSERACT_")}
    environment["NO_COLOR"] = "1"
    try:
        done = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=environment, timeout=timeout,
                              shell=False, **osapi.no_window_kwargs())
    except subprocess.TimeoutExpired as error:
        raise EngineStartError(ExitKind.COULD_NOT_START, exe,
                               "no answer within %.0f seconds" % timeout) from error
    except (OSError, ValueError) as error:
        raise EngineStartError(osapi.classify_start_error(error), exe, str(error),
                               os_error=error) from error
    output, errors = _decode(done.stdout), _decode(done.stderr)
    if done.returncode != 0:
        raise EngineStartError(osapi.classify_exit(done.returncode, errors + "\n" + output), exe,
                               _first_line(errors) or _first_line(output), returncode=done.returncode)
    first = _first_line(output) or _first_line(errors)
    if not first:
        raise EngineStartError(ExitKind.ENGINE_ERROR, exe, "no version text", returncode=0)
    return first


# ============================================================================ following the log

_LINE_END = re.compile(rb"[\r\n]")
_CR, _LF = 0x0D, 0x0A


def _whole_characters(data: bytes) -> bytes:
    """``data`` without a UTF-8 character that is cut off at its very end."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:                          # a continuation byte: its first byte is further back
            continue
        if byte >= 0xC0:                                 # a first byte: are all of its bytes there?
            needed = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            if needed > back:
                return data[:-back]
        break
    return data


class _LineSplitter:
    """Cuts a stream of bytes into lines.

    A line ends at a newline, at a carriage return, or at the pair of them (one end, not
    two). Progress lines are rewritten in place with carriage returns; each rewrite is a
    line. The unfinished last line is kept for the next call. Bytes that are not UTF-8
    become the replacement character.

    A line that is being rewritten in place is the one that never gets its ending until
    the next rewrite arrives, so the newest progress would always be one step behind.
    :meth:`idle`, called when a round brought nothing new, therefore hands out such a line
    early, once; it is not handed out again when its ending arrives.
    """

    def __init__(self) -> None:
        self._pending = bytearray()
        self._scanned = 0            # bytes of _pending already searched for a line end
        self._after_cr = False       # the unfinished line began right after a bare carriage return
        self._shown = -1             # how many bytes of the unfinished line idle() has handed out
        self._late_lf = False        # idle() settled a carriage return; its newline may still come
        self._late_blank: Optional[bytes] = None   # ...and the blank line that it would then end

    def _emit(self, out: List[str], line: bytes, ended_by_cr: bool) -> None:
        shown, self._shown = self._shown, -1
        self._after_cr = ended_by_cr
        if shown == len(line):
            return                   # handed out early already, and nothing was added since
        if ended_by_cr and not line.strip():
            return                   # a carriage return on an empty or blanked line moves the cursor only
        out.append(line.decode("utf-8", "replace"))

    def feed(self, data: bytes) -> List[str]:
        """Add new bytes; returns the lines they complete."""
        out: List[str] = []
        if self._late_lf and data:
            self._late_lf = False
            blank, self._late_blank = self._late_blank, None
            if data[:1] == b"\n":                        # it was the pair after all: one line end, not two
                data = data[1:]
                self._after_cr = False
                if blank is not None:                    # and so the blank line was a line, not a rewrite
                    out.append(blank.decode("utf-8", "replace"))
        if not data:
            return out
        pending = self._pending
        pending += data
        size = len(pending)
        start, search_from = 0, self._scanned
        while True:
            match = _LINE_END.search(pending, search_from)
            if match is None:
                break
            end = match.start()
            if pending[end] == _LF:
                self._emit(out, bytes(pending[start:end]), False)
                start = end + 1
            elif end + 1 >= size:
                break                # a carriage return at the very end: the newline may still follow
            elif pending[end + 1] == _LF:
                self._emit(out, bytes(pending[start:end]), False)
                start = end + 2
            else:
                self._emit(out, bytes(pending[start:end]), True)
                start = end + 1
            search_from = start
        del pending[:start]
        while len(pending) > _MAX_LINE_BYTES:           # no line end for a very long time
            self._emit(out, bytes(pending[:_MAX_LINE_BYTES]), False)
            del pending[:_MAX_LINE_BYTES]
        self._scanned = len(pending) - 1 if pending.endswith(b"\r") else len(pending)
        return out

    def idle(self) -> List[str]:
        """Nothing new arrived this round: settle what was waiting for more."""
        out: List[str] = []
        pending = self._pending
        if pending.endswith(b"\r"):                      # no newline followed: it was a rewrite
            line = bytes(pending[:-1])
            del pending[:]
            self._scanned = 0
            self._emit(out, line, True)
            self._late_lf = True
            self._late_blank = None if line.strip() else line
        elif pending and self._after_cr:
            whole = _whole_characters(bytes(pending))    # never half a character
            if len(whole) != self._shown and whole.strip():
                self._shown = len(whole)
                out.append(whole.decode("utf-8", "replace"))
        return out

    def finish(self) -> List[str]:
        """The stream has ended: hand out what is left."""
        out: List[str] = []
        pending = self._pending
        if pending:
            ended_by_cr = pending.endswith(b"\r")
            line = bytes(pending[:-1] if ended_by_cr else pending)
            del pending[:]
            self._scanned = 0
            if line or ended_by_cr:
                self._emit(out, line, ended_by_cr)
        return out


class _Follower:
    """Reads what an engine appends to its log file and hands it on line by line."""

    def __init__(self, path: str, offset: int, on_line: Optional[Callable[[str], Any]]) -> None:
        self._path = path
        self._offset = offset
        self._on_line = on_line
        self._reader: Any = None
        self._splitter = _LineSplitter()
        self._complained = False
        self._after_jump = False     # the next line handed out begins in the middle: leave it out
        #: Bytes of the log that were never handed to ``on_line`` because the engine printed too fast.
        self.skipped_bytes = 0
        self.tail: Deque[str] = collections.deque(maxlen=TAIL_LINES)

    def _open(self) -> None:
        try:
            reader = open(self._path, "rb", buffering=0)
            reader.seek(self._offset)
            self._reader = reader
        except OSError as error:
            if not self._complained:
                self._complained = True
                log.warning("cannot follow the log %s yet: %s", self._path, error)

    def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass

    def _deliver(self, lines: List[str]) -> None:
        for line in lines:
            if line.strip():
                self.tail.append(line[:_TAIL_LINE_CHARS])
            if self._on_line is not None:
                self._on_line(line)

    def _jump_if_far_behind(self) -> None:
        """Leave out the middle of a flood of output, so that a round stays short."""
        try:
            end = self._reader.seek(0, os.SEEK_END)
            behind = end - self._offset
            target = end - _KEEP_BEHIND if behind > _MAX_BEHIND else self._offset
            self._reader.seek(target)
        except OSError as error:                         # open again next round, where we were
            log.debug("looking at the log %s failed: %s", self._path, error)
            self.close()
            return
        if target != self._offset:
            if not self.skipped_bytes:
                log.warning("the engine prints faster than its output can be followed; part of it is "
                            "left out here (all of it is in %s)", self._path)
            self.skipped_bytes += target - self._offset
            self._offset = target
            self._splitter = _LineSplitter()
            self._after_jump = True

    def pump(self) -> bool:
        """Read what is new, at most ``_ROUND_BYTES`` of it. True when something was."""
        if self._reader is None:
            self._open()
        if self._reader is not None:
            self._jump_if_far_behind()
        got = False
        room = _ROUND_BYTES
        while self._reader is not None and room > 0:
            wanted = min(_READ_CHUNK, room)
            try:
                chunk = self._reader.read(wanted)
            except OSError as error:                     # a drive that stutters: open again next round
                log.debug("reading the log %s failed: %s", self._path, error)
                self.close()
                break
            if not chunk:
                break
            got = True
            room -= len(chunk)
            self._offset += len(chunk)
            lines = self._splitter.feed(chunk)
            if self._after_jump and lines:               # the rest of a line whose beginning was left out
                self._after_jump = False
                del lines[0]
            self._deliver(lines)
            if len(chunk) < wanted:
                break
        if not got and not self._after_jump:
            self._deliver(self._splitter.idle())
        return got

    def finish(self) -> None:
        """The engine has ended: read to the end of the file and hand out the last line."""
        while self.pump():
            pass
        self._deliver(self._splitter.finish())
        self.close()


def _wait(process: Any, seconds: float) -> Optional[int]:
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None


# ============================================================================ running

def run_engine(argv: Sequence[str], *, cwd: Optional[str], env: Optional[Mapping[str, str]],
               log_path: str, group: ProcessGroup, stop_event: threading.Event,
               on_line: Optional[Callable[[str], Any]] = None,
               on_working: Optional[Callable[[float, int], Any]] = None,
               low_priority: bool = True, idle_timeout: float = 1800.0,
               poll: float = 0.25) -> EngineResult:
    """Run one engine to its end and report how it ended.

    ``argv`` is a list (never a shell command line); the engine's input is empty; both of
    its output streams are appended, byte for byte, to ``log_path`` (give a full path: a
    relative one is taken from this program's folder, not from ``cwd``). ``cwd`` may be a
    network path. The engine is started inside ``group``, so it cannot outlive this
    program. Call this from the thread that will wait for the result.

    While the engine runs, every ``poll`` seconds:

    * what is new in the log is read and ``on_line(text)`` is called for each complete
      line. A line ends at a newline or a carriage return; text is UTF-8, bad bytes
      become the replacement character; an unfinished line waits for its ending. (A
      progress line that is rewritten in place is handed out as soon as it has stood
      still for one round.)
    * about once a second ``on_working(busy_threads, memory_bytes)`` is called:
      ``busy_threads`` is the processor time the group used divided by the time that
      passed (8 threads fully busy give about 8.0).
    * if ``stop_event`` is set, the whole group is ended, and the result has
      ``stopped=True`` and kind STOPPED. (An engine that had just finished with exit
      code 0 when the stop arrived is reported as finished, not as stopped.)
    * if the engine has used no processor time at all and printed nothing for
      ``idle_timeout`` seconds, it is ended and the result has ``hung=True`` (kind CRASH).
      Time during which the PC slept does not count: a jump of the clock restarts the
      count. ``idle_timeout=0`` switches this off.

    An engine that cannot be started gives a result with ``returncode=None`` and the kind
    from :func:`osapi.classify_start_error`; nothing is raised for it. ``OSError`` is
    raised only when the log file itself cannot be opened for writing. Both callbacks run
    in the calling thread; if one raises, the engine is ended and the exception goes on.
    """
    command = [os.fspath(part) for part in argv]
    poll = max(0.01, float(poll))
    began = _monotonic()
    if not command:
        error: BaseException = ValueError("no program was named")
        log.warning("could not start an engine: %s", error)
        return EngineResult(None, osapi.classify_start_error(error), False, False, 0.0, 0, "",
                            start_error=error)

    process: Any = None
    follower: Optional[_Follower] = None
    stopped = hung = False
    returncode: Optional[int] = None
    peak = 0
    # From the moment the engine exists, whatever goes wrong below must not leave it running.
    try:
        with open(log_path, "ab") as log_file:
            offset = log_file.seek(0, os.SEEK_END)
            try:
                process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log_file, stderr=subprocess.STDOUT, shell=False,
                                           **group.popen_kwargs(low_priority))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                log.warning("could not start %s: %s", command[0], error)
                return EngineResult(None, osapi.classify_start_error(error), False, False,
                                    _monotonic() - began, 0, "", start_error=error)
            group.add(process)                           # at once: until then nothing ties it to us
            kills_before = group.kill_count              # a kill of the group from here on includes it
        # Our own handle to the log is closed here; the engine has its copy.

        follower = _Follower(log_path, offset, on_line)
        idle_limit = float(idle_timeout) if idle_timeout and idle_timeout > 0 else 0.0
        sample_every = min(_WORK_INTERVAL, max(poll, idle_limit / 4.0)) if idle_limit else _WORK_INTERVAL
        jump = max(_JUMP_SECONDS, 4.0 * poll)
        previous, previous_wall = _monotonic(), _wall()
        sampled_at = worked_at = previous
        sampled_cpu = worked_cpu = group.cpu_seconds()
        idle = 0.0
        while True:
            returncode = process.poll()
            active = follower.pump()
            if returncode is not None:
                break
            now, wall = _monotonic(), _wall()
            step, wall_step = now - previous, wall - previous_wall
            previous, previous_wall = now, wall
            if stop_event.is_set():
                stopped = True
                break
            if max(step, wall_step) > jump:
                # The PC slept, or this program stood still. That silence says nothing about
                # the engine: count again from here.
                log.info("the clock jumped by %.0f s while an engine was running", max(step, wall_step))
                idle = 0.0
                sampled_at = worked_at = now
                sampled_cpu = worked_cpu = group.cpu_seconds()
            else:
                idle = 0.0 if active else idle + max(0.0, step)
                if now - sampled_at >= sample_every:
                    cpu, memory = group.cpu_seconds(), group.memory_bytes()
                    peak = max(peak, memory)
                    if cpu is None or sampled_cpu is None or cpu > sampled_cpu:
                        idle = 0.0                       # it works (or we cannot tell: never guess "hung")
                    sampled_at, sampled_cpu = now, cpu
                    if on_working is not None and now - worked_at >= _WORK_INTERVAL * 0.95:
                        busy = 0.0
                        if cpu is not None and worked_cpu is not None:
                            busy = max(0.0, (cpu - worked_cpu) / (now - worked_at))
                        on_working(busy, memory)
                        worked_at, worked_cpu = now, cpu
                if idle_limit and idle >= idle_limit:
                    hung = True
                    break
            stop_event.wait(poll)

        if stopped or hung:
            group.kill()
            returncode = _wait(process, _KILL_WAIT)
            if returncode is None:                       # the group could not end it: try it directly
                try:
                    process.kill()
                except OSError:
                    pass
                returncode = _wait(process, _KILL_WAIT)
            if returncode == 0:
                stopped = hung = False                   # it had finished by itself a moment before
        follower.finish()
    finally:
        if follower is not None:
            follower.close()
        if process is not None and process.poll() is None:
            # Reached when a callback raised, on Ctrl+C, or when the engine would not end.
            log.error("ending an engine that was still running (pid %d)", process.pid)
            group.kill()
            try:
                process.kill()
            except OSError:
                pass
            _wait(process, _KILL_WAIT)

    seconds = _monotonic() - began
    tail = "\n".join(follower.tail)
    peak = max(peak, group.peak_memory_bytes(process))
    if not stopped and not hung and returncode != 0 and (group.kill_count != kills_before or group.closed):
        stopped = True                                   # the group was ended by someone else meanwhile
    if stopped:
        kind = ExitKind.STOPPED
    elif hung:
        kind = ExitKind.CRASH
    else:
        kind = osapi.classify_exit(returncode, tail)
    return EngineResult(returncode, kind, stopped, hung, seconds, peak, tail)
