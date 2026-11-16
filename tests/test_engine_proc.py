# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""engine_proc: starting, following, stopping and explaining one engine.

The engines here are stand-ins: small Python scripts started through ``sys.executable``.
Two tests at the end use the real engines on a 60 kb genome. Everything that does not
need POSIX signals or permissions also runs on Windows, where only CI can run it.
"""

from __future__ import annotations

import ctypes
import os
import random
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from tesseract_win import appdirs, engine_proc, osapi, versions
from tesseract_win.engine_proc import (EngineResult, EngineStartError, ExitKind, Progress,
                                       _LineSplitter, last_error_line, parse_progress, run_engine)

WINDOWS = os.name == "nt"
posix_only = pytest.mark.skipif(WINDOWS, reason="needs POSIX signals, permissions or script files")
windows_only = pytest.mark.skipif(not WINDOWS, reason="the exit codes and errors of Windows")
#: The exit code of a process that this program ended itself.
ENDED_BY_US = osapi.STOP_EXIT_CODE if WINDOWS else -9
if WINDOWS:
    osapi.set_process_defaults()        # the stand-ins that crash on purpose must not raise a dialog

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPSTREAM_ASSEMBLER = os.path.join(REPO, "engines", "tesseract", "upstream", "src", "assembler.cpp")
UPSTREAM_SCEPTER = os.path.join(REPO, "engines", "scepter", "upstream", "src", "processor.cpp")
GOLDEN_LOG = os.path.join(REPO, "tests", "parity", "golden", "adapter", "run", "engine_out", "assembly.log")


# ------------------------------------------------------------------ helpers

#: First lines of a stand-in that writes exact bytes: no newline translation on Windows.
RAW_OUTPUT = """\
import os
if os.name == "nt":
    import msvcrt
    msvcrt.setmode(1, os.O_BINARY)
    msvcrt.setmode(2, os.O_BINARY)
