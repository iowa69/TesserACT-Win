# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""osapi: the Windows calls and their Linux counterparts.

Three kinds of test:

* the Linux behaviour, for real (process groups, locks, drives, memory);
* facts that hold on every system (exit-code tables, structure sizes, prototypes);
* the Windows code paths, run on Linux against stand-in libraries. A stand-in library
  knows only the functions of ``osapi._PROTOTYPES``, refuses a call whose arguments ctypes
  could not convert to the declared types, and records every call. That proves the calls
  are well-formed and made in the right order with the right constants. It cannot prove
  that Windows answers as documented: the CI job on Windows has to.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
import uuid

import pytest

from tesseract_win import osapi
from tesseract_win.osapi import ExitKind, FolderLock, ProcessGroup

WINDOWS = os.name == "nt"
posix_only = pytest.mark.skipif(WINDOWS, reason="exercises the Linux side")
windows_only = pytest.mark.skipif(not WINDOWS, reason="exercises the real Windows calls; run by CI")
# Linux signal numbers, written out: the signal module of Windows does not have them all.
SIGHUP, SIGINT, SIGILL, SIGABRT, SIGBUS, SIGFPE, SIGKILL, SIGSEGV, SIGPIPE, SIGTERM = 1, 2, 4, 6, 7, 8, 9, 11, 13, 15

PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLEEPER = [PY, "-c", "import time; time.sleep(300)"]


# ------------------------------------------------------------------ helpers

def alive(pid):
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


def helper(tmp_path, body, *arguments, name="helper.py", **options):
    """Start a second Python process that can import the package."""
    path = tmp_path / name
    path.write_text("import sys\nsys.path.insert(0, %r)\n" % REPO + textwrap.dedent(body), encoding="utf-8")
    return subprocess.Popen([PY, str(path)] + [str(argument) for argument in arguments],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, **options)


class FakeFunction:
    def __init__(self, library, name, behaviour):
        self._library, self._name, self._behaviour = library, name, behaviour
        self.restype = "not declared"
        self.argtypes = None

    def __call__(self, *arguments):
        assert self.argtypes is not None and self.restype != "not declared", \
            "%s was called without a declared prototype" % self._name
        assert len(arguments) == len(self.argtypes), \
            "%s takes %d arguments, got %d" % (self._name, len(self.argtypes), len(arguments))
        for kind, value in zip(self.argtypes, arguments):
            kind.from_param(value)                    # raises when ctypes could not pass it
        self._library.calls.append((self._name,) + tuple(arguments))
        behaviour = self._behaviour
        if callable(behaviour):
            return behaviour(*arguments)
        return behaviour


class FakeLibrary:
    """Stands in for a Windows DLL. Unknown functions do not exist, as on a real one."""

    def __init__(self, name, behaviours=None, missing=()):
        self.name = name
        self.behaviours = dict(behaviours or {})
        self.missing = set(missing)
        self.calls = []
        self._functions = {}

    def __getattr__(self, attribute):
        if attribute.startswith("_"):
            raise AttributeError(attribute)
        if attribute not in osapi._PROTOTYPES[self.name] or attribute in self.missing:
            raise AttributeError("function %r not found" % attribute)
        if attribute not in self._functions:
            self._functions[attribute] = FakeFunction(self, attribute, self.behaviours.get(attribute, 0))
        return self._functions[attribute]

    def names(self):
        return [call[0] for call in self.calls]

    def calls_of(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class FakeWindows:
    def __init__(self):
        self.libraries = {}
        self.unloadable = set()
        self.last_error = 0

    def library(self, name, behaviours=None, missing=()):
        self.libraries[name] = FakeLibrary(name, behaviours, missing)
        return self.libraries[name]

    def load(self, name):
        if name in self.unloadable:
            raise OSError("cannot load %s" % name)
        if name not in self.libraries:
            self.library(name)
        return self.libraries[name]


@pytest.fixture
def windows(monkeypatch):
    """Make osapi believe it runs on Windows, with stand-in system libraries."""
    fake = FakeWindows()
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    monkeypatch.setattr(osapi, "_RAISE", True)
    monkeypatch.setattr(osapi, "_libraries", {})
    monkeypatch.setattr(osapi, "_load_library", fake.load)
    monkeypatch.setattr(osapi, "_last_error", lambda: fake.last_error)
    monkeypatch.setattr(osapi, "_defaults", None)
    return fake


def fake_process(pid=4321, running=True, handle=0x77):
    process = types.SimpleNamespace(pid=pid, returncode=None if running else 0, _handle=handle, killed=0)
    process.poll = lambda: process.returncode
    process.wait = lambda timeout=None: process.returncode

    def kill():
        process.killed += 1
        process.returncode = 1

    process.kill = kill
    return process


# ================================================================== facts for every system

class TestStructures:
    """Sizes and offsets as on 64-bit Windows. A wrong size makes the Windows call fail."""

    @pytest.mark.parametrize("structure,size", [
        (osapi.JOBOBJECT_EXTENDED_LIMIT_INFORMATION, 144),
        (osapi.JOBOBJECT_BASIC_LIMIT_INFORMATION, 64),
        (osapi.IO_COUNTERS, 48),
        (osapi.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, 48),
        (osapi.JOBOBJECT_BASIC_PROCESS_ID_LIST, 8 + 8 * osapi._PID_SLOTS),
        (osapi.PROCESS_MEMORY_COUNTERS, 72),
        (osapi.FILETIME, 8),
        (osapi.PROCESS_POWER_THROTTLING_STATE, 12),
        (osapi.MEMORYSTATUSEX, 64),
        (osapi.SYSTEM_POWER_STATUS, 12),
        (osapi.FLASHWINFO, 32),
        (osapi.HIGHCONTRASTW, 16),
        (osapi.POINT, 8),
        (osapi.GUID, 16),
        (osapi.RECT, 16),
        (osapi.MONITORINFO, 40),
    ])
    def test_size(self, structure, size):
        assert ctypes.sizeof(ctypes.c_void_p) == 8, "these sizes are for a 64-bit system"
        assert ctypes.sizeof(structure) == size

    def test_offsets(self):
        basic = osapi.JOBOBJECT_BASIC_LIMIT_INFORMATION
        assert (basic.LimitFlags.offset, basic.MinimumWorkingSetSize.offset) == (16, 24)
        assert (basic.ActiveProcessLimit.offset, basic.Affinity.offset) == (40, 48)
        assert (basic.PriorityClass.offset, basic.SchedulingClass.offset) == (56, 60)
        extended = osapi.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        assert (extended.IoInfo.offset, extended.ProcessMemoryLimit.offset) == (64, 112)
        assert (extended.PeakProcessMemoryUsed.offset, extended.PeakJobMemoryUsed.offset) == (128, 136)
        accounting = osapi.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        assert (accounting.TotalUserTime.offset, accounting.TotalKernelTime.offset) == (0, 8)
        assert accounting.ActiveProcesses.offset == 40
        counters = osapi.PROCESS_MEMORY_COUNTERS
        assert (counters.PeakWorkingSetSize.offset, counters.WorkingSetSize.offset) == (8, 16)
        memory = osapi.MEMORYSTATUSEX
        assert (memory.ullTotalPhys.offset, memory.ullAvailPhys.offset) == (8, 16)
        assert (osapi.FLASHWINFO.hwnd.offset, osapi.FLASHWINFO.dwFlags.offset) == (8, 16)
        assert osapi.JOBOBJECT_BASIC_PROCESS_ID_LIST.ProcessIdList.offset == 8

    def test_constants(self):
        assert osapi._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000
        assert osapi._JOBOBJECT_EXTENDED_LIMIT_INFORMATION == 9
        assert osapi._CREATE_NO_WINDOW == 0x08000000
        assert osapi._BELOW_NORMAL_PRIORITY_CLASS == 0x00004000
        assert osapi._ES_CONTINUOUS | osapi._ES_SYSTEM_REQUIRED == 0x80000001
        assert osapi._FLASHW_TRAY | osapi._FLASHW_TIMERNOFG == 14
        assert osapi._ERROR_ALREADY_EXISTS == 183
        assert osapi._FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS == 0x00400000 and osapi._FILE_ATTRIBUTE_OFFLINE == 0x1000
        assert osapi.UTF8_BUILD == 18362
        assert 0 < osapi.STOP_EXIT_CODE < 0x80000000 and osapi.STOP_EXIT_CODE > 255
        if hasattr(subprocess, "CREATE_NO_WINDOW"):       # on Windows: the same numbers as the library's
            assert osapi._CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW
            assert osapi._BELOW_NORMAL_PRIORITY_CLASS == subprocess.BELOW_NORMAL_PRIORITY_CLASS


class TestPrototypes:
    HANDLE_RETURNING = {("kernel32", "CreateJobObjectW"), ("kernel32", "OpenProcess"),
                        ("kernel32", "CreateMutexW"), ("user32", "GetForegroundWindow"),
                        ("user32", "MonitorFromPoint"), ("shell32", "ILCreateFromPathW")}

    def test_every_prototype_is_well_formed(self):
        for library, functions in osapi._PROTOTYPES.items():
            for name, (restype, argtypes) in functions.items():
                assert restype is None or hasattr(restype, "from_param"), (library, name)
                assert isinstance(argtypes, list), (library, name)
                for kind in argtypes:
                    assert hasattr(kind, "from_param"), (library, name)

    def test_handles_are_returned_at_full_width(self):
        for library, name in self.HANDLE_RETURNING:
            restype = osapi._PROTOTYPES[library][name][0]
            assert restype is ctypes.c_void_p, (library, name)
            assert ctypes.sizeof(restype) == ctypes.sizeof(ctypes.c_void_p)

    def test_handle_arguments_are_pointer_sized(self):
        kernel32 = osapi._PROTOTYPES["kernel32"]
        for name in ("SetInformationJobObject", "QueryInformationJobObject", "AssignProcessToJobObject",
                     "TerminateJobObject", "CloseHandle", "TerminateProcess", "GetProcessTimes",
                     "K32GetProcessMemoryInfo", "SetProcessInformation"):
            assert kernel32[name][1][0] is ctypes.c_void_p, name
        assert kernel32["AssignProcessToJobObject"][1] == [ctypes.c_void_p, ctypes.c_void_p]

    def test_dll_declares_every_prototype_and_loads_once(self, windows):
        library = osapi._dll("kernel32")
        assert osapi._dll("kernel32") is library
        for name, (restype, argtypes) in osapi._PROTOTYPES["kernel32"].items():
            function = getattr(library, name)
            assert function.restype is restype and function.argtypes == argtypes

    def test_a_function_missing_from_a_library_does_not_stop_the_others(self, windows):
        windows.library("kernel32", missing={"K32GetProcessMemoryInfo"})
        library = osapi._dll("kernel32")
        assert library.GetACP.argtypes == []
        assert not hasattr(library, "K32GetProcessMemoryInfo")

    def test_every_windows_function_used_in_the_source_is_declared(self):
        import re
        with open(osapi.__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        declared = {name for functions in osapi._PROTOTYPES.values() for name in functions}
        used = set(re.findall(r"\b(?:kernel32|user32|shell32|ole32|dwmapi)\.([A-Z]\w+)\(", source))
        used |= set(re.findall(r'_dll\("\w+"\)\.([A-Z]\w+)\(', source))
        assert used, "the scan found nothing: it is broken"
        assert used <= declared, used - declared
        assert declared <= used, "declared but never called: %s" % (declared - used)


class TestClassifyExit:
    @pytest.mark.parametrize("code,kind", [
        (0, ExitKind.OK),
        (1, ExitKind.ENGINE_ERROR),
        (2, ExitKind.ENGINE_ERROR),
        (64, ExitKind.ENGINE_ERROR),
        (3, ExitKind.CRASH),                              # abort() of the Windows C runtime
        (osapi.STOP_EXIT_CODE, ExitKind.STOPPED),
        (0xC0000005, ExitKind.CRASH),                     # access violation
        (3221225477, ExitKind.CRASH),                     # the same, as Python reports it
        (0xC00000FD, ExitKind.CRASH),                     # stack overflow
        (0xC0000409, ExitKind.CRASH),                     # abort / fail fast
        (0xC000001D, ExitKind.CRASH),                     # illegal instruction
        (0x80000003, ExitKind.CRASH),                     # breakpoint
        (0xC0000135, ExitKind.COULD_NOT_START),           # a DLL is missing
        (0xC0000142, ExitKind.COULD_NOT_START),           # a DLL failed to initialise
        (0xC000007B, ExitKind.COULD_NOT_START),           # not a valid 64-bit program
        (0xC0000022, ExitKind.BLOCKED),
        (0xC0000428, ExitKind.BLOCKED),
        (0xC0000017, ExitKind.OUT_OF_MEMORY),
        (0xC000012D, ExitKind.OUT_OF_MEMORY),
        (0xC000013A, ExitKind.STOPPED),
        (-1073741819, ExitKind.CRASH),                    # 0xC0000005 as a signed number
        (-1073741515, ExitKind.COULD_NOT_START),          # 0xC0000135 as a signed number
        (-SIGSEGV, ExitKind.CRASH),
        (-SIGBUS, ExitKind.CRASH),
        (-SIGILL, ExitKind.CRASH),
        (-SIGFPE, ExitKind.CRASH),
        (-SIGABRT, ExitKind.CRASH),
        (-SIGPIPE, ExitKind.CRASH),
        (-SIGKILL, ExitKind.OUT_OF_MEMORY),               # not ours (ours is reported by run_engine)
        (-SIGTERM, ExitKind.STOPPED),
        (-SIGINT, ExitKind.STOPPED),
        (-SIGHUP, ExitKind.STOPPED),
        (134, ExitKind.CRASH),                            # SIGABRT seen through a shell
        (137, ExitKind.OUT_OF_MEMORY),                    # SIGKILL seen through a shell
        (139, ExitKind.CRASH),
        (143, ExitKind.STOPPED),
        (None, ExitKind.CRASH),
    ])
    def test_code(self, code, kind):
        assert osapi.classify_exit(code, "") is kind

    @pytest.mark.parametrize("code", [1, 3, 0xC0000409, 0xC0000005, -SIGABRT, 134])
    @pytest.mark.parametrize("tail", [
        "terminate called after throwing an instance of 'std::bad_alloc'\n  what():  std::bad_alloc",
        "error: ran out of the memory budget (6.4 GB) while counting 55-mers. Raise it with --max-memory",
        "scepter: Out of memory",
        "mmap: Cannot allocate memory",
    ])
    def test_memory_words_in_the_log_win(self, code, tail):
        assert osapi.classify_exit(code, "[3/7] de Bruijn iterations\n" + tail) is ExitKind.OUT_OF_MEMORY

    def test_memory_words_do_not_change_a_clear_answer(self):
        tail = "std::bad_alloc"
        assert osapi.classify_exit(0, tail) is ExitKind.OK
        assert osapi.classify_exit(osapi.STOP_EXIT_CODE, tail) is ExitKind.STOPPED
        assert osapi.classify_exit(0xC0000135, tail) is ExitKind.COULD_NOT_START
        assert osapi.classify_exit(-SIGTERM, tail) is ExitKind.STOPPED

    def test_every_kind_can_be_reached(self):
        codes = [0, 1, 3, osapi.STOP_EXIT_CODE, 0xC0000017, 0xC0000135, 0xC0000022]
        assert {osapi.classify_exit(code) for code in codes} == set(ExitKind)

    @posix_only
    def test_signal_numbers_match_this_system(self):
        assert (osapi._SIGHUP, osapi._SIGINT, osapi._SIGQUIT, osapi._SIGKILL, osapi._SIGTERM) == \
            (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGKILL, signal.SIGTERM)
        assert (SIGHUP, SIGINT, SIGILL, SIGABRT, SIGBUS, SIGFPE, SIGKILL, SIGSEGV, SIGPIPE, SIGTERM) == \
            (signal.SIGHUP, signal.SIGINT, signal.SIGILL, signal.SIGABRT, signal.SIGBUS, signal.SIGFPE,
             signal.SIGKILL, signal.SIGSEGV, signal.SIGPIPE, signal.SIGTERM)


class TestClassifyStartError:
    @staticmethod
    def windows_error(code):
        error = OSError(errno.EINVAL, "CreateProcess failed")
        error.winerror = code
        return error

    @pytest.mark.parametrize("code,kind", [
        (2, ExitKind.COULD_NOT_START), (3, ExitKind.COULD_NOT_START), (193, ExitKind.COULD_NOT_START),
        (216, ExitKind.COULD_NOT_START), (267, ExitKind.COULD_NOT_START), (14001, ExitKind.COULD_NOT_START),
        (87, ExitKind.COULD_NOT_START),
        (5, ExitKind.BLOCKED), (225, ExitKind.BLOCKED), (226, ExitKind.BLOCKED), (1260, ExitKind.BLOCKED),
        (4551, ExitKind.BLOCKED), (32, ExitKind.BLOCKED), (740, ExitKind.BLOCKED),
        (8, ExitKind.OUT_OF_MEMORY), (1455, ExitKind.OUT_OF_MEMORY),
    ])
    def test_windows_code(self, code, kind):
        assert osapi.classify_start_error(self.windows_error(code)) is kind

    def test_the_windows_code_wins_over_errno(self):
        error = OSError(errno.ENOENT, "odd")
        error.winerror = 225
        assert osapi.classify_start_error(error) is ExitKind.BLOCKED

    @pytest.mark.parametrize("error,kind", [
        (FileNotFoundError(errno.ENOENT, "gone"), ExitKind.COULD_NOT_START),
        (NotADirectoryError(errno.ENOTDIR, "odd"), ExitKind.COULD_NOT_START),
        (OSError(errno.ENOEXEC, "Exec format error"), ExitKind.COULD_NOT_START),
        (PermissionError(errno.EACCES, "denied"), ExitKind.BLOCKED),
        (PermissionError(errno.EPERM, "not permitted"), ExitKind.BLOCKED),
        (OSError(errno.ETXTBSY, "busy"), ExitKind.BLOCKED),
        (OSError(errno.ENOMEM, "no memory"), ExitKind.OUT_OF_MEMORY),
        (BlockingIOError(errno.EAGAIN, "no more processes"), ExitKind.OUT_OF_MEMORY),
        (ValueError("embedded null byte"), ExitKind.COULD_NOT_START),
        (OSError("no number at all"), ExitKind.COULD_NOT_START),
    ])
    def test_errno(self, error, kind):
        assert osapi.classify_start_error(error) is kind


def test_the_module_imports_the_standard_library_only():
    code = ("import sys; import tesseract_win.osapi, tesseract_win.engine_proc; "
            "extra = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('tkinter', '_tkinter', 'psutil', 'PIL', 'numpy', 'win32api')); print(extra)")
    done = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


# ================================================================== Linux, for real

@posix_only
class TestProcessGroupOnLinux:
    @pytest.fixture
    def group(self):
        made = ProcessGroup()
        yield made
        made.close()

    def start(self, group, argv=None, low_priority=False, **options):
        process = subprocess.Popen(argv or SLEEPER, stdin=subprocess.DEVNULL,
                                   **group.popen_kwargs(low_priority), **options)
        group.add(process)
        return process

    def test_popen_kwargs(self, group):
        kwargs = group.popen_kwargs(low_priority=True)
        assert kwargs["start_new_session"] is True
        assert callable(kwargs["preexec_fn"])
        assert "creationflags" not in kwargs
        assert osapi.no_window_kwargs() == {}

    def test_child_is_in_its_own_session_and_low_priority_is_nicer(self, group):
        code = "import os, time; print(os.getsid(0) == os.getpid(), os.nice(0), flush=True); time.sleep(300)"
        low = self.start(group, [PY, "-c", code], low_priority=True, stdout=subprocess.PIPE, text=True)
        normal = self.start(group, [PY, "-c", code], low_priority=False, stdout=subprocess.PIPE, text=True)
        leads_low, nice_low = low.stdout.readline().split()
        leads_normal, nice_normal = normal.stdout.readline().split()
        assert (leads_low, leads_normal) == ("True", "True")
        assert int(nice_low) == int(nice_normal) + osapi._LOW_PRIORITY_NICE
        low.stdout.close()
        normal.stdout.close()

    def test_kill_ends_children_and_their_children(self, group):
        code = ("import subprocess, sys, time\n"
                "inner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
                "print(inner.pid, flush=True); time.sleep(300)")
        first = self.start(group, [PY, "-c", code], stdout=subprocess.PIPE, text=True)
        second = self.start(group)
        inner = int(first.stdout.readline())
        first.stdout.close()
        assert alive(first.pid) and alive(second.pid) and alive(inner)
        assert group.kill_count == 0
        began = time.monotonic()
        group.kill()
        assert first.wait(timeout=3) == -signal.SIGKILL
        assert second.wait(timeout=3) == -signal.SIGKILL
        assert gone(inner)
        assert time.monotonic() - began < 2.0
        assert group.kill_count == 1
        group.kill()                                      # nothing left: harmless
        assert group.kill_count == 2

    def test_kill_reaches_a_grandchild_that_left_the_process_group(self, group):
        code = ("import subprocess, sys, time\n"
                "inner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],"
                " process_group=0)\n"
                "print(inner.pid, flush=True); time.sleep(300)")
        if sys.version_info < (3, 11):
            pytest.skip("process_group needs Python 3.11")
        outer = self.start(group, [PY, "-c", code], stdout=subprocess.PIPE, text=True)
        inner = int(outer.stdout.readline())
        outer.stdout.close()
        assert os.getpgid(inner) != os.getpgid(outer.pid)
        group.kill()
        outer.wait(timeout=3)
        assert gone(inner)

    def test_the_group_can_be_used_again_after_a_kill(self, group):
        first = self.start(group)
        group.kill()
        first.wait(timeout=3)
        second = self.start(group)
        assert alive(second.pid)
        group.kill()
        assert second.wait(timeout=3) == -signal.SIGKILL

    def test_kill_does_not_touch_other_processes(self, group):
        outsider = subprocess.Popen(SLEEPER, start_new_session=True)
        try:
            inside = self.start(group)
            group.kill()
            inside.wait(timeout=3)
            time.sleep(0.2)
            assert outsider.poll() is None
        finally:
            outsider.kill()
            outsider.wait()

    def test_close_ends_everything_and_is_safe_twice(self):
        group = ProcessGroup()
        process = self.start(group)
        group.close()
        assert process.poll() == -signal.SIGKILL          # close() also collected it
        group.close()
        late = subprocess.Popen(SLEEPER, **group.popen_kwargs(False))
        assert group.add(late) is False                   # a closed group ends what it is given
        assert late.wait(timeout=3) == -signal.SIGKILL

    def test_context_manager(self):
        with ProcessGroup() as group:
            process = self.start(group)
        assert process.poll() == -signal.SIGKILL

    def test_cpu_seconds_grow_with_a_busy_child_and_never_shrink(self, group):
        assert group.cpu_seconds() == 0.0
        busy = self.start(group, [PY, "-c", "while True: pass"])
        time.sleep(0.2)
        first = group.cpu_seconds()
        time.sleep(1.0)
        second = group.cpu_seconds()
        assert 0.3 < second - first < 1.4, (first, second)
        group.kill()
        busy.wait(timeout=3)
        time.sleep(0.1)
        after = group.cpu_seconds()
        assert after >= second                            # the time of an ended engine stays counted
        time.sleep(0.1)
        assert group.cpu_seconds() == after

    def test_cpu_seconds_of_a_sleeping_child_stand_still(self, group):
        code = "import time; print('asleep', flush=True); time.sleep(300)"
        child = self.start(group, [PY, "-c", code], stdout=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == "asleep"
        child.stdout.close()
        time.sleep(0.3)
        first = group.cpu_seconds()
        time.sleep(0.7)
        assert group.cpu_seconds() == first

    def test_memory_now_and_peak(self, group):
        assert group.memory_bytes() == 0 and group.peak_memory_bytes() == 0
        code = ("import time\nblock = bytearray(60 * 1024 * 1024)\n"
                "for index in range(0, len(block), 4096): block[index] = 1\n"
                "print('ready', flush=True); time.sleep(300)")
        big = self.start(group, [PY, "-c", code], stdout=subprocess.PIPE, text=True)
        assert big.stdout.readline().strip() == "ready"
        big.stdout.close()
        small = self.start(group)
        time.sleep(0.3)
        now = group.memory_bytes()
        assert 60 * 1024 * 1024 < now < 400 * 1024 * 1024
        assert group.peak_memory_bytes() >= now
        assert group.peak_memory_bytes(big) >= 60 * 1024 * 1024
        assert 0 < group.peak_memory_bytes(small) < 40 * 1024 * 1024
        group.kill()
        big.wait(timeout=3)
        small.wait(timeout=3)
        time.sleep(0.1)
        assert group.memory_bytes() == 0
        assert group.peak_memory_bytes() >= now           # the peak is remembered
        assert group.peak_memory_bytes(big) >= 60 * 1024 * 1024

    def test_finished_engines_are_forgotten(self, group):
        process = self.start(group, [PY, "-c", "pass"])
        process.wait(timeout=10)
        time.sleep(0.1)
        group.cpu_seconds()
        assert group._members == {} and group._cpu_seen == {}
        group.kill()                                      # must not signal a number that is no longer ours

    def test_a_number_handed_to_a_stranger_is_left_alone(self, group, monkeypatch):
        # The engine was collected long ago; the kernel has given its number to another program.
        stranger = subprocess.Popen(SLEEPER, start_new_session=True)
        try:
            ended = types.SimpleNamespace(pid=stranger.pid, returncode=0, poll=lambda: 0,
                                          wait=lambda timeout=None: 0)
            group._members[stranger.pid] = osapi._Member(ended, True)
            group.kill()
            time.sleep(0.2)
            assert stranger.poll() is None
            assert stranger.pid not in group._members
            group._members[stranger.pid] = osapi._Member(ended, True)
            assert group.memory_bytes() == 0
            assert stranger.pid not in group._members
        finally:
            stranger.kill()
            stranger.wait()

    def test_a_process_started_without_the_group_arguments_is_still_ended(self, group):
        plain = subprocess.Popen(SLEEPER)
        assert group.add(plain) is False                  # noted, but its children cannot be tracked
        time.sleep(0.2)
        assert group.memory_bytes() > 0
        group.kill()
        assert plain.wait(timeout=3) == -signal.SIGKILL

    def test_children_die_when_the_owning_process_is_killed(self, tmp_path):
        owner = helper(tmp_path, """
            import subprocess, time
            from tesseract_win import osapi
            group = osapi.ProcessGroup()
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                     **group.popen_kwargs(False))
            group.add(child)
            print(child.pid, flush=True)
            time.sleep(300)
        """)
        child = int(owner.stdout.readline())
        try:
            assert alive(child)
            owner.send_signal(signal.SIGKILL)             # no chance to clean up
            assert owner.wait(timeout=3) == -signal.SIGKILL
            assert gone(child, timeout=3.0)
        finally:
            owner.stdout.close()
            owner.stdin.close()
            if alive(child):
                os.kill(child, signal.SIGKILL)

    def test_without_the_group_such_a_child_would_live_on(self, tmp_path):
        """The control for the test above: it is the group that ends the child."""
        owner = helper(tmp_path, """
            import subprocess, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                     start_new_session=True)
            print(child.pid, flush=True)
            time.sleep(300)
        """)
        child = int(owner.stdout.readline())
        try:
            owner.send_signal(signal.SIGKILL)
            owner.wait(timeout=3)
            time.sleep(0.5)
            assert alive(child)
        finally:
            owner.stdout.close()
            owner.stdin.close()
            os.kill(child, signal.SIGKILL)

    def test_children_die_when_the_owning_process_simply_exits(self, tmp_path):
        owner = helper(tmp_path, """
            import subprocess
            from tesseract_win import osapi
            group = osapi.ProcessGroup()
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                     **group.popen_kwargs(False))
            group.add(child)
            print(child.pid, flush=True)
        """)
        child = int(owner.stdout.readline())
        assert owner.wait(timeout=10) == 0
        owner.stdout.close()
        owner.stdin.close()
        assert gone(child)

    def test_methods_may_be_called_from_several_threads(self, group):
        self.start(group, [PY, "-c", "while True: pass"])
        errors = []

        def measure():
            try:
                for _ in range(30):
                    group.cpu_seconds()
                    group.memory_bytes()
                    group.peak_memory_bytes()
            except Exception as error:                    # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=measure) for _ in range(4)]
        for thread in threads:
            thread.start()
        group.kill()
        for thread in threads:
            thread.join()
        assert errors == []


@posix_only
class TestFolderLockOnLinux:
    HOLDER = """
        from tesseract_win import osapi
        lock = osapi.FolderLock()
        print("LOCKED" if lock.acquire(sys.argv[1], sys.argv[2]) else "BUSY", flush=True)
        sys.stdin.readline()
    """

    def test_a_second_process_is_kept_out_and_the_lock_goes_with_its_holder(self, tmp_path):
        folder = tmp_path / "results"
        folder.mkdir()
        holder = helper(tmp_path, self.HOLDER, folder, "PC-7, anna, 2026-11-17T03:42:10")
        try:
            assert holder.stdout.readline().strip() == "LOCKED"
            lock = FolderLock()
            began = time.monotonic()
            assert lock.acquire(str(folder), "me") is False
            assert time.monotonic() - began < 1.0
            assert lock.held is False and lock.degraded is False
            assert FolderLock.holder_text(str(folder)) == "PC-7, anna, 2026-11-17T03:42:10"
            assert lock.holder_text(str(folder)) == "PC-7, anna, 2026-11-17T03:42:10"

            holder.send_signal(signal.SIGKILL)            # a crash: no clean-up at all
            holder.wait(timeout=3)
            assert (folder / osapi.LOCK_FILE_NAME).exists()          # the file is still there...
            assert FolderLock.holder_text(str(folder)) == ""         # ...and means nothing
            assert lock.acquire(str(folder), "me, now") is True
            assert lock.held is True and lock.degraded is False
            assert (folder / osapi.LOCK_FILE_NAME).read_text(encoding="utf-8") == "me, now"
            lock.release()
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
            holder.stdin.close()

    def test_the_other_process_is_kept_out_while_we_hold_it(self, tmp_path):
        folder = tmp_path / "results"
        folder.mkdir()
        lock = FolderLock()
        assert lock.acquire(str(folder), "first") is True
        second = helper(tmp_path, self.HOLDER, folder, "second")
        try:
            assert second.stdout.readline().strip() == "BUSY"
        finally:
            second.stdin.close()
            second.wait(timeout=5)
            second.stdout.close()
        assert FolderLock.holder_text(str(folder)) == "first"
        lock.release()
        third = helper(tmp_path, self.HOLDER, folder, "third")
        try:
            assert third.stdout.readline().strip() == "LOCKED"
        finally:
            third.stdin.close()
            third.wait(timeout=5)
            third.stdout.close()

    def test_a_lock_file_that_merely_exists_is_not_a_lock(self, tmp_path):
        (tmp_path / osapi.LOCK_FILE_NAME).write_text("left behind by a crash last week", encoding="utf-8")
        assert FolderLock.holder_text(str(tmp_path)) == ""
        lock = FolderLock()
        assert lock.acquire(str(tmp_path), "new run") is True
        assert (tmp_path / osapi.LOCK_FILE_NAME).read_text(encoding="utf-8") == "new run"
        lock.release()

    def test_release_removes_the_file_and_the_lock(self, tmp_path):
        lock = FolderLock()
        assert lock.acquire(str(tmp_path), "one") is True
        assert (tmp_path / osapi.LOCK_FILE_NAME).is_file()
        lock.release()
        assert not (tmp_path / osapi.LOCK_FILE_NAME).exists()
        assert lock.held is False
        lock.release()                                    # twice is fine
        assert FolderLock.holder_text(str(tmp_path)) == ""
        assert lock.acquire(str(tmp_path), "two") is True
        lock.release()

    def test_two_locks_in_one_process_exclude_each_other(self, tmp_path):
        first, second = FolderLock(), FolderLock()
        assert first.acquire(str(tmp_path), "first") is True
        assert second.acquire(str(tmp_path), "second") is False
        assert FolderLock.holder_text(str(tmp_path)) == "first"       # also for our own lock
        first.release()
        assert second.acquire(str(tmp_path), "second") is True
        assert FolderLock.holder_text(str(tmp_path)) == "second"
        second.release()

    def test_acquiring_again_moves_the_lock(self, tmp_path):
        one, two = tmp_path / "one", tmp_path / "two"
        one.mkdir()
        two.mkdir()
        lock = FolderLock()
        assert lock.acquire(str(one), "x") and lock.acquire(str(two), "y")
        assert FolderLock.holder_text(str(one)) == "" and FolderLock.holder_text(str(two)) == "y"
        with lock:
            pass
        assert FolderLock.holder_text(str(two)) == ""

    def test_the_lock_file_itself_may_be_named(self, tmp_path):
        path = tmp_path / "my.lock"
        lock = FolderLock()
        assert lock.acquire(str(path), "by file") is True
        assert path.read_text(encoding="utf-8") == "by file"
        assert FolderLock.holder_text(str(path)) == "by file"
        lock.release()
        assert not path.exists()

    def test_a_holder_without_text_is_unknown_not_absent(self, tmp_path):
        lock = FolderLock()
        assert lock.acquire(str(tmp_path)) is True
        assert FolderLock.holder_text(str(tmp_path)) == osapi.UNKNOWN_HOLDER
        lock.release()

    def test_text_with_accents_and_no_folder(self, tmp_path):
        lock = FolderLock()
        assert lock.acquire(str(tmp_path), "PC-Jos\u00e9, \u65e5\u672c") is True
        assert FolderLock.holder_text(str(tmp_path)) == "PC-Jos\u00e9, \u65e5\u672c"
        lock.release()
        assert FolderLock.holder_text(str(tmp_path / "no" / "such")) == ""

    def test_a_lock_that_cannot_be_taken_lets_the_run_through(self, tmp_path):
        lock = FolderLock()
        assert lock.acquire(str(tmp_path / "no" / "such" / "folder"), "x") is True
        assert lock.degraded is True and lock.held is False
        lock.release()
        assert lock.degraded is False

    def test_a_drive_without_locks_lets_the_run_through(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osapi, "_try_lock", lambda descriptor: None)
        lock = FolderLock()
        assert lock.acquire(str(tmp_path), "x") is True
        assert lock.degraded is True and lock.held is False
        assert FolderLock.holder_text(str(tmp_path)) == ""

    def test_engines_do_not_inherit_the_lock(self, tmp_path):
        lock = FolderLock()
        assert lock.acquire(str(tmp_path), "run") is True
        child = subprocess.Popen(SLEEPER)                 # lives on after the release
        try:
            lock.release()
            again = FolderLock()
            assert again.acquire(str(tmp_path), "next run") is True
            again.release()
        finally:
            child.kill()
            child.wait()

    @pytest.mark.parametrize("made_again", [False, True])
    def test_a_file_deleted_between_open_and_lock_is_not_trusted(self, tmp_path, monkeypatch, made_again):
        real = osapi._try_lock
        path = tmp_path / osapi.LOCK_FILE_NAME
        state = {"first": True}

        def racing(descriptor):
            if state["first"]:                            # another run releases (deletes) right now
                state["first"] = False
                os.remove(str(path))
                if made_again:                            # ...and a third one makes the file anew
                    path.write_text("a third run")
            return real(descriptor)

        monkeypatch.setattr(osapi, "_try_lock", racing)
        lock = FolderLock()
        assert lock.acquire(str(tmp_path), "careful") is True
        assert path.read_text(encoding="utf-8") == "careful"      # the lock is on the file that has the name
        assert os.fstat(lock._descriptor).st_ino == os.stat(str(path)).st_ino
        lock.release()


@posix_only
class TestSingleInstanceOnLinux:
    def test_second_claim_is_refused_until_the_first_is_given_up(self):
        name = "tesseract-win-test-%s" % uuid.uuid4().hex
        first = osapi.single_instance(name)
        assert first is not None and first.exclusive is True
        assert osapi.single_instance(name) is None
        other = osapi.single_instance(name + "-other")
        assert other is not None
        other.close()
        first.close()
        first.close()                                     # twice is fine
        again = osapi.single_instance(name)
        assert again is not None
        with again:
            assert osapi.single_instance(name) is None
        assert osapi.bring_other_instance_to_front("TesserACT-Win") is False

    def test_a_second_process_is_refused_and_a_dead_one_frees_the_name(self, tmp_path):
        name = "tesseract-win-test-%s" % uuid.uuid4().hex
        body = """
            from tesseract_win import osapi
            handle = osapi.single_instance(sys.argv[1])
            print("FIRST" if handle else "SECOND", flush=True)
            sys.stdin.readline()
        """
        first = helper(tmp_path, body, name)
        try:
            assert first.stdout.readline().strip() == "FIRST"
            assert osapi.single_instance(name) is None
            second = helper(tmp_path, body, name)
            assert second.stdout.readline().strip() == "SECOND"
            second.stdin.close()
            second.wait(timeout=5)
            second.stdout.close()
            first.send_signal(signal.SIGKILL)
            first.wait(timeout=3)
            handle = osapi.single_instance(name)
            assert handle is not None
            handle.close()
        finally:
            first.kill()
            first.wait()
            first.stdout.close()
            first.stdin.close()

    def test_odd_characters_in_the_name(self):
        handle = osapi.single_instance("Local\\Tess/er ACT:%s" % uuid.uuid4().hex)
        assert handle is not None
        handle.close()


@posix_only
class TestLinuxAnswers:
    def test_process_defaults_do_nothing_twice(self, monkeypatch):
        monkeypatch.setattr(osapi, "_defaults", None)
        expected = {"error_mode": False, "dll_directory": False, "dpi_aware": False}
        assert osapi.set_process_defaults() == expected
        assert osapi.set_process_defaults() == expected

    def test_keep_awake(self):
        assert osapi.keep_awake(True) is False
        assert osapi.keep_awake(False) is False

    def test_power_status(self):
        on_battery, percent, has_battery = osapi.power_status()
        assert on_battery in (True, False, None) and has_battery in (True, False, None)
        assert percent is None or 0 <= percent <= 100

    def make_supply(self, root, name, **files):
        folder = root / name
        folder.mkdir()
        for key, value in files.items():
            (folder / key).write_text(value + "\n")

    def test_power_status_read_from_the_system_files(self, tmp_path):
        assert osapi._posix_power_status(str(tmp_path / "nothing")) == (None, None, None)
        assert osapi._posix_power_status(str(tmp_path)) == (False, None, False)        # a desktop
        self.make_supply(tmp_path, "AC", type="Mains", online="1")
        assert osapi._posix_power_status(str(tmp_path)) == (False, None, False)
        self.make_supply(tmp_path, "hidpp_battery_0", type="Battery", scope="Device", capacity="55",
                         status="Discharging")                                         # a mouse
        assert osapi._posix_power_status(str(tmp_path)) == (False, None, False)
        self.make_supply(tmp_path, "BAT0", type="Battery", capacity="79", status="Charging")
        assert osapi._posix_power_status(str(tmp_path)) == (False, 79, True)
        (tmp_path / "AC" / "online").write_text("0\n")
        (tmp_path / "BAT0" / "status").write_text("Discharging\n")
        (tmp_path / "BAT0" / "capacity").write_text("18\n")
        assert osapi._posix_power_status(str(tmp_path)) == (True, 18, True)
        (tmp_path / "BAT0" / "status").write_text("Unknown\n")
        assert osapi._posix_power_status(str(tmp_path)) == (True, 18, True)            # the charger is out

    def test_memory(self):
        total, available = osapi.total_memory_bytes(), osapi.available_memory_bytes()
        assert total > 256 * 1024 * 1024
        assert 0 < available <= total
        assert total == os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")

    def test_drive_kind_and_filesystem(self, tmp_path):
        assert osapi.drive_kind(str(tmp_path)) in ("fixed", "removable", "network")
        assert osapi.drive_kind(str(tmp_path / "not" / "made" / "yet")) == osapi.drive_kind(str(tmp_path))
        assert osapi.drive_kind("/proc") == "fixed"
        name = osapi.filesystem_name(str(tmp_path))
        assert name and name == osapi.filesystem_name(str(tmp_path / "later"))
        assert osapi.filesystem_name("/proc") == "proc"

    def test_mount_table(self, tmp_path):
        table = tmp_path / "mountinfo"
        table.write_text(textwrap.dedent("""\
            23 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw
            40 23 0:35 / /mnt/nas rw - cifs //nas/share rw,vers=3.0
            41 23 8:17 / /media/anna/USB\\040STICK rw,nosuid - vfat /dev/sdb1 rw
            42 23 0:36 / /home/anna/server rw - fuse.sshfs anna@host:/data rw
            43 23 8:33 / /mnt/big rw - exfat /dev/sdc1 rw
            44 23 0:40 / /mnt/nas/deeper rw - nfs4 other:/x rw
            garbage line
        """))
        mount = lambda path: osapi._posix_mount(path, str(table))      # noqa: E731
        assert mount("/home/anna/results") == ("/", "ext4", "/dev/sda2")
        assert mount("/mnt/nas/run 1") == ("/mnt/nas", "cifs", "//nas/share")
        assert mount("/mnt/nas/deeper/x") == ("/mnt/nas/deeper", "nfs4", "other:/x")
        assert mount("/mnt/nasty") == ("/", "ext4", "/dev/sda2")       # not inside /mnt/nas
        assert mount("/media/anna/USB STICK/reads") == ("/media/anna/USB STICK", "vfat", "/dev/sdb1")
        assert mount("/home/anna/server/x")[1] == "fuse.sshfs"
        assert osapi._posix_mount("/x", str(tmp_path / "missing")) is None

    def test_drive_answers_built_from_the_mount_table(self, monkeypatch):
        mounts = {"/n": ("/n", "cifs", "//nas/share"), "/u": ("/u", "vfat", "/dev/sdb1"),
                  "/e": ("/e", "exfat", "/dev/sdc1"), "/w": ("/w", "ntfs3", "/dev/sda1"),
                  "/s": ("/s", "fuse.sshfs", "host:/x"), "/h": ("/h", "ext4", "/dev/sda2")}
        monkeypatch.setattr(osapi, "_posix_mount", lambda path: mounts.get(path))
        monkeypatch.setattr(osapi, "_posix_is_removable", lambda source: source == "/dev/sdb1")
        assert [osapi.drive_kind(path) for path in ("/n", "/s", "/u", "/h", "/zzz")] == \
            ["network", "network", "removable", "fixed", "unknown"]
        assert [osapi.filesystem_name(path) for path in ("/u", "/e", "/w", "/h", "/n", "/zzz")] == \
            ["FAT32", "exFAT", "NTFS", "ext4", "cifs", ""]

    def test_plain_answers(self, tmp_path, monkeypatch):
        monkeypatch.delenv(osapi.THEME_VARIABLE, raising=False)
        assert osapi.is_online_only(str(tmp_path)) is False
        assert osapi.windows_build() == 0
        assert osapi.ansi_code_page() == 0
        assert osapi.path_fits_system_encoding("/data/\u65e5\u672c/\u00e9") is True
        assert osapi.is_dark_mode() is False
        assert osapi.is_high_contrast() is False
        assert osapi.flash_taskbar(object()) is False
        assert osapi.set_dark_titlebar(object(), True) is False
        assert osapi.work_area_at(100, 100) is None

    def test_open_and_reveal_start_the_desktop_opener(self, tmp_path, monkeypatch):
        started = []

        class FakeProcess:
            def __init__(self, command, **options):
                started.append((command, options))

            def wait(self):
                return 0

        monkeypatch.setattr(osapi.subprocess, "Popen", FakeProcess)
        monkeypatch.setattr(osapi.shutil, "which", lambda name: "/usr/bin/" + name if name == "xdg-open" else None)
        report = tmp_path / "report \u00e9 #1 100%.html"
        report.write_text("x")
        assert osapi.open_path(str(report)) is True
        assert osapi.reveal(str(report)) is True
        assert osapi.open_path(str(tmp_path / "missing.html")) is False
        assert osapi.reveal(str(tmp_path / "missing.html")) is False
        assert osapi.open_path("") is False
        assert [command for command, _ in started] == [["/usr/bin/xdg-open", str(report)],
                                                       ["/usr/bin/xdg-open", str(tmp_path)]]
        for _, options in started:                        # detached, silent, and in no process group of ours
            assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
            assert options["start_new_session"] is True

    def test_desktop_folder(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".config").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert osapi.desktop_dir() == str(home)                    # no Desktop at all: the home folder
        (home / "Desktop").mkdir()
        assert osapi.desktop_dir() == str(home / "Desktop")
        (home / "Scrivania").mkdir()                               # a translated Desktop
        (home / ".config" / "user-dirs.dirs").write_text(
            '# comment\nXDG_DOCUMENTS_DIR="$HOME/Documenti"\nXDG_DESKTOP_DIR="$HOME/Scrivania"\n')
        assert osapi.desktop_dir() == str(home / "Scrivania")
        (home / ".config" / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/Gone"\n')
        assert osapi.desktop_dir() == str(home / "Desktop")        # named, but not there

    def test_open_without_an_opener_or_with_one_that_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(osapi.shutil, "which", lambda name: None)
        assert osapi.open_path(str(tmp_path)) is False

        def refuse(*_arguments, **_options):
            raise OSError("no")

        monkeypatch.setattr(osapi.shutil, "which", lambda name: "/usr/bin/xdg-open")
        monkeypatch.setattr(osapi.subprocess, "Popen", refuse)
        assert osapi.open_path(str(tmp_path)) is False
        assert osapi.reveal(str(tmp_path)) is False


# ================================================================== Windows, for real (CI only)

@windows_only
@pytest.mark.windows
class TestOnRealWindows:
    """What no stand-in library can show: that Windows answers as its documentation says.

    These run only on Windows, so only in CI. Nothing here opens a window that stays or
    starts a program that outlives the test.
    """

    INNER = ("import subprocess, sys, time\n"
             "inner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
             "print(inner.pid, flush=True); time.sleep(300)")
    HOLDER = """
        from tesseract_win import osapi
        lock = osapi.FolderLock()
        print("LOCKED" if lock.acquire(sys.argv[1], sys.argv[2]) else "BUSY", flush=True)
        sys.stdin.readline()
    """

    @pytest.fixture
    def group(self):
        made = ProcessGroup()
        yield made
        made.close()

    def start(self, group, argv=None, low_priority=False, **options):
        process = subprocess.Popen(argv or SLEEPER, stdin=subprocess.DEVNULL,
                                   **group.popen_kwargs(low_priority), **options)
        assert group.add(process) is True                 # really in the Job Object
        return process

    def test_every_declared_function_exists_in_its_library(self):
        for library, functions in osapi._PROTOTYPES.items():
            loaded = osapi._dll(library)
            for name in functions:
                assert hasattr(loaded, name), "%s.%s" % (library, name)

    def test_process_defaults(self):
        first = osapi.set_process_defaults()
        assert first["error_mode"] is True and first["dll_directory"] is True
        assert osapi.set_process_defaults() == first
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetErrorMode.restype = ctypes.c_uint32
        assert kernel32.GetErrorMode() & 0x0003 == 0x0003          # no "no disk" box, no crash box

    def test_the_job_holds_the_engines_and_never_this_program(self, group):
        assert group._job, "no Job Object could be made"
        child = self.start(group)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        inside = ctypes.c_int(-1)
        assert kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), group._job, ctypes.byref(inside))
        assert inside.value == 0, "this program is inside its own job"
        assert kernel32.IsProcessInJob(int(child._handle), group._job, ctypes.byref(inside))
        assert inside.value != 0

    def test_kill_ends_the_engine_and_its_own_child_with_our_exit_code(self, group):
        outer = self.start(group, [PY, "-c", self.INNER], stdout=subprocess.PIPE, text=True)
        inner = int(outer.stdout.readline())
        outer.stdout.close()
        assert alive(outer.pid) and alive(inner)
        began = time.monotonic()
        group.kill()
        assert outer.wait(timeout=5) == osapi.STOP_EXIT_CODE
        assert gone(inner)
        assert time.monotonic() - began < 2.0
        assert osapi.classify_exit(outer.returncode) is ExitKind.STOPPED
        again = self.start(group)                         # the job is still usable
        group.kill()
        assert again.wait(timeout=5) == osapi.STOP_EXIT_CODE

    def test_close_ends_what_is_left(self):
        group = ProcessGroup()
        process = self.start(group)
        group.close()
        assert process.wait(timeout=5) == osapi.STOP_EXIT_CODE
        group.close()

    def test_low_priority_and_no_console_window(self, group):
        code = ("import ctypes\n"
                "k = ctypes.WinDLL('kernel32')\n"
                "k.GetCurrentProcess.restype = ctypes.c_void_p\n"
                "k.GetPriorityClass.argtypes = [ctypes.c_void_p]\n"
                "k.GetPriorityClass.restype = ctypes.c_uint32\n"
                "k.GetConsoleWindow.restype = ctypes.c_void_p\n"
                "u = ctypes.WinDLL('user32')\n"
                "u.IsWindowVisible.argtypes = [ctypes.c_void_p]\n"
                "window = k.GetConsoleWindow()\n"
                "print(k.GetPriorityClass(k.GetCurrentProcess()), int(bool(window and u.IsWindowVisible(window))),"
                " flush=True)")
        low = self.start(group, [PY, "-c", code], low_priority=True, stdout=subprocess.PIPE, text=True)
        priority, visible = low.stdout.readline().split()
        low.stdout.close()
        assert int(priority) == 0x4000                     # BELOW_NORMAL_PRIORITY_CLASS
        assert visible == "0"

    def test_cpu_and_memory_of_the_job(self, group):
        busy = self.start(group, [PY, "-c", "while True: pass"])
        time.sleep(0.5)
        first = group.cpu_seconds()
        time.sleep(1.0)
        second = group.cpu_seconds()
        assert first is not None and second is not None
        assert 0.3 < second - first < 1.5, (first, second)
        now = group.memory_bytes()
        assert 2_000_000 < now < 500_000_000
        assert group.peak_memory_bytes() >= now
        assert group.peak_memory_bytes(busy) >= 2_000_000
        group.kill()
        busy.wait(timeout=5)
        time.sleep(0.3)
        assert group.cpu_seconds() >= second               # the job goes on counting what has ended
        assert group.memory_bytes() == 0
        assert group.peak_memory_bytes(busy) >= 2_000_000

    def test_children_die_when_the_owning_process_is_killed(self, tmp_path):
        owner = helper(tmp_path, """
            import subprocess, time
            from tesseract_win import osapi
            group = osapi.ProcessGroup()
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                     **group.popen_kwargs(False))
            print(child.pid, int(group.add(child)), flush=True)
            time.sleep(300)
        """)
        child, in_job = [int(part) for part in owner.stdout.readline().split()]
        try:
            assert in_job == 1 and alive(child)
            owner.kill()                                  # TerminateProcess: no clean-up at all
            owner.wait(timeout=5)
            assert gone(child, timeout=5.0)               # Windows closed the job handle; the job ended it
        finally:
            owner.stdout.close()
            owner.stdin.close()
            if alive(child):
                subprocess.run(["taskkill", "/F", "/PID", str(child)], capture_output=True)

    def test_without_the_group_such_a_child_would_live_on(self, tmp_path):
        owner = helper(tmp_path, """
            import subprocess, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                                     creationflags=0x08000000)
            print(child.pid, flush=True)
            time.sleep(300)
        """)
        child = int(owner.stdout.readline())
        try:
            owner.kill()
            owner.wait(timeout=5)
            time.sleep(1.0)
            assert alive(child)
        finally:
            owner.stdout.close()
            owner.stdin.close()
            subprocess.run(["taskkill", "/F", "/PID", str(child)], capture_output=True)

    def test_folder_lock_between_two_processes(self, tmp_path):
        folder = tmp_path / "results é"
        folder.mkdir()
        lock_file = folder / osapi.LOCK_FILE_NAME
        holder = helper(tmp_path, self.HOLDER, folder, "PC-7, anna, 2026-11-17T03:42:10")
        try:
            assert holder.stdout.readline().strip() == "LOCKED"
            lock = FolderLock()
            assert lock.acquire(str(folder), "me") is False
            assert lock.held is False and lock.degraded is False
            # The text can be read although the file is locked: the locked byte is far behind it.
            assert FolderLock.holder_text(str(folder)) == "PC-7, anna, 2026-11-17T03:42:10"
            assert lock_file.read_text(encoding="utf-8") == "PC-7, anna, 2026-11-17T03:42:10"
            holder.kill()                                 # a crash
            holder.wait(timeout=5)
            assert lock_file.exists()
            assert FolderLock.holder_text(str(folder)) == ""          # the file alone is no lock
            assert lock.acquire(str(folder), "me, now") is True
            assert lock.held is True and lock.degraded is False
            assert FolderLock().acquire(str(folder), "a second lock in this process") is False
            assert FolderLock.holder_text(str(folder)) == "me, now"
            lock.release()
            assert not lock_file.exists()
            assert FolderLock.holder_text(str(folder)) == ""
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
            holder.stdin.close()

    def test_single_instance(self, tmp_path):
        name = "tesseract-win-test-%s" % uuid.uuid4().hex
        first = osapi.single_instance(name)
        assert first is not None and first.exclusive is True
        assert osapi.single_instance(name) is None
        second = helper(tmp_path, """
            from tesseract_win import osapi
            print("FIRST" if osapi.single_instance(sys.argv[1]) else "SECOND", flush=True)
        """, name)
        assert second.stdout.readline().strip() == "SECOND"
        second.wait(timeout=10)
        second.stdout.close()
        second.stdin.close()
        first.close()
        again = osapi.single_instance(name)
        assert again is not None and again.exclusive is True
        again.close()
        assert osapi.bring_other_instance_to_front("no window is called %s" % name) is False

    def test_keep_awake(self):
        assert threading.current_thread() is threading.main_thread()
        assert osapi.keep_awake(True) is True
        assert osapi.keep_awake(False) is False

    def test_facts_about_this_pc(self, tmp_path):
        on_battery, percent, has_battery = osapi.power_status()
        assert on_battery in (True, False, None) and has_battery in (True, False, None)
        assert percent is None or 0 <= percent <= 100
        total, available = osapi.total_memory_bytes(), osapi.available_memory_bytes()
        assert total > 1024 ** 3 and 0 < available <= total
        assert osapi.drive_kind(str(tmp_path)) == "fixed"
        assert osapi.drive_kind(str(tmp_path / "not" / "made" / "yet")) == "fixed"
        assert osapi.filesystem_name(str(tmp_path)) in ("NTFS", "ReFS")
        assert osapi.synced_folder_name(str(tmp_path)) == ""
        assert osapi.windows_build() >= 10240
        assert osapi.ansi_code_page() > 0
        assert osapi.path_fits_system_encoding(str(tmp_path / "日本 é")) is True
        plain = tmp_path / "plain.txt"
        plain.write_text("x")
        assert osapi.is_online_only(str(plain)) is False
        assert osapi.is_online_only(str(tmp_path / "missing")) is False
        assert osapi.is_dark_mode() in (True, False)
        assert osapi.is_high_contrast() in (True, False)
        assert osapi.open_path(str(tmp_path / "missing.html")) is False
        assert osapi.reveal(str(tmp_path / "missing.html")) is False
        desktop = osapi.desktop_dir()
        assert os.path.isdir(desktop) and os.path.isabs(desktop)
        area = osapi.work_area_at(0, 0)
        assert area is None or (area[2] > area[0] and area[3] > area[1])

    @pytest.mark.gui
    def test_window_calls_with_a_real_window(self):
        tkinter = pytest.importorskip("tkinter")
        try:
            root = tkinter.Tk()
        except tkinter.TclError:
            pytest.skip("no desktop to open a window on")
        try:
            root.update()
            area = osapi.work_area_at(root.winfo_rootx(), root.winfo_rooty())
            assert area is not None and area[2] > area[0] and area[3] > area[1]
            assert osapi._tk_frame_handle(root) > 0
            assert osapi.set_dark_titlebar(root, True) in (True, False)
            assert osapi.flash_taskbar(root) in (True, False)
            root.update()
        finally:
            root.destroy()


# ================================================================== answers that do not depend on the system

class TestSyncedFolders:
    @pytest.mark.parametrize("path,name", [
        (r"C:\Users\anna\OneDrive\Desktop\Results", "OneDrive"),
        (r"C:\Users\anna\OneDrive - Contoso Ltd\Sequencing", "OneDrive"),
        ("/home/anna/Dropbox/run1", "Dropbox"),
        (r"D:\Dropbox (Personal)\run1", "Dropbox"),
        (r"C:\Users\anna\Google Drive\run1", "Google Drive"),
        (r"G:\My Drive\run1", "Google Drive"),
        ("/Users/anna/Library/CloudStorage/GoogleDrive-anna@lab.org/My Drive/x", "Google Drive"),
        (r"C:\Users\anna\iCloudDrive\run1", "iCloud Drive"),
        ("/Users/anna/iCloud Drive/x", "iCloud Drive"),
        (r"C:\Users\anna\Nextcloud\lab", "Nextcloud"),
        (r"C:\Users\anna\ONEDRIVE\x", "OneDrive"),
        (r"C:\Sequencing\Results", ""),
        (r"C:\Users\anna\Documents\dropbox-notes\x", ""),
        (r"C:\Users\anna\OneDriveBackup\x", ""),
        (r"\\server\share\Results", ""),
        ("/data/results", ""),
    ])
    def test_by_name(self, path, name, monkeypatch):
        for variable in osapi._ONEDRIVE_VARIABLES:
            monkeypatch.delenv(variable, raising=False)
        assert osapi.synced_folder_name(path) == name

    def test_by_the_folders_windows_names(self, monkeypatch):
        monkeypatch.setenv("OneDriveCommercial", r"D:\Cloud\Work Files")
        monkeypatch.delenv("OneDrive", raising=False)
        monkeypatch.delenv("OneDriveConsumer", raising=False)
        assert osapi.synced_folder_name(r"D:\Cloud\Work Files\Results\KP_001") == "OneDrive"
        assert osapi.synced_folder_name(r"d:/cloud/work files") == "OneDrive"
        assert osapi.synced_folder_name(r"D:\Cloud\Work Files 2\Results") == ""
        assert osapi.synced_folder_name(r"D:\Cloud") == ""


class TestEncoding:
    def test_round_trip(self):
        assert osapi._round_trips(r"C:\Données\Münster\reads_R1.fastq.gz", "cp1252") is True
        assert osapi._round_trips("C:\\\u65e5\u672c\\reads.fastq.gz", "cp1252") is False
        assert osapi._round_trips("C:\\\u65e5\u672c\\reads.fastq.gz", "cp932") is True
        assert osapi._round_trips("C:\\Donn\u00e9es", "cp932") is False
        assert osapi._round_trips("C:\\\u0141\u00f3d\u017a", "cp1250") is True
        assert osapi._round_trips("C:\\\u0141\u00f3d\u017a", "cp1252") is False
        assert osapi._round_trips("plain", "no-such-encoding") is False

    def test_a_look_alike_is_not_a_round_trip(self, monkeypatch):
        # What Windows' "best fit" does: no error, but another character comes back.
        class BestFit(str):
            def encode(self, encoding="utf-8", errors="strict"):
                return b"C:\\Lodz"

        assert osapi._round_trips(BestFit("C:\\\u0141\u00f3d\u017a"), "cp1252") is False

    def test_old_windows_asks_the_code_page_new_windows_does_not(self, windows, monkeypatch):
        asked = []
        monkeypatch.setattr(osapi, "_round_trips", lambda text, encoding: asked.append((text, encoding)) or False)
        version = types.SimpleNamespace(build=17763, platform_version=(10, 0, 17763))
        monkeypatch.setattr(sys, "getwindowsversion", lambda: version, raising=False)
        assert osapi.windows_build() == 17763
        assert osapi.path_fits_system_encoding("C:\\\u65e5\u672c") is False
        assert asked == [("C:\\\u65e5\u672c", "mbcs")]
        version.build, version.platform_version = 18362, (10, 0, 18362)
        assert osapi.path_fits_system_encoding("C:\\\u65e5\u672c") is True
        version.build, version.platform_version = 9200, (10, 0, 22631)        # a build hidden by compatibility
        assert osapi.windows_build() == 22631
        assert len(asked) == 1

    def test_an_unknown_build_is_treated_as_old(self, windows, monkeypatch):
        monkeypatch.setattr(osapi, "_RAISE", False)

        def broken():
            raise OSError("no version")

        monkeypatch.setattr(sys, "getwindowsversion", broken, raising=False)
        monkeypatch.setattr(osapi, "_round_trips", lambda text, encoding: "ok" in text)
        assert osapi.windows_build() == 0
        assert osapi.path_fits_system_encoding("ok") is True
        assert osapi.path_fits_system_encoding("no") is False


def test_theme_can_be_forced(monkeypatch):
    monkeypatch.setenv(osapi.THEME_VARIABLE, "dark")
    assert osapi.is_dark_mode() is True
    monkeypatch.setenv(osapi.THEME_VARIABLE, " Light ")
    assert osapi.is_dark_mode() is False


# ================================================================== the Windows paths, against stand-ins

class TestWindowsProcessDefaults:
    def test_all_three_are_set_once(self, windows):
        kernel32 = windows.library("kernel32", {"SetErrorMode": 0, "SetDllDirectoryW": 1})
        shcore = windows.library("shcore", {"SetProcessDpiAwareness": 0})
        user32 = windows.library("user32")
        assert osapi.set_process_defaults() == {"error_mode": True, "dll_directory": True, "dpi_aware": True}
        assert kernel32.calls == [("SetErrorMode", 0x8003), ("SetDllDirectoryW", None)]
        assert shcore.calls == [("SetProcessDpiAwareness", 1)]             # system DPI aware, not per monitor
        assert user32.calls == []
        assert osapi.set_process_defaults() == {"error_mode": True, "dll_directory": True, "dpi_aware": True}
        assert len(kernel32.calls) == 2 and len(shcore.calls) == 1         # the second call did nothing

    def test_error_mode_keeps_flags_that_were_set_before(self, windows):
        kernel32 = windows.library("kernel32", {"SetErrorMode": 0x0004, "SetDllDirectoryW": 1})
        windows.library("shcore", {"SetProcessDpiAwareness": 0})
        osapi.set_process_defaults()
        assert kernel32.calls_of("SetErrorMode") == [(0x8003,), (0x8007,)]

    def test_dpi_already_declared_counts_as_done(self, windows):
        windows.library("kernel32", {"SetDllDirectoryW": 1})
        windows.library("shcore", {"SetProcessDpiAwareness": osapi._E_ACCESSDENIED})
        user32 = windows.library("user32")
        assert osapi.set_process_defaults()["dpi_aware"] is True
        assert user32.calls == []

    @pytest.mark.parametrize("how", ["no library", "no function", "refused"])
    def test_dpi_falls_back_to_the_older_call(self, windows, how):
        windows.library("kernel32", {"SetDllDirectoryW": 1})
        if how == "no library":
            windows.unloadable.add("shcore")
        elif how == "no function":
            windows.library("shcore", missing={"SetProcessDpiAwareness"})
        else:
            windows.library("shcore", {"SetProcessDpiAwareness": -2147024809})    # E_INVALIDARG
        user32 = windows.library("user32", {"SetProcessDPIAware": 1})
        assert osapi.set_process_defaults()["dpi_aware"] is True
        assert user32.calls == [("SetProcessDPIAware",)]

    def test_a_failing_step_is_logged_and_the_others_still_run(self, windows, monkeypatch, caplog):
        monkeypatch.setattr(osapi, "_RAISE", False)
        monkeypatch.setattr(osapi, "_reported", set())

        def explode(_mode):
            raise OSError("kernel32 is having a bad day")

        kernel32 = windows.library("kernel32", {"SetErrorMode": explode, "SetDllDirectoryW": 0})
        windows.library("shcore", {"SetProcessDpiAwareness": 0})
        windows.last_error = 87
        with caplog.at_level("WARNING", logger="tesseract_win.osapi"):
            result = osapi.set_process_defaults()
        assert result == {"error_mode": False, "dll_directory": False, "dpi_aware": True}
        assert "SetDllDirectoryW" in kernel32.names()
        assert "error_mode" in caplog.text and "Windows error 87" in caplog.text


class TestWindowsProcessGroup:
    JOB = 0x1F4

    def kernel(self, windows, **overrides):
        behaviours = {"CreateJobObjectW": self.JOB, "SetInformationJobObject": 1,
                      "OpenProcess": 0x2A8, "AssignProcessToJobObject": 1, "TerminateJobObject": 1,
                      "TerminateProcess": 1, "CloseHandle": 1, "SetProcessInformation": 1}
        behaviours.update(overrides)
        return windows.library("kernel32", behaviours)

    def test_the_job_is_made_with_kill_on_close(self, windows):
        seen = {}

        def set_information(job, kind, buffer, size):
            info = buffer._obj
            seen.update(job=job, kind=kind, size=size, type=type(info),
                        flags=info.BasicLimitInformation.LimitFlags)
            return 1

        kernel32 = self.kernel(windows, SetInformationJobObject=set_information)
        group = ProcessGroup()
        assert group._job == self.JOB
        assert kernel32.calls_of("CreateJobObjectW") == [(None, None)]      # no name; not inheritable
        assert seen["job"] == self.JOB and seen["kind"] == 9 and seen["size"] == 144
        assert seen["type"] is osapi.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        assert seen["flags"] & 0x2000
        assert seen["flags"] == 0x2400
        # never this program itself:
        assert "AssignProcessToJobObject" not in kernel32.names() and "OpenProcess" not in kernel32.names()
        group.close()

    def test_the_optional_flag_is_dropped_if_windows_refuses_it(self, windows):
        flags = []

        def set_information(job, kind, buffer, size):
            flags.append(buffer._obj.BasicLimitInformation.LimitFlags)
            return 1 if len(flags) == 2 else 0

        self.kernel(windows, SetInformationJobObject=set_information)
        group = ProcessGroup()
        assert flags == [0x2400, 0x2000] and group._job == self.JOB
        group.close()

    def test_popen_kwargs(self, windows):
        self.kernel(windows)
        group = ProcessGroup()
        assert group.popen_kwargs(low_priority=True) == {"creationflags": 0x08000000 | 0x00004000}
        assert group.popen_kwargs(low_priority=False) == {"creationflags": 0x08000000}
        assert osapi.no_window_kwargs() == {"creationflags": 0x08000000}
        group.close()

    def test_add_puts_the_engine_into_the_job(self, windows):
        kernel32 = self.kernel(windows)
        group = ProcessGroup()
        process = fake_process(pid=4321)
        assert group.add(process) is True
        assert kernel32.calls_of("OpenProcess") == [(0x0301, 0, 4321)]      # set quota, terminate, set information
        assert kernel32.calls_of("AssignProcessToJobObject") == [(self.JOB, 0x2A8)]
        assert kernel32.calls_of("CloseHandle") == [(0x2A8,)]              # the opened handle, not the job
        assert group._loose == []
        group.close()

    def test_add_switches_power_throttling_off_for_the_engine(self, windows):
        seen = {}

        def set_information(handle, kind, buffer, size):
            state = buffer._obj
            seen.update(handle=handle, kind=kind, size=size, version=state.Version,
                        control=state.ControlMask, state=state.StateMask)
            return 1

        kernel32 = self.kernel(windows, SetProcessInformation=set_information)
        group = ProcessGroup()
        assert group.add(fake_process()) is True
        assert seen == {"handle": 0x2A8, "kind": 4, "size": 12, "version": 1, "control": 1, "state": 0}
        names = kernel32.names()
        assert names.index("SetProcessInformation") < names.index("CloseHandle")     # while the handle is open
        group.close()

    @pytest.mark.parametrize("how", ["refused", "unknown to this Windows", "raises"])
    def test_an_old_windows_without_power_throttling_is_no_problem(self, windows, monkeypatch, how):
        monkeypatch.setattr(osapi, "_RAISE", False)

        def explode(*_arguments):
            raise OSError("no")

        if how == "unknown to this Windows":
            kernel32 = self.kernel(windows)
            kernel32.missing.add("SetProcessInformation")
        else:
            kernel32 = self.kernel(windows, SetProcessInformation=0 if how == "refused" else explode)
        windows.last_error = 87
        group = ProcessGroup()
        assert group.add(fake_process()) is True
        assert kernel32.calls_of("AssignProcessToJobObject") == [(self.JOB, 0x2A8)]
        assert kernel32.calls_of("CloseHandle") == [(0x2A8,)]
        group.close()

    def test_add_uses_the_handle_of_subprocess_when_the_process_cannot_be_opened(self, windows):
        kernel32 = self.kernel(windows, OpenProcess=None)
        group = ProcessGroup()
        assert group.add(fake_process(handle=0x77)) is True
        assert kernel32.calls_of("AssignProcessToJobObject") == [(self.JOB, 0x77)]
        assert kernel32.calls_of("CloseHandle") == []                      # not ours to close
        group.close()

    def test_an_engine_that_had_already_ended_is_fine(self, windows):
        self.kernel(windows, AssignProcessToJobObject=0)
        group = ProcessGroup()
        assert group.add(fake_process(running=False)) is True
        assert group._loose == []
        group.close()

    def test_when_assigning_fails_the_engine_is_ended_one_by_one(self, windows, caplog):
        kernel32 = self.kernel(windows, AssignProcessToJobObject=0)
        windows.last_error = 5
        group = ProcessGroup()
        process = fake_process(pid=99)
        with caplog.at_level("WARNING", logger="tesseract_win.osapi"):
            assert group.add(process) is False
        assert "Windows error 5" in caplog.text
        assert group._loose == [process]
        group.kill()
        assert kernel32.calls_of("TerminateJobObject") == [(self.JOB, osapi.STOP_EXIT_CODE)]
        assert kernel32.calls_of("TerminateProcess") == [(0x2A8, osapi.STOP_EXIT_CODE)]
        assert process.killed == 0                                         # ended with our own exit code
        group.close()

    def test_kill_ends_the_whole_job_with_our_exit_code(self, windows):
        kernel32 = self.kernel(windows)
        group = ProcessGroup()
        group.add(fake_process())
        group.kill()
        group.kill()
        assert kernel32.calls_of("TerminateJobObject") == [(self.JOB, osapi.STOP_EXIT_CODE)] * 2
        assert group.kill_count == 2 and group._job == self.JOB            # still usable
        group.close()

    def test_close_ends_the_job_and_closes_its_handle_once(self, windows):
        kernel32 = self.kernel(windows)
        group = ProcessGroup()
        group.close()
        group.close()
        assert kernel32.names() == ["CreateJobObjectW", "SetInformationJobObject",
                                    "TerminateJobObject", "CloseHandle"]
        assert kernel32.calls_of("CloseHandle") == [(self.JOB,)]
        assert group._job is None
        process = fake_process()
        assert group.add(process) is False                                 # closed: ended at once
        assert kernel32.calls_of("TerminateProcess") == [(0x2A8, osapi.STOP_EXIT_CODE)]

    def test_without_a_job_everything_still_works(self, windows, caplog):
        kernel32 = self.kernel(windows, CreateJobObjectW=None)
        windows.last_error = 8
        with caplog.at_level("WARNING", logger="tesseract_win.osapi"):
            group = ProcessGroup()
        assert group._job is None and "CreateJobObjectW failed" in caplog.text
        process = fake_process()
        assert group.add(process) is False
        group.kill()
        assert kernel32.calls_of("TerminateProcess") == [(0x2A8, osapi.STOP_EXIT_CODE)]
        assert "TerminateJobObject" not in kernel32.names()
        group.close()

    def test_a_job_that_cannot_be_configured_is_given_up(self, windows):
        kernel32 = self.kernel(windows, SetInformationJobObject=0)
        group = ProcessGroup()
        assert group._job is None
        assert kernel32.calls_of("CloseHandle") == [(self.JOB,)]
        group.close()

    def measuring_kernel(self, windows, pids, **overrides):
        opened = []

        def query(job, kind, buffer, size, returned):
            target = buffer._obj
            if kind == 1:
                assert isinstance(target, osapi.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) and size == 48
                target.TotalUserTime = 30_000_000           # 3 s, in units of 100 ns
                target.TotalKernelTime = 5_000_000          # 0.5 s
                return 1
            assert kind == 3 and isinstance(target, osapi.JOBOBJECT_BASIC_PROCESS_ID_LIST)
            assert size == ctypes.sizeof(target)
            target.NumberOfAssignedProcesses = len(pids)
            target.NumberOfProcessIdsInList = len(pids)
            for index, pid in enumerate(pids):
                target.ProcessIdList[index] = pid
            return 1

        def open_process(access, inherit, pid):
            opened.append((access, pid))
            return 0x1000 + pid

        def memory_info(handle, buffer, size):
            counters = buffer._obj
            assert size == 72 and counters.cb == 72
            pid = handle - 0x1000
            counters.WorkingSetSize = pid * 1_000_000
            counters.PeakWorkingSetSize = pid * 1_500_000
            return 1

        behaviours = {"QueryInformationJobObject": query, "OpenProcess": open_process,
                      "K32GetProcessMemoryInfo": memory_info}
        behaviours.update(overrides)
        return self.kernel(windows, **behaviours), opened

    def test_cpu_and_memory_come_from_the_job(self, windows):
        kernel32, opened = self.measuring_kernel(windows, [200, 300])
        group = ProcessGroup()
        assert group.cpu_seconds() == pytest.approx(3.5)
        assert group.memory_bytes() == 500_000_000
        assert group.peak_memory_bytes() == 500_000_000          # the largest sum seen
        assert group.peak_memory_bytes(fake_process(pid=300)) == 450_000_000
        assert group.peak_memory_bytes(fake_process(pid=200)) == 300_000_000
        assert all(access == 0x1010 for access, _ in opened)     # query limited information + read memory
        closed = [call[0] for call in kernel32.calls_of("CloseHandle")]
        assert sorted(closed) == sorted(0x1000 + pid for _, pid in opened)     # every handle closed again
        group.close()

    def test_measurements_are_reused_for_a_moment(self, windows):
        kernel32, _ = self.measuring_kernel(windows, [200])
        group = ProcessGroup()
        group.cpu_seconds()
        group.memory_bytes()
        assert len(kernel32.calls_of("QueryInformationJobObject")) == 2        # one look, two answers
        group.close()

    def test_cpu_is_unknown_when_the_job_cannot_be_asked(self, windows):
        self.measuring_kernel(windows, [], QueryInformationJobObject=0)
        group = ProcessGroup()
        assert group.cpu_seconds() is None
        assert group.memory_bytes() == 0
        group.close()

    def test_a_long_process_list_is_read_as_far_as_it_fits(self, windows):
        def query(job, kind, buffer, size, returned):
            target = buffer._obj
            if kind == 1:
                return 1
            target.NumberOfAssignedProcesses = 5000
            target.NumberOfProcessIdsInList = 5000              # more than the list can hold
            for index in range(osapi._PID_SLOTS):
                target.ProcessIdList[index] = 8
            return 0                                             # "more data"

        self.measuring_kernel(windows, [], QueryInformationJobObject=query)
        windows.last_error = 234
        group = ProcessGroup()
        assert group.memory_bytes() == osapi._PID_SLOTS * 8_000_000
        group.close()

    def test_engines_outside_the_job_are_measured_one_by_one(self, windows):
        def times(handle, created, ended, kernel, user):
            kernel._obj.dwLowDateTime = 10_000_000              # 1 s
            user._obj.dwHighDateTime = 1                        # 2**32 * 100 ns = 429.4967296 s
            return 1

        self.measuring_kernel(windows, [], CreateJobObjectW=None, GetProcessTimes=times)
        group = ProcessGroup()
        process = fake_process(pid=7)
        group.add(process)
        assert group.cpu_seconds() == pytest.approx(430.4967296)
        assert group.memory_bytes() == 7_000_000
        process.returncode = 0                                   # it ends: its time stays counted
        time.sleep(osapi._SAMPLE_REUSE_SECONDS + 0.02)
        assert group.cpu_seconds() == pytest.approx(430.4967296)
        assert group._loose == []
        time.sleep(osapi._SAMPLE_REUSE_SECONDS + 0.02)
        assert group.cpu_seconds() == pytest.approx(430.4967296)
        assert group.memory_bytes() == 0
        group.close()


class TestWindowsKeepAwake:
    def test_on_and_off(self, windows):
        kernel32 = windows.library("kernel32", {"SetThreadExecutionState": 0x80000000})
        assert osapi.keep_awake(True) is True
        assert osapi.keep_awake(False) is False
        assert kernel32.calls == [("SetThreadExecutionState", 0x80000001), ("SetThreadExecutionState", 0x80000000)]

    def test_a_refused_request_is_reported(self, windows):
        windows.library("kernel32", {"SetThreadExecutionState": 0})
        assert osapi.keep_awake(True) is False

    def test_another_thread_gets_a_warning(self, windows, caplog):
        windows.library("kernel32", {"SetThreadExecutionState": 0x80000000})
        with caplog.at_level("WARNING", logger="tesseract_win.osapi"):
            thread = threading.Thread(target=osapi.keep_awake, args=(True,))
            thread.start()
            thread.join()
        assert "main thread" in caplog.text


class TestWindowsSingleInstance:
    def test_first_copy(self, windows):
        kernel32 = windows.library("kernel32", {"CreateMutexW": 0x3C, "CloseHandle": 1})
        handle = osapi.single_instance("TesserACT-Win-7f3a")
        assert handle is not None and handle.exclusive
        assert kernel32.calls == [("CreateMutexW", None, 0, "Local\\TesserACT-Win-7f3a")]
        handle.close()
        handle.close()
        assert kernel32.calls_of("CloseHandle") == [(0x3C,)]

    def test_second_copy(self, windows):
        kernel32 = windows.library("kernel32", {"CreateMutexW": 0x3C, "CloseHandle": 1})
        windows.last_error = 183                                 # the mutex existed already
        assert osapi.single_instance("TesserACT-Win-7f3a") is None
        assert kernel32.calls_of("CloseHandle") == [(0x3C,)]    # our extra handle must not keep it alive

    def test_a_mutex_we_may_not_open_is_also_a_running_copy(self, windows):
        windows.library("kernel32", {"CreateMutexW": None})
        windows.last_error = 5
        assert osapi.single_instance("x") is None

    def test_a_broken_check_lets_the_program_through(self, windows):
        windows.library("kernel32", {"CreateMutexW": None})
        windows.last_error = 1450
        handle = osapi.single_instance("x")
        assert handle is not None and handle.exclusive is False
        handle.close()

    def test_the_name_is_made_safe(self, windows):
        kernel32 = windows.library("kernel32", {"CreateMutexW": 1, "CloseHandle": 1})
        osapi.single_instance("Global\\odd name/1")
        assert kernel32.calls[0][3] == "Local\\Global_odd_name_1"


class TestWindowsBringToFront:
    def user(self, windows, windows_on_screen, **overrides):
        """``windows_on_screen``: handle -> (visible, owner pid, class, title, minimised)."""
        def enum_windows(callback, parameter):
            for handle in windows_on_screen:
                if not callback(handle, parameter):
                    return 0
            return 1

        def class_name(handle, buffer, size):
            buffer.value = windows_on_screen[handle][2][:size - 1]
            return len(buffer.value)

        def window_text(handle, buffer, size):
            buffer.value = windows_on_screen[handle][3][:size - 1]
            return len(buffer.value)

        def owner(handle, pointer):
            pointer._obj.value = windows_on_screen[handle][1]
            return 1

        behaviours = {"EnumWindows": enum_windows, "GetClassNameW": class_name,
                      "GetWindowTextW": window_text, "GetWindowThreadProcessId": owner,
                      "IsWindowVisible": lambda handle: int(windows_on_screen[handle][0]),
                      "IsIconic": lambda handle: int(windows_on_screen[handle][4]),
                      "ShowWindow": 1, "SetForegroundWindow": 1}
        behaviours.update(overrides)
        return windows.library("user32", behaviours)

    def test_finds_restores_and_raises_the_other_copy(self, windows):
        other = os.getpid() + 1
        user32 = self.user(windows, {
            10: (True, other, "CabinetWClass", "TesserACT-Win", False),          # an Explorer window
            11: (False, other, "TkTopLevel", "TesserACT-Win 0.1.0", False),      # hidden
            12: (True, os.getpid(), "TkTopLevel", "TesserACT-Win 0.1.0", False),  # our own
            13: (True, other, "TkTopLevel", "Some other Tk program", False),
            14: (True, other, "TkTopLevel", "TesserACT-Win 0.1.0 \u2014 Sample 3 of 40", True),
            15: (True, other, "TkTopLevel", "TesserACT-Win never reached", False),
        })
        assert osapi.bring_other_instance_to_front("TesserACT-Win") is True
        assert user32.calls_of("ShowWindow") == [(14, 9)]                         # SW_RESTORE
        assert user32.calls_of("SetForegroundWindow") == [(14,)]
        assert 15 not in [call[0] for call in user32.calls_of("IsWindowVisible")]  # stopped at the find

    def test_a_window_that_is_not_minimised_is_not_restored(self, windows):
        user32 = self.user(windows, {20: (True, 1, "TkTopLevel", "TesserACT-Win", False)},
                           SetForegroundWindow=0)
        assert osapi.bring_other_instance_to_front("TesserACT-Win") is True       # found, even if not raised
        assert user32.calls_of("ShowWindow") == []

    def test_a_title_that_only_contains_the_name_is_the_second_choice(self, windows):
        other = os.getpid() + 1
        user32 = self.user(windows, {
            50: (True, other, "TkTopLevel", "Finished: 38 done, 2 failed - TesserACT-Win", False),
            51: (True, other, "TkTopLevel", "Stop the run?", False),
        })
        assert osapi.bring_other_instance_to_front("TesserACT-Win") is True
        assert user32.calls_of("SetForegroundWindow") == [(50,)]
        user32 = self.user(windows, {
            60: (True, other, "TkTopLevel", "Finished - TesserACT-Win", False),
            61: (True, other, "TkTopLevel", "TesserACT-Win 0.1.0", False),
        })
        osapi._libraries.clear()
        assert osapi.bring_other_instance_to_front("TesserACT-Win") is True
        assert user32.calls_of("SetForegroundWindow") == [(61,)]            # a title that starts with it wins

    def test_nothing_found(self, windows):
        user32 = self.user(windows, {30: (True, 1, "TkTopLevel", "WMLST", False)})
        assert osapi.bring_other_instance_to_front("TesserACT-Win") is False
        assert osapi.bring_other_instance_to_front("") is False
        assert user32.calls_of("SetForegroundWindow") == []

    def test_an_error_inside_the_callback_never_reaches_windows(self, windows, monkeypatch):
        monkeypatch.setattr(osapi, "_RAISE", False)

        def broken(handle):
            raise RuntimeError("boom")

        self.user(windows, {40: (True, 1, "TkTopLevel", "TesserACT-Win", False)}, IsWindowVisible=broken)
        assert osapi.bring_other_instance_to_front("TesserACT-Win") is False


class FakeMsvcrt(types.ModuleType):
    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4

    def __init__(self):
        super().__init__("msvcrt")
        self.calls = []
        self.error = None

    def locking(self, descriptor, mode, count):
        self.calls.append((mode, count, os.lseek(descriptor, 0, os.SEEK_CUR)))
        if self.error is not None and mode != self.LK_UNLCK:
            raise self.error


class TestWindowsFolderLock:
    @pytest.fixture
    def msvcrt(self, windows, monkeypatch):
        fake = FakeMsvcrt()
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        return fake

    def test_one_byte_far_behind_the_text_is_locked_without_waiting(self, tmp_path, msvcrt):
        lock = FolderLock()
        assert lock.acquire(str(tmp_path), "PC-7, anna") is True
        assert msvcrt.calls == [(FakeMsvcrt.LK_NBLCK, 1, 0x40000000)]
        path = tmp_path / osapi.LOCK_FILE_NAME
        assert path.read_bytes() == b"PC-7, anna"                # the text is at the start, outside the lock
        lock.release()
        assert msvcrt.calls[-1] == (FakeMsvcrt.LK_UNLCK, 1, 0x40000000)
        assert not path.exists()                                 # closed first, then deleted

    def test_held_by_someone_else(self, tmp_path, msvcrt):
        (tmp_path / osapi.LOCK_FILE_NAME).write_text("PC-9, piet")
        msvcrt.error = PermissionError(errno.EACCES, "Permission denied")
        lock = FolderLock()
        assert lock.acquire(str(tmp_path), "me") is False
        assert len(msvcrt.calls) == osapi._LOCK_TRIES            # tried again: a reader may have had it
        assert lock.held is False and lock.degraded is False
        assert (tmp_path / osapi.LOCK_FILE_NAME).read_text() == "PC-9, piet"     # their text is untouched
        assert FolderLock.holder_text(str(tmp_path)) == "PC-9, piet"

    def test_a_share_without_locks_lets_the_run_through(self, tmp_path, msvcrt, caplog):
        msvcrt.error = OSError(errno.EINVAL, "Invalid argument")
        lock = FolderLock()
        with caplog.at_level("WARNING", logger="tesseract_win.osapi"):
            assert lock.acquire(str(tmp_path), "me") is True
        assert lock.degraded is True and lock.held is False
        assert "unexpected" in caplog.text
        assert FolderLock.holder_text(str(tmp_path)) == ""

    def test_nobody_holds_it(self, tmp_path, msvcrt):
        (tmp_path / osapi.LOCK_FILE_NAME).write_text("stale")
        assert FolderLock.holder_text(str(tmp_path)) == ""
        assert [call[0] for call in msvcrt.calls] == [FakeMsvcrt.LK_NBLCK, FakeMsvcrt.LK_UNLCK]


class TestWindowsPowerAndMemory:
    @pytest.mark.parametrize("ac,flag,percent,expected", [
        (1, 0, 80, (False, 80, True)),                           # laptop on the charger
        (0, 1, 62, (True, 62, True)),                            # on battery
        (0, 4, 4, (True, 4, True)),                              # on battery, critical
        (1, 8, 100, (False, 100, True)),                         # charging
        (1, 128, 255, (False, None, False)),                     # a desktop: no battery
        (255, 128, 255, (False, None, False)),
        (255, 255, 255, (None, None, None)),                     # nothing known
        (255, 1, 50, (None, 50, True)),
    ])
    def test_power_status(self, windows, ac, flag, percent, expected):
        def fill(buffer):
            status = buffer._obj
            status.ACLineStatus, status.BatteryFlag, status.BatteryLifePercent = ac, flag, percent
            return 1

        windows.library("kernel32", {"GetSystemPowerStatus": fill})
        assert osapi.power_status() == expected

    def test_power_status_unknown_when_the_call_fails(self, windows):
        windows.library("kernel32", {"GetSystemPowerStatus": 0})
        assert osapi.power_status() == (None, None, None)

    def test_memory(self, windows):
        def fill(buffer):
            status = buffer._obj
            assert status.dwLength == 64
            status.ullTotalPhys = 8 * 1024 ** 3
            status.ullAvailPhys = 3 * 1024 ** 3
            return 1

        windows.library("kernel32", {"GlobalMemoryStatusEx": fill})
        assert osapi.total_memory_bytes() == 8 * 1024 ** 3
        assert osapi.available_memory_bytes() == 3 * 1024 ** 3

    def test_memory_unknown_when_the_call_fails(self, windows):
        windows.library("kernel32", {"GlobalMemoryStatusEx": 0})
        assert osapi.total_memory_bytes() is None and osapi.available_memory_bytes() is None


class TestWindowsDrives:
    def kernel(self, windows, drive_types, filesystems=None, labels=None):
        def volume_path(path, buffer, size):
            drive = osapi.ntpath.splitdrive(path)[0]
            if not drive:
                return 0
            buffer.value = drive + "\\"
            return 1

        def volume_information(root, label, label_size, serial, length, flags, name, name_size):
            if (filesystems or {}).get(root) is None:
                return 0
            if name is not None:
                name.value = filesystems[root]
            if label is not None:
                label.value = (labels or {}).get(root, "")
            return 1

        return windows.library("kernel32", {
            "GetVolumePathNameW": volume_path,
            "GetDriveTypeW": lambda root: drive_types.get(root, 1),
            "GetVolumeInformationW": volume_information,
        })

    def test_drive_kind(self, windows):
        kernel32 = self.kernel(windows, {"C:\\": 3, "E:\\": 2, "Z:\\": 4, "D:\\": 5, "R:\\": 6,
                                         "\\\\nas\\share\\": 4})
        assert osapi.drive_kind(r"C:\Users\anna\Results") == "fixed"
        assert osapi.drive_kind(r"E:\Results") == "removable"
        assert osapi.drive_kind(r"Z:\Results") == "network"
        assert osapi.drive_kind(r"D:\disc") == "removable"
        assert osapi.drive_kind(r"R:\ramdisk") == "fixed"
        assert osapi.drive_kind(r"\\nas\share\Results") == "network"
        assert osapi.drive_kind(r"Q:\nothing here") == "unknown"
        assert kernel32.calls_of("GetDriveTypeW")[0] == ("C:\\",)        # the root, with its backslash

    def test_a_share_is_a_network_drive_even_if_windows_does_not_say_so(self, windows):
        self.kernel(windows, {})
        assert osapi.drive_kind(r"\\server\data\run") == "network"
        assert osapi.drive_kind(r"\\?\UNC\server\data\run") == "network"
        assert osapi.drive_kind("//server/data/run") == "network"
        assert osapi.drive_kind(r"\\?\Q:\long\path") == "unknown"
        assert osapi.drive_kind(r"\\.\PhysicalDrive0") == "unknown"

    def test_filesystem_name(self, windows):
        self.kernel(windows, {}, {"C:\\": "NTFS", "E:\\": "FAT32", "F:\\": "exFAT"})
        assert osapi.filesystem_name(r"C:\x") == "NTFS"
        assert osapi.filesystem_name(r"E:\x\y") == "FAT32"
        assert osapi.filesystem_name(r"F:\x") == "exFAT"
        assert osapi.filesystem_name(r"Q:\x") == ""

    def test_google_drive_is_known_by_its_volume_label(self, windows, monkeypatch):
        for variable in osapi._ONEDRIVE_VARIABLES:
            monkeypatch.delenv(variable, raising=False)
        self.kernel(windows, {}, {"G:\\": "FAT32", "C:\\": "NTFS"}, {"G:\\": "Google Drive", "C:\\": "Windows"})
        assert osapi.synced_folder_name(r"G:\Shared drives\Lab\run") == "Google Drive"
        assert osapi.synced_folder_name(r"C:\Results") == ""

    @pytest.mark.parametrize("attributes,expected", [
        (0x00000020, False),                                     # an ordinary file
        (0x00400020, True),                                      # OneDrive: content fetched on access
        (0x00001020, True),                                      # offline
        (0x00040010, True),                                      # a folder that is fetched on open
        (0x00080020, False),                                     # "always keep on this device"
        (0xFFFFFFFF, False),                                     # no such file
    ])
    def test_online_only(self, windows, attributes, expected):
        kernel32 = windows.library("kernel32", {"GetFileAttributesW": attributes})
        assert osapi.is_online_only(r"C:\Users\anna\OneDrive\reads_R1.fastq.gz") is expected
        assert kernel32.names() == ["GetFileAttributesW"]        # attributes only: the file is not opened

    def test_code_page(self, windows):
        windows.library("kernel32", {"GetACP": 1252})
        assert osapi.ansi_code_page() == 1252


class FakeRegistry(types.ModuleType):
    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, values):
        super().__init__("winreg")
        self.values = values
        self.opened = []

    def OpenKey(self, root, path):
        self.opened.append((root, path))
        if self.values is None:
            raise FileNotFoundError(2, "no such key")
        registry = self

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        Key.registry = registry
        return Key()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(2, "no such value")
        return self.values[name], 4


class TestWindowsAppearance:
    @pytest.mark.parametrize("values,expected", [
        ({"AppsUseLightTheme": 0}, True), ({"AppsUseLightTheme": 1}, False),
        ({"SystemUsesLightTheme": 0}, False),                    # the value is missing: light
        (None, False),                                           # the key is missing (old build): light
    ])
    def test_dark_mode(self, windows, monkeypatch, values, expected):
        monkeypatch.delenv(osapi.THEME_VARIABLE, raising=False)
        registry = FakeRegistry(values)
        monkeypatch.setitem(sys.modules, "winreg", registry)
        assert osapi.is_dark_mode() is expected
        assert registry.opened == [("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")]

    @pytest.mark.parametrize("flags,expected", [(0x1, True), (0x7F, True), (0x7E, False), (0, False)])
    def test_high_contrast(self, windows, flags, expected):
        def parameters(action, size, buffer, update):
            assert (action, size, update) == (0x42, 16, 0) and buffer._obj.cbSize == 16
            buffer._obj.dwFlags = flags
            return 1

        windows.library("user32", {"SystemParametersInfoW": parameters})
        assert osapi.is_high_contrast() is expected

    def test_high_contrast_unknown_is_off(self, windows):
        windows.library("user32", {"SystemParametersInfoW": 0})
        assert osapi.is_high_contrast() is False


class TestWindowsOpening:
    def test_open_path_uses_the_shell_with_the_plain_path(self, windows, tmp_path, monkeypatch):
        opened = []
        monkeypatch.setattr(os, "startfile", opened.append, raising=False)
        report = tmp_path / "report #1 100% \u00e9.html"
        report.write_text("x")
        assert osapi.open_path(str(report)) is True
        assert opened == [osapi.ntpath.normpath(str(report))]
        assert osapi.open_path(str(tmp_path / "missing.html")) is False
        assert len(opened) == 1

    def test_open_path_when_nothing_opens_that_kind_of_file(self, windows, tmp_path, monkeypatch):
        def no_program(path):
            error = OSError(errno.EINVAL, "No application is associated with the specified file")
            error.winerror = 1155
            raise error

        monkeypatch.setattr(os, "startfile", no_program, raising=False)
        graph = tmp_path / "assembly_graph.gfa"
        graph.write_text("x")
        assert osapi.open_path(str(graph)) is False

    def shell(self, windows, select_result=0, item=0xABC, initialised=1):
        shell32 = windows.library("shell32", {"ILCreateFromPathW": item,
                                              "SHOpenFolderAndSelectItems": select_result, "ILFree": None})
        ole32 = windows.library("ole32", {"CoInitializeEx": initialised, "CoUninitialize": None})
        return shell32, ole32

    def test_reveal_selects_the_file_in_explorer(self, windows, tmp_path, monkeypatch):
        monkeypatch.setattr(osapi.subprocess, "Popen", lambda *a, **k: pytest.fail("no fallback expected"))
        shell32, ole32 = self.shell(windows)
        target = tmp_path / "contigs.fasta"
        target.write_text(">x\nACGT\n")
        assert osapi.reveal(str(target)) is True
        full = osapi.ntpath.normpath(str(target))
        assert shell32.calls == [("ILCreateFromPathW", full),
                                 ("SHOpenFolderAndSelectItems", 0xABC, 0, None, 0), ("ILFree", 0xABC)]
        assert ole32.names() == ["CoInitializeEx", "CoUninitialize"]          # balanced
        assert ole32.calls[0] == ("CoInitializeEx", None, 0x2)

    def test_reveal_does_not_uninitialise_what_it_did_not_initialise(self, windows, tmp_path, monkeypatch):
        monkeypatch.setattr(osapi.subprocess, "Popen", lambda *a, **k: pytest.fail("no fallback expected"))
        _, ole32 = self.shell(windows, initialised=-2147417850)               # RPC_E_CHANGED_MODE
        assert osapi.reveal(str(tmp_path)) is True
        assert ole32.names() == ["CoInitializeEx"]

    @pytest.mark.parametrize("item,result", [(0xABC, -2147467259), (None, 0)])
    def test_reveal_falls_back_to_explorer_itself(self, windows, tmp_path, monkeypatch, item, result):
        started = []

        class FakeProcess:
            def __init__(self, command, **options):
                started.append((command, options))

            def wait(self):
                return 1                                         # explorer answers 1 even when it worked

        monkeypatch.setattr(osapi.subprocess, "Popen", FakeProcess)
        shell32, _ = self.shell(windows, select_result=result, item=item)
        target = tmp_path / "a b.txt"
        target.write_text("x")
        assert osapi.reveal(str(target)) is True
        command, options = started[0]
        assert command == 'explorer.exe /select,"%s"' % osapi.ntpath.normpath(str(target))
        assert "start_new_session" not in options and "creationflags" not in options
        assert ("ILFree", 0xABC) in shell32.calls or item is None            # the item is always freed


class TestWindowsDesktop:
    def test_windows_is_asked_where_the_desktop_is(self, windows, tmp_path, monkeypatch):
        moved = tmp_path / "OneDrive" / "Desktop"
        moved.mkdir(parents=True)
        text = ctypes.create_unicode_buffer(str(moved))            # stands in for memory the shell allocated
        seen = {}

        def known_folder(identifier, flags, token, out):
            seen.update(guid=bytes(identifier._obj), flags=flags, token=token)
            out._obj.value = ctypes.addressof(text)
            return 0

        shell32 = windows.library("shell32", {"SHGetKnownFolderPath": known_folder})
        ole32 = windows.library("ole32", {"CoTaskMemFree": None})
        assert osapi.desktop_dir() == str(moved)
        assert uuid.UUID(bytes_le=seen["guid"]) == uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
        assert (seen["flags"], seen["token"]) == (0, None)
        assert shell32.names() == ["SHGetKnownFolderPath"]
        freed = ole32.calls_of("CoTaskMemFree")
        assert len(freed) == 1 and freed[0][0].value == ctypes.addressof(text)     # freed, once

    def test_the_home_folder_is_the_fallback(self, windows, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        shell32 = windows.library("shell32", {"SHGetKnownFolderPath": -2147024894})      # not found
        ole32 = windows.library("ole32", {"CoTaskMemFree": None})
        assert osapi.desktop_dir() == str(tmp_path)
        assert ole32.calls == [] and len(shell32.calls) == 1
        (tmp_path / "Desktop").mkdir()
        assert osapi.desktop_dir() == str(tmp_path / "Desktop")


class FakeTkRoot:
    def __init__(self, frame="0x2a0f6c"):
        self.frame = frame

    def wm_frame(self):
        return self.frame


class TestWindowsWindow:
    def test_flash_taskbar(self, windows):
        seen = {}

        def flash(buffer):
            info = buffer._obj
            seen.update(size=info.cbSize, window=info.hwnd, flags=info.dwFlags, count=info.uCount)
            return 0

        user32 = windows.library("user32", {"GetForegroundWindow": 0x999, "FlashWindowEx": flash})
        assert osapi.flash_taskbar(FakeTkRoot("0x2a0f6c")) is True
        assert seen == {"size": 32, "window": 0x2A0F6C, "flags": 14, "count": 0}   # the frame, tray, until looked at
        assert user32.names() == ["GetForegroundWindow", "FlashWindowEx"]

    def test_no_flash_when_the_window_is_in_front(self, windows):
        user32 = windows.library("user32", {"GetForegroundWindow": 0x2A0F6C})
        assert osapi.flash_taskbar(FakeTkRoot("0x2a0f6c")) is False
        assert "FlashWindowEx" not in user32.names()

    def test_flash_with_a_broken_window_is_only_false(self, windows, monkeypatch):
        monkeypatch.setattr(osapi, "_RAISE", False)
        windows.library("user32")
        assert osapi.flash_taskbar(FakeTkRoot("not a handle")) is False

    @pytest.mark.parametrize("build,order", [(19045, [20]), (22631, [20]), (18362, [19])])
    def test_dark_titlebar(self, windows, monkeypatch, build, order):
        monkeypatch.setattr(sys, "getwindowsversion",
                            lambda: types.SimpleNamespace(build=build, platform_version=(10, 0, build)),
                            raising=False)
        values = []

        def set_attribute(window, attribute, pointer, size):
            values.append((attribute, pointer._obj.value, size))
            return 0

        dwmapi = windows.library("dwmapi", {"DwmSetWindowAttribute": set_attribute})
        user32 = windows.library("user32", {"SetWindowPos": 1})
        assert osapi.set_dark_titlebar(FakeTkRoot("0x10"), True) is True
        assert osapi.set_dark_titlebar(FakeTkRoot("0x10"), False) is True
        assert values == [(order[0], 1, 4), (order[0], 0, 4)]
        assert dwmapi.calls[0][1] == 0x10
        assert user32.calls_of("SetWindowPos") == [(0x10, None, 0, 0, 0, 0, 0x37)] * 2    # frame changed, nothing else

    def test_work_area_of_the_monitor_under_a_point(self, windows):
        seen = {}

        def monitor_from_point(point, flags):
            seen.update(x=point.x, y=point.y, flags=flags)
            return 0x10001

        def monitor_info(monitor, buffer):
            info = buffer._obj
            assert monitor == 0x10001 and info.cbSize == 40
            info.rcMonitor.left, info.rcMonitor.right = 1920, 4480
            info.rcMonitor.top, info.rcMonitor.bottom = -200, 1240
            info.rcWork.left, info.rcWork.right = 1920, 4480
            info.rcWork.top, info.rcWork.bottom = -200, 1192            # the taskbar is left out
            return 1

        windows.library("user32", {"MonitorFromPoint": monitor_from_point, "GetMonitorInfoW": monitor_info})
        assert osapi.work_area_at(2500, -50) == (1920, -200, 4480, 1192)
        assert seen == {"x": 2500, "y": -50, "flags": 2}               # the nearest monitor

    @pytest.mark.parametrize("monitor,answered", [(None, 1), (0x10001, 0)])
    def test_work_area_unknown(self, windows, monitor, answered):
        windows.library("user32", {"MonitorFromPoint": monitor, "GetMonitorInfoW": answered})
        assert osapi.work_area_at(10, 10) is None

    def test_dark_titlebar_tries_the_other_number_then_gives_up(self, windows, monkeypatch):
        monkeypatch.setattr(sys, "getwindowsversion",
                            lambda: types.SimpleNamespace(build=19045, platform_version=(10, 0, 19045)),
                            raising=False)
        tried = []

        def refuse(window, attribute, pointer, size):
            tried.append(attribute)
            return -2147024809

        windows.library("dwmapi", {"DwmSetWindowAttribute": refuse})
        user32 = windows.library("user32")
        assert osapi.set_dark_titlebar(FakeTkRoot(), True) is False
        assert tried == [20, 19] and user32.calls == []


class TestDegradingOnWindows:
    """Without ``_RAISE`` a failing library must give the harmless answer, and say so in the log."""

    def test_every_function_survives_missing_libraries(self, windows, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(osapi, "_RAISE", False)
        monkeypatch.setattr(osapi, "_reported", set())
        monkeypatch.delenv(osapi.THEME_VARIABLE, raising=False)
        monkeypatch.delitem(sys.modules, "winreg", raising=False)
        monkeypatch.setattr(osapi.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
        windows.unloadable.update(osapi._PROTOTYPES)
        with caplog.at_level("WARNING", logger="tesseract_win.osapi"):
            assert osapi.set_process_defaults() == {"error_mode": False, "dll_directory": False,
                                                    "dpi_aware": False}
            group = ProcessGroup()
            assert group._job is None
            assert group.cpu_seconds() is None and group.memory_bytes() == 0
            assert group.peak_memory_bytes() == 0
            process = fake_process()
            assert group.add(process) is False
            group.kill()
            assert process.killed == 1                           # the last resort: Popen.kill()
            group.close()
            assert osapi.keep_awake(True) is False
            handle = osapi.single_instance("x")
            assert handle is not None and handle.exclusive is False
            assert osapi.bring_other_instance_to_front("TesserACT-Win") is False
            assert osapi.power_status() == (None, None, None)
            assert osapi.total_memory_bytes() is None and osapi.available_memory_bytes() is None
            assert osapi.drive_kind(r"C:\x") == "unknown"
            assert osapi.drive_kind(r"\\server\share\x") == "network"
            assert osapi.filesystem_name(r"C:\x") == ""
            assert osapi.synced_folder_name(r"C:\x") == ""
            assert osapi.is_online_only(r"C:\x") is False
            assert osapi.ansi_code_page() == 0
            assert osapi.is_dark_mode() is False
            assert osapi.is_high_contrast() is False
            assert osapi.reveal(str(tmp_path)) is False
            assert os.path.isdir(osapi.desktop_dir())
            assert osapi.flash_taskbar(FakeTkRoot()) is False
            assert osapi.set_dark_titlebar(FakeTkRoot(), True) is False
            assert osapi.work_area_at(5, 5) is None
        assert "carrying on without it" in caplog.text

    def test_failures_are_logged_loudly_once(self, windows, monkeypatch, caplog):
        monkeypatch.setattr(osapi, "_RAISE", False)
        monkeypatch.setattr(osapi, "_reported", set())
        windows.unloadable.add("kernel32")
        with caplog.at_level("DEBUG", logger="tesseract_win.osapi"):
            for _ in range(5):
                assert osapi.ansi_code_page() == 0
        levels = [record.levelname for record in caplog.records if "ansi_code_page" in record.getMessage()]
        assert levels == ["WARNING", "DEBUG", "DEBUG", "DEBUG", "DEBUG"]