"""


def standin(tmp_path, body, name="standin.py", raw=False):
    """Write a stand-in engine and return the argv that starts it."""
    path = tmp_path / name
    path.write_text((RAW_OUTPUT if raw else "") + textwrap.dedent(body), encoding="utf-8")
    return [PY, str(path)]


def alive(pid):
    """True while the process exists and has not ended (a zombie has ended)."""
    if WINDOWS:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, 0, pid)
        if not handle:
            return False
        code = ctypes.c_uint32(0)
        answered = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(answered) and code.value == 259          # STILL_ACTIVE
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    return data[data.rfind(b")") + 2:].split()[0] != b"Z"


def gone(pid, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return not alive(pid)


@pytest.fixture
def group():
    made = osapi.ProcessGroup()
    yield made
    made.close()


def run(argv, tmp_path, group, **options):
    settings = dict(cwd=str(tmp_path), env=None, log_path=str(tmp_path / "engine.log"), group=group,
                    stop_event=threading.Event(), poll=0.05)
    settings.update(options)
    return run_engine(argv, **settings)


# ------------------------------------------------------------------ the line splitter

class TestLineSplitter:
    def test_newlines_and_the_unfinished_last_line(self):
        splitter = _LineSplitter()
        assert splitter.feed(b"one\ntwo\r\nthr") == ["one", "two"]
        assert splitter.feed(b"ee") == []
        assert splitter.idle() == []                      # an ordinary partial line waits
        assert splitter.feed(b"\nfour") == ["three"]
        assert splitter.finish() == ["four"]
        assert splitter.finish() == []

    def test_blank_lines_are_kept(self):
        splitter = _LineSplitter()
        assert splitter.feed(b"a\n\nb\r\n\r\nc\n") == ["a", "", "b", "", "c"]

    def test_progress_rewritten_in_place(self):
        splitter = _LineSplitter()
        assert splitter.feed(b"\r 10% \r 20% ") == [" 10% "]
        assert splitter.idle() == [" 20% "]               # handed out early: it is the current state
        assert splitter.idle() == []                      # ...once
        assert splitter.feed(b"\r 30% ") == []            # the ending of " 20% " brings nothing new
        assert splitter.idle() == [" 30% "]
        assert splitter.feed(b"\r" + b" " * 78 + b"\r") == []     # the engine blanks the line
        assert splitter.idle() == []
        assert splitter.feed(b"done\n") == ["done"]
        assert splitter.finish() == []

    def test_early_line_is_given_again_when_it_grew(self):
        splitter = _LineSplitter()
        assert splitter.feed(b"x\r 47") == ["x"]
        assert splitter.idle() == [" 47"]
        assert splitter.feed(b".5%\r") == []
        assert splitter.idle() == [" 47.5%"]

    def test_carriage_return_and_newline_split_between_two_reads(self):
        splitter = _LineSplitter()
        assert splitter.feed(b"abc\r") == []              # the newline may still follow
        assert splitter.feed(b"\ndef\n") == ["abc", "def"]

    def test_carriage_return_newline_with_a_quiet_round_in_between(self):
        splitter = _LineSplitter()
        assert splitter.feed(b"abc\r") == []
        assert splitter.idle() == ["abc"]
        assert splitter.feed(b"\npartial") == []          # no blank line from the late newline
        assert splitter.idle() == []                      # and "partial" is an ordinary partial line
        assert splitter.feed(b" line\n") == ["partial line"]

    def test_bytes_that_are_not_utf8(self):
        splitter = _LineSplitter()
        assert splitter.feed(b"caf\xe9 \xff\xfe ok\n") == ["caf\ufffd \ufffd\ufffd ok"]

    def test_a_character_cut_in_two_by_the_read(self):
        splitter = _LineSplitter()
        assert splitter.feed("caf".encode() + b"\xc3") == []
        assert splitter.feed(b"\xa9 \xe6\x97") == []
        assert splitter.feed(b"\xa5\n") == ["caf\u00e9 \u65e5"]

    def test_a_line_without_end_is_handed_out_in_pieces(self):
        splitter = _LineSplitter()
        size = engine_proc._MAX_LINE_BYTES
        lines = splitter.feed(b"x" * (2 * size + 10))
        assert [len(line) for line in lines] == [size, size]
        assert splitter.finish() == ["x" * 10]

    def test_any_cutting_of_the_stream_gives_the_same_lines(self):
        rng = random.Random(4)
        alphabet = [b"\r", b"\n", b"\r\n", b"a", b"bc", b" ", b"\xc3\xa9", b"\xff", b"50%"]
        for _ in range(200):
            data = b"".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            whole = _LineSplitter()
            expected = whole.feed(data) + whole.finish()
            pieces = _LineSplitter()
            got = []
            position = 0
            while position < len(data):
                step = rng.randint(1, 5)
                got += pieces.feed(data[position:position + step])
                position += step
            got += pieces.finish()
            assert got == expected, data

    def test_nothing_is_lost_or_invented(self):
        rng = random.Random(9)
        for _ in range(100):
            words = ["w%d" % rng.randint(0, 99) for _ in range(rng.randint(1, 30))]
            data = b"".join(word.encode() + rng.choice([b"\n", b"\r\n"]) for word in words)
            splitter = _LineSplitter()
            got = []
            for start in range(0, len(data), 7):
                got += splitter.feed(data[start:start + 7])
                if rng.random() < 0.3:
                    got += splitter.idle()
            got += splitter.finish()
            assert got == words, data


# ------------------------------------------------------------------ following the output

def test_follows_carriage_returns_and_partial_lines(tmp_path, group):
    argv = standin(tmp_path, r"""
        import time
        def w(data):
            os.write(2, data)
        w(b"first li"); time.sleep(0.5)
        w(b"ne\n\r 10% "); time.sleep(1.0)
        w(b"\r 55% "); time.sleep(0.4)
        w(b"\r100% "); time.sleep(0.4)
        w(b"\r" + b" " * 20 + b"\r")
        os.write(1, b"caf\xc3\xa9 r\xe9sum\xe9 \xff\n")
        os.write(1, b"crlf line\r\n")
        os.write(1, b"no newline at the end")
    """, raw=True)
    seen = []
    result = run(argv, tmp_path, group, on_line=lambda line: seen.append((time.monotonic(), line)))
    lines = [line for _, line in seen]
    assert lines == ["first line", " 10% ", " 55% ", "100% ", "caf\u00e9 r\ufffdsum\ufffd \ufffd",
                     "crlf line", "no newline at the end"]
    assert result.kind is ExitKind.OK and result.returncode == 0
    assert result.log_tail.splitlines() == lines
    when = dict((line, moment) for moment, line in seen)
    # " 10% " stood for a second before the engine rewrote it: it must not wait for the rewrite.
    assert when[" 10% "] - when["first line"] < 0.6


def test_the_log_file_holds_every_byte(tmp_path, group):
    rng = random.Random(1)
    payload = bytes(rng.getrandbits(8) for _ in range(150_000)) + b"\r\nend \xff\xfe\x00 of data"
    (tmp_path / "payload.bin").write_bytes(payload)
    log_path = tmp_path / "engine.log"
    log_path.write_bytes(b"written before the engine started\n")
    argv = standin(tmp_path, r"""
        import sys, time
        data = open(sys.argv[1], "rb").read()
        for start in range(0, len(data), 4096):
            os.write(1 if (start // 4096) % 2 else 2, data[start:start + 4096])   # both streams, in turn
            if start % 40960 == 0:
                time.sleep(0.01)
    """, raw=True) + [str(tmp_path / "payload.bin")]
    lines = []
    result = run(argv, tmp_path, group, on_line=lines.append)
    assert result.kind is ExitKind.OK
    assert log_path.read_bytes() == b"written before the engine started\n" + payload
    assert lines and lines[-1] == "end \ufffd\ufffd\x00 of data"
    assert "written before the engine started" not in lines       # only this run is followed


def test_a_second_run_appends_and_follows_only_itself(tmp_path, group):
    argv = standin(tmp_path, "import sys; print('run', sys.argv[1])")
    first, second = [], []
    run(argv + ["1"], tmp_path, group, on_line=first.append)
    result = run(argv + ["2"], tmp_path, group, on_line=second.append)
    assert (first, second) == (["run 1"], ["run 2"])
    assert result.log_tail == "run 2"
    assert (tmp_path / "engine.log").read_bytes().replace(b"\r\n", b"\n") == b"run 1\nrun 2\n"


def test_input_is_empty_and_cwd_env_and_odd_names_arrive(tmp_path, group):
    folder = tmp_path / "dir with spaces \u00e9 [1] \u65e5\u672c"
    folder.mkdir()
    argv = standin(tmp_path, r"""
        import os, sys
        sys.stdout.reconfigure(encoding="utf-8")
        print("stdin:%r" % sys.stdin.read())
        print("cwd:" + os.path.normcase(os.path.realpath(os.getcwd())))
        print("env:" + os.environ.get("STANDIN_VALUE", "missing"))
        print("arg:" + sys.argv[1])
    """) + ["\u00e7a va [x] *.fastq"]
    lines = []
    environment = dict(os.environ, STANDIN_VALUE="\u00e9t\u00e9")
    result = run(argv, tmp_path, group, cwd=str(folder), env=environment, on_line=lines.append,
                 log_path=str(folder / "engine.log"))
    assert result.kind is ExitKind.OK
    assert lines == ["stdin:''", "cwd:" + os.path.normcase(os.path.realpath(str(folder))),
                     "env:\u00e9t\u00e9", "arg:\u00e7a va [x] *.fastq"]


def test_run_engine_uses_no_shell_and_no_pipes(tmp_path, group, monkeypatch):
    seen = {}
    real_popen = subprocess.Popen

    def spy(command, **options):
        seen.update(options, command=command)
        return real_popen(command, **options)

    monkeypatch.setattr(engine_proc.subprocess, "Popen", spy)
    result = run(standin(tmp_path, "print('ok')"), tmp_path, group)
    assert result.kind is ExitKind.OK
    assert isinstance(seen["command"], list)
    assert seen["shell"] is False
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["stderr"] == subprocess.STDOUT
    assert seen["stdout"] not in (subprocess.PIPE, None) and hasattr(seen["stdout"], "fileno")
    if WINDOWS:
        assert seen["creationflags"] == 0x08000000 | 0x00004000      # no window, below normal
    else:
        assert seen["start_new_session"] is True


# ------------------------------------------------------------------ stopping

CHILD_WITH_CHILD = r"""
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    print("pids %d %d" % (__import__("os").getpid(), child.pid), flush=True)
    time.sleep(300)
"""


def test_stop_ends_the_engine_and_its_own_child_within_two_seconds(tmp_path, group):
    stop = threading.Event()
    seen = {}

    def on_line(line):
        match = re.match(r"pids (\d+) (\d+)", line)
        if match:
            seen["pids"] = (int(match.group(1)), int(match.group(2)))
            assert alive(seen["pids"][0]) and alive(seen["pids"][1])
            seen["asked"] = time.monotonic()
            stop.set()

    result = run(standin(tmp_path, CHILD_WITH_CHILD), tmp_path, group, stop_event=stop, on_line=on_line)
    took = time.monotonic() - seen["asked"]
    assert took < 2.0
    assert result.stopped is True and result.hung is False
    assert result.kind is ExitKind.STOPPED
    assert result.returncode == ENDED_BY_US
    engine, child = seen["pids"]
    assert gone(engine) and gone(child)
    time.sleep(0.1)
    assert group.memory_bytes() == 0


def test_stop_set_before_the_start_still_ends_at_once(tmp_path, group):
    stop = threading.Event()
    stop.set()
    began = time.monotonic()
    result = run(standin(tmp_path, "import time; time.sleep(300)"), tmp_path, group, stop_event=stop)
    assert time.monotonic() - began < 2.0
    assert result.stopped and result.kind is ExitKind.STOPPED


def test_an_engine_that_had_just_finished_is_not_called_stopped(tmp_path, group):
    stop = threading.Event()

    def on_line(line):                                    # the stop arrives when the engine is over,
        assert gone(int(line), timeout=10.0)              # but before run_engine has noticed that
        stop.set()

    result = run(standin(tmp_path, "import os; print(os.getpid())"), tmp_path, group, stop_event=stop,
                 on_line=on_line)
    assert result.kind is ExitKind.OK and result.stopped is False and result.returncode == 0


def test_a_stop_during_the_last_line_gives_one_of_the_two_honest_answers(tmp_path, group):
    stop = threading.Event()
    result = run(standin(tmp_path, "print('bye')"), tmp_path, group, stop_event=stop,
                 on_line=lambda line: stop.set())
    if result.stopped:
        assert (result.kind, result.returncode) == (ExitKind.STOPPED, ENDED_BY_US)
    else:
        assert (result.kind, result.returncode) == (ExitKind.OK, 0)


def test_a_kill_of_the_group_from_elsewhere_counts_as_stopped(tmp_path, group):
    timer = threading.Timer(0.6, group.kill)
    timer.start()
    try:
        result = run(standin(tmp_path, "import time; time.sleep(300)"), tmp_path, group)
    finally:
        timer.cancel()
    assert result.stopped is True and result.kind is ExitKind.STOPPED


def test_an_engine_started_in_a_closed_group_is_ended_and_counts_as_stopped(tmp_path):
    closed = osapi.ProcessGroup()
    closed.close()
    began = time.monotonic()
    result = run(standin(tmp_path, "import time; time.sleep(300)"), tmp_path, closed)
    assert time.monotonic() - began < 3.0
    assert result.stopped is True and result.kind is ExitKind.STOPPED


def test_a_callback_that_raises_does_not_leave_the_engine_running(tmp_path, group):
    seen = {}

    def on_line(line):
        seen["pid"] = int(line)
        raise RuntimeError("the window has a bug")

    argv = standin(tmp_path, "import os, time; print(os.getpid(), flush=True); time.sleep(300)")
    with pytest.raises(RuntimeError, match="the window has a bug"):
        run(argv, tmp_path, group, on_line=on_line)
    assert gone(seen["pid"])


# ------------------------------------------------------------------ how it ended

NO_CORE = "import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
BAD_ALLOC = ("import os, sys\n"
             "sys.stderr.write(\"terminate called after throwing an instance of 'std::bad_alloc'\\n\")\n"
             "sys.stderr.flush()\n")
EXIT_WITH = "import ctypes; ctypes.WinDLL('kernel32').ExitProcess(ctypes.c_uint(%#x))"

ENDINGS = [
    ("pass", (0,), ExitKind.OK),
    ("import sys; sys.stderr.write('error: input file not found: x.fastq\\n'); sys.exit(1)", (1,),
     ExitKind.ENGINE_ERROR),
    ("import sys; sys.stderr.write('error: unknown option\\n'); sys.exit(2)", (2,), ExitKind.ENGINE_ERROR),
    ("import sys; sys.stderr.write('error: ran out of the memory budget (1.0 GB) while counting 21-mers.\\n')\n"
     "sys.exit(1)", (1,), ExitKind.OUT_OF_MEMORY),
]
if WINDOWS:
    ENDINGS += [
        # A real abort of the C runtime: exit code 3 or "fail fast", depending on the runtime.
        # It must come back at all: without the error mode a dialog would wait for a click.
        ("import os; os.abort()", (3, 0xC0000409), ExitKind.CRASH),
        (BAD_ALLOC + "os.abort()", (3, 0xC0000409), ExitKind.OUT_OF_MEMORY),
        (EXIT_WITH % 0xC0000005, (0xC0000005,), ExitKind.CRASH),
        (EXIT_WITH % 0xC00000FD, (0xC00000FD,), ExitKind.CRASH),
        (EXIT_WITH % 0xC0000135, (0xC0000135,), ExitKind.COULD_NOT_START),
        (EXIT_WITH % osapi.STOP_EXIT_CODE, (osapi.STOP_EXIT_CODE,), ExitKind.STOPPED),
    ]
else:
    ENDINGS += [
        (NO_CORE + "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)", (-signal.SIGSEGV,), ExitKind.CRASH),
        (NO_CORE + "import os; os.abort()", (-signal.SIGABRT,), ExitKind.CRASH),
        (NO_CORE + BAD_ALLOC + "os.abort()", (-signal.SIGABRT,), ExitKind.OUT_OF_MEMORY),
        ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", (-signal.SIGKILL,), ExitKind.OUT_OF_MEMORY),
        ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", (-signal.SIGTERM,), ExitKind.STOPPED),
    ]


@pytest.mark.parametrize("body,codes,kind", ENDINGS,
                         ids=["%s%d" % (item[2].value, index) for index, item in enumerate(ENDINGS)])
def test_the_end_of_a_real_process_is_classified(tmp_path, group, body, codes, kind):
    result = run(standin(tmp_path, body), tmp_path, group)
    assert isinstance(result, EngineResult)
    assert result.returncode in codes and result.kind is kind, (result.returncode, result.kind)
    assert result.stopped is False and result.hung is False
    assert result.ok is (kind is ExitKind.OK)
    assert result.seconds > 0


def test_last_error_line():
    tail = "[1/7] loading reads\nerror: first\n  k=21 count\nError: input file not found: x\nbye"
    assert last_error_line(tail) == "Error: input file not found: x"
    assert last_error_line("all fine\n") == ""
    assert last_error_line("") == ""


def test_an_engine_that_is_missing_gives_a_result_not_an_exception(tmp_path, group):
    result = run([str(tmp_path / "no-such-engine")], tmp_path, group)
    assert result.returncode is None
    assert result.kind is ExitKind.COULD_NOT_START
    assert isinstance(result.start_error, FileNotFoundError)
    assert (result.stopped, result.hung, result.log_tail) == (False, False, "")


@windows_only
def test_a_file_that_is_not_a_program_cannot_start(tmp_path, group):
    damaged = tmp_path / "damaged-engine.exe"
    damaged.write_text("this is not a program")
    result = run([str(damaged)], tmp_path, group)
    assert result.returncode is None and result.kind is ExitKind.COULD_NOT_START
    assert getattr(result.start_error, "winerror", None) == 193
    with pytest.raises(EngineStartError) as caught:
        engine_proc.engine_version(str(damaged))
    assert caught.value.kind is ExitKind.COULD_NOT_START


@posix_only
def test_an_engine_that_may_not_be_run_is_blocked(tmp_path, group):
    engine = tmp_path / "engine-without-permission"
    engine.write_text("#!/bin/sh\necho hello\n")
    engine.chmod(0o644)
    result = run([str(engine)], tmp_path, group)
    assert result.returncode is None and result.kind is ExitKind.BLOCKED
    assert isinstance(result.start_error, PermissionError)


def test_a_missing_working_folder_is_a_failed_start(tmp_path, group):
    result = run(standin(tmp_path, "pass"), tmp_path, group, cwd=str(tmp_path / "gone"),
                 log_path=str(tmp_path / "engine.log"))
    assert result.returncode is None and result.kind is ExitKind.COULD_NOT_START


def test_a_log_that_cannot_be_written_raises(tmp_path, group):
    with pytest.raises(OSError):
        run(standin(tmp_path, "pass"), tmp_path, group, log_path=str(tmp_path / "no" / "folder" / "x.log"))


# ------------------------------------------------------------------ hung, working

def test_an_engine_that_does_nothing_is_ended_as_hung(tmp_path, group):
    seen = []
    argv = standin(tmp_path, "import os, time; print(os.getpid(), flush=True); time.sleep(300)")
    result = run(argv, tmp_path, group, idle_timeout=1.0, on_line=seen.append)
    assert result.hung is True and result.stopped is False
    assert result.kind is ExitKind.CRASH                 # documented: check ``hung`` first
    assert 0.9 < result.seconds < 8.0
    assert gone(int(seen[0]))


def test_a_busy_engine_is_not_hung(tmp_path, group):
    argv = standin(tmp_path, """
        import time
        end = time.time() + 2.5
        while time.time() < end:
            pass
    """)
    result = run(argv, tmp_path, group, idle_timeout=1.0, low_priority=False)
    assert result.hung is False and result.kind is ExitKind.OK


def test_an_engine_that_keeps_printing_is_not_hung(tmp_path, group):
    argv = standin(tmp_path, """
        import time
        for number in range(12):
            print("still here", number, flush=True)
            time.sleep(0.2)
    """)
    result = run(argv, tmp_path, group, idle_timeout=1.0)
    assert result.hung is False and result.kind is ExitKind.OK


def test_time_the_pc_slept_does_not_count_as_hung(tmp_path, group, monkeypatch):
    # The wall clock leaps an hour forward between any two looks at it: the PC "slept".
    leaps = {"count": 0}

    def leaping_clock():
        leaps["count"] += 1
        return time.time() + 3600.0 * leaps["count"]

    monkeypatch.setattr(engine_proc, "_wall", leaping_clock)
    stop = threading.Event()
    timer = threading.Timer(2.5, stop.set)
    timer.start()
    try:
        result = run(standin(tmp_path, "import time; time.sleep(300)"), tmp_path, group,
                     idle_timeout=1.0, stop_event=stop)
    finally:
        timer.cancel()
    assert result.hung is False                           # without the leaps this is the hung case above
    assert result.stopped is True and result.seconds >= 2.0


def test_no_idle_timeout_means_never_hung(tmp_path, group):
    result = run(standin(tmp_path, "import time; time.sleep(1.5)"), tmp_path, group, idle_timeout=0)
    assert result.hung is False and result.kind is ExitKind.OK


def test_an_unknown_processor_time_is_never_called_hung(tmp_path, group, monkeypatch):
    monkeypatch.setattr(group, "cpu_seconds", lambda: None)
    working = []
    result = run(standin(tmp_path, "import time; time.sleep(2.2)"), tmp_path, group, idle_timeout=0.5,
                 on_working=lambda busy, memory: working.append(busy))
    assert result.hung is False and result.kind is ExitKind.OK
    assert working and set(working) == {0.0}


BUSY = """
    import time
    end = time.time() + 3.6
    while time.time() < end:
        pass
"""


def test_working_reports_one_busy_thread_for_a_busy_loop(tmp_path, group):
    working = []
    moments = []

    def on_working(busy, memory):
        working.append((busy, memory))
        moments.append(time.monotonic())

    result = run(standin(tmp_path, BUSY), tmp_path, group, on_working=on_working, low_priority=False)
    assert result.kind is ExitKind.OK
    assert 2 <= len(working) <= 6                         # about once a second for 3.6 seconds and the start
    assert all(0.7 < later - earlier < 1.6 for earlier, later in zip(moments, moments[1:]))
    busiest = max(busy for busy, _ in working)
    assert 0.3 < busiest < 1.5, working
    assert all(0.0 <= busy < 1.5 for busy, _ in working)
    assert all(2_000_000 < memory < 500_000_000 for _, memory in working), working
    assert result.peak_memory_bytes >= max(memory for _, memory in working)


def test_working_counts_the_whole_group(tmp_path, group):
    argv = standin(tmp_path, """
        import subprocess, sys
        code = "import time\\nend = time.time() + 3.6\\nwhile time.time() < end: pass"
        other = subprocess.Popen([sys.executable, "-c", code])
        exec(code)
        other.wait()
    """)
    working = []
    result = run(argv, tmp_path, group, on_working=lambda busy, memory: working.append(busy),
                 low_priority=False)
    assert result.kind is ExitKind.OK
    assert 1.1 < max(working) < 2.6, working              # two processes, each one thread busy


def test_peak_memory_is_plausible(tmp_path, group):
    argv = standin(tmp_path, """
        import time
        block = bytearray(80 * 1024 * 1024)
        for index in range(0, len(block), 4096):
            block[index] = 1
        time.sleep(1.6)
    """)
    result = run(argv, tmp_path, group)
    assert result.kind is ExitKind.OK
    assert 80 * 1024 * 1024 < result.peak_memory_bytes < 400 * 1024 * 1024


# ------------------------------------------------------------------ engine_version

def version_standin(tmp_path, body, name="fake-engine"):
    path = tmp_path / name
    path.write_text("#!%s\n%s" % (PY, textwrap.dedent(body)), encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@posix_only
def test_engine_version_is_the_first_line(tmp_path):
    exe = version_standin(tmp_path, """
        import sys
        assert sys.argv[1:] == ["--version"]
        sys.stderr.write("a note on the other stream\\n")
        print("FakeEngine 9.9.9\\r")
        print("a second line")
    """)
    assert engine_proc.engine_version(exe) == "FakeEngine 9.9.9"


@posix_only
def test_engine_version_hides_the_users_tesseract_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_P2_CIRC", "nonsense")
    monkeypatch.setenv("KEEP_ME", "kept")
    exe = version_standin(tmp_path, """
        import os
        stray = sorted(name for name in os.environ if name.upper().startswith("TESSERACT_"))
        print("stray=%s keep=%s color=%s" % (stray, os.environ.get("KEEP_ME"), os.environ.get("NO_COLOR")))
    """)
    assert engine_proc.engine_version(exe) == "stray=[] keep=kept color=1"


def test_engine_version_of_a_missing_engine(tmp_path):
    with pytest.raises(EngineStartError) as caught:
        engine_proc.engine_version(str(tmp_path / "nothing-here"))
    assert caught.value.kind is ExitKind.COULD_NOT_START
    assert isinstance(caught.value.os_error, FileNotFoundError)
    assert caught.value.exe.endswith("nothing-here")


@posix_only
def test_engine_version_of_a_blocked_engine(tmp_path):
    engine = tmp_path / "blocked"
    engine.write_text("#!/bin/sh\necho x\n")
    engine.chmod(0o644)
    with pytest.raises(EngineStartError) as caught:
        engine_proc.engine_version(str(engine))
    assert caught.value.kind is ExitKind.BLOCKED


@posix_only
def test_engine_version_of_an_engine_that_fails(tmp_path):
    exe = version_standin(tmp_path, "import sys; sys.stderr.write('error: broken\\n'); sys.exit(2)")
    with pytest.raises(EngineStartError) as caught:
        engine_proc.engine_version(exe)
    assert (caught.value.kind, caught.value.returncode) == (ExitKind.ENGINE_ERROR, 2)
    assert caught.value.detail == "error: broken"


@posix_only
def test_engine_version_of_an_engine_that_crashes(tmp_path):
    exe = version_standin(tmp_path, NO_CORE + "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)")
    with pytest.raises(EngineStartError) as caught:
        engine_proc.engine_version(exe)
    assert caught.value.kind is ExitKind.CRASH


@posix_only
def test_engine_version_of_a_silent_or_stuck_engine(tmp_path):
    with pytest.raises(EngineStartError) as caught:
        engine_proc.engine_version(version_standin(tmp_path, "pass", "silent"))
    assert caught.value.kind is ExitKind.ENGINE_ERROR
    began = time.monotonic()
    with pytest.raises(EngineStartError) as caught:
        engine_proc.engine_version(version_standin(tmp_path, "import time; time.sleep(60)", "stuck"),
                                   timeout=0.5)
    assert caught.value.kind is ExitKind.COULD_NOT_START
    assert time.monotonic() - began < 5.0


# ------------------------------------------------------------------ parse_progress

def test_progress_of_the_assembler_banners():
    assert parse_progress("tesseract-asm", "[1/7] loading reads") == Progress(1, 7, "loading reads")
    assert parse_progress("tesseract-asm",
                          "[2/7] read error correction (k=21, trust cutoff auto, graph cutoff auto)") == \
        Progress(2, 7, "read error correction")
    assert parse_progress("tesseract-asm", "[3/7] de Bruijn iterations") == Progress(3, 7, "de Bruijn iterations")
    assert parse_progress("tesseract-asm", "[4/7] collecting contigs") == Progress(4, 7, "collecting contigs")
    assert parse_progress("tesseract-asm", "[7/7] writing output\r") == Progress(7, 7, "writing output")


def test_progress_of_the_assembler_k_lines():
    graph = Progress(3, 7, "de Bruijn iterations", None, 55)
    counted = "  k=55  count 0.5s  distinct=444,869 solid=410,177 cutoff=2 peak=40.0"
    assert parse_progress("tesseract-asm", counted) == graph
    assert parse_progress("tesseract-asm", "  k=55  graph 0.4s    built   unitigs=8599  total=874523") == graph
    assert parse_progress("tesseract-asm", "  k=55  [carrygate] enabled=1 mode=1") == graph
    assert parse_progress("tesseract-asm", "  k=127 simplify 0.0s    simplified").k == 127
    assert parse_progress("tesseract-asm", "  k=127 skipped (not shorter than the reads)") is None


@pytest.mark.parametrize("line", [
    "", "      120,000 reads, 18,000,000 bases, max length 150, paired  [mode: standard]",
    "[config] TESSERACT_P2_CIRC=verify", "[seqio] files=2 records=120000",
    "    round 1  tips=2143   bubbles=1727", "[dropoutbridge] K=99 med=22.50",
    "  [deep_norm] sel=30.0 target=2.0", " [3/7] indented", "error: [3/7] not at the start",
    "      QC: dropping 1 top rung(s) -- k=99 would carry only 3.1x",
])
def test_other_assembler_lines_are_not_progress(line):
    assert parse_progress("tesseract-asm", line) is None


def test_progress_of_scepter():
    line = "  [===========             ]  47.5%  466.94K reads  92.25K reads/s   "
    assert parse_progress("scepter", line) == Progress(None, None, "466.94K reads 92.25K reads/s", 0.475)
    full = "  [========================] 100.0%  8.00K reads  48.14K reads/s   "
    assert parse_progress("scepter", full).fraction == 1.0
    assert parse_progress("scepter", "  [                        ]   0.0%  0 reads  0 reads/s").fraction == 0.0
    coloured = "\x1b[90m  [======                  ]  25.0%  1.00M reads  1.00M reads/s\x1b[0m   "
    assert parse_progress("scepter", coloured).fraction == 0.25
    unknown_size = parse_progress("scepter", "  466.94K reads  92.25K reads/s   ")
    assert unknown_size == Progress(None, None, "466.94K reads 92.25K reads/s", None)
    for other in ("[detect] reservoir sample: 200000 + 200000 reads in 2.24s", " " * 78, "",
                  "[detect] A01234 detected; enabling polyG trimming", "error: 47.5% of reads"):
        assert parse_progress("scepter", other) is None


def test_the_engine_may_be_named_by_its_path():
    assert parse_progress(r"C:\Tools\TesserACT-Win\engines\tesseract-asm.exe", "[1/7] loading reads").step == 1
    assert parse_progress("/x/build/engines/linux/scepter", "  [=] 50.0%  1 reads  1 reads/s").fraction == 0.5
    assert parse_progress("SCEPTER.EXE", "  [=] 50.0%  1 reads  1 reads/s").fraction == 0.5
    assert parse_progress("something-else", "[1/7] loading reads") is None
    # each engine's lines mean nothing for the other one
    assert parse_progress("scepter", "[1/7] loading reads") is None
    assert parse_progress("tesseract-asm", "  [=] 50.0%  1 reads  1 reads/s") is None


def test_progress_texts_are_the_ones_in_the_pinned_engine_source():
    """If a new upstream release renumbers or renames its banners, this fails and says so."""
    with open(UPSTREAM_ASSEMBLER, "r", encoding="utf-8") as handle:
        source = handle.read()
    banners = re.findall(r'"(\[\d+/\d+\] [^"\\(]*)', source)
    assert len(banners) == 8, banners                      # step 4 has two wordings
    steps = set()
    for banner in banners:
        progress = parse_progress("tesseract-asm", banner)
        assert progress is not None and progress.total_steps == engine_proc.ASSEMBLER_STEPS, banner
        assert progress.detail == banner.split("] ", 1)[1].strip()
        steps.add(progress.step)
    assert steps == set(range(1, engine_proc.ASSEMBLER_STEPS + 1))
    graph_banner = "[%d/%d] %s" % (engine_proc.ASSEMBLER_GRAPH_STEP, engine_proc.ASSEMBLER_STEPS,
                                   engine_proc.ASSEMBLER_GRAPH_DETAIL)
    assert graph_banner in banners
    # every per-k line is printed with this prefix, and only inside the graph step
    k_formats = re.findall(r'"(  k=%-3d [^"]*)', source)
    assert len(k_formats) >= 8
    assert any(text.startswith("  k=%-3d count ") for text in k_formats)
    assert any(text.startswith("  k=%-3d skipped ") for text in k_formats)
    with open(UPSTREAM_SCEPTER, "r", encoding="utf-8") as handle:
        assert r'"\r%s  [%s] %5.1f%%  %s reads  %s reads/s%s   "' in handle.read()


@pytest.mark.skipif(not os.path.isfile(GOLDEN_LOG), reason="the golden assembly log is not there")
def test_progress_read_from_a_real_assembly_log():
    with open(GOLDEN_LOG, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    seen = []
    for line in lines:
        progress = parse_progress("tesseract-asm", line)
        if progress is not None and (not seen or seen[-1] != progress):
            seen.append(progress)
    assert [progress.step for progress in seen if progress.k is None] == [1, 2, 3, 4, 5, 6, 7]
    assert [progress.k for progress in seen if progress.k is not None] == [21, 33, 55, 77, 87, 99]
    assert all(progress.step == 3 for progress in seen if progress.k is not None)
    assert all(progress.total_steps == 7 and progress.fraction is None for progress in seen)


# ------------------------------------------------------------------ the real engines

def _engines():
    try:
        return appdirs.find_engines()
    except appdirs.EngineMissing:
        return None


@pytest.mark.engines
def test_version_of_the_real_engines():
    engines = _engines()
    if engines is None:
        pytest.skip("the Linux engines are not built")
    assert engine_proc.engine_version(engines.tesseract_asm) == "TesserACT " + versions.TESSERACT_VERSION
    assert engine_proc.engine_version(engines.scepter) == "scepter " + versions.SCEPTER_VERSION


@pytest.mark.engines
def test_the_real_engines_run_and_report_progress(tmp_path, group):
    engines = _engines()
    if engines is None:
        pytest.skip("the Linux engines are not built")
    generator = os.path.join(REPO, "tests", "tools", "gen.py")
    genome = str(tmp_path / "genome.fa")
    subprocess.run([PY, generator, "genome", genome, "7", "60000"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([PY, generator, "reads", genome, str(tmp_path / "reads"), "40", "150", "350", "40",
                    "0.003", "11", "paired", "fastq", "gz"], check=True, stdout=subprocess.DEVNULL)
    environment = {name: value for name, value in os.environ.items() if not name.startswith("TESSERACT_")}
    environment["NO_COLOR"] = "1"

    trimmed = []
    result = run([engines.scepter, "-i", str(tmp_path / "reads_1.fq.gz"), "-I", str(tmp_path / "reads_2.fq.gz"),
                  "-o", "trimmed_R1.fastq.gz", "-O", "trimmed_R2.fastq.gz", "-j", "report.json",
                  "-h", "report.html", "-w", "2", "--no-color", "--no-summary", "--verbose"],
                 tmp_path, group, env=environment, log_path=str(tmp_path / "trimming.log"),
                 on_line=lambda line: trimmed.append(parse_progress("scepter", line)))
    assert result.kind is ExitKind.OK, result.log_tail
    fractions = [progress.fraction for progress in trimmed if progress is not None]
    assert fractions and fractions[-1] == 1.0
    assert (tmp_path / "trimmed_R1.fastq.gz").stat().st_size > 0

    assembled = []

    def on_line(line):
        progress = parse_progress(engines.tesseract_asm, line)
        if progress is not None and (not assembled or assembled[-1] != progress):
            assembled.append(progress)

    result = run([engines.tesseract_asm, "-1", "trimmed_R1.fastq.gz", "-2", "trimmed_R2.fastq.gz",
                  "-o", "assembly", "-t", "2"],
                 tmp_path, group, env=environment, log_path=str(tmp_path / "assembly.log"), on_line=on_line)
    assert result.kind is ExitKind.OK, result.log_tail
    assert [progress.step for progress in assembled if progress.k is None] == [1, 2, 3, 4, 5, 6, 7]
    sizes = [progress.k for progress in assembled if progress.k is not None]
    assert sizes == sorted(sizes) and len(sizes) >= 3
    assert (tmp_path / "assembly" / "contigs.fasta").stat().st_size > 50_000
    assert result.peak_memory_bytes > 10 * 1024 * 1024
    assert "contigs" in result.log_tail
