# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Every Windows-specific call, behind functions that also work on Linux.

The rest of the program never tests the platform: it calls the functions of this module
and gets a sensible answer everywhere. On Windows they use the system libraries through
``ctypes`` (every function with a declared return type and argument types, loaded with
``use_last_error=True``). A library or function that is missing, or a call that fails, is
written to the log and the function falls back to a harmless answer. Nothing here raises
into the caller, and nothing here builds a sentence for the screen: the answers are
enums, numbers and short facts.

Rules that callers must keep:

* :func:`set_process_defaults` runs once, before any window exists.
* :func:`keep_awake`, :func:`flash_taskbar`, :func:`set_dark_titlebar` and
  :func:`work_area_at` are called from the main thread.
* An engine is started with ``ProcessGroup.popen_kwargs()``, added with
  ``ProcessGroup.add()``, and waited for by the thread that started it.
"""

from __future__ import annotations

import atexit
import ctypes
import enum
import errno
import importlib
import logging
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

__all__ = [
    "IS_WINDOWS", "ExitKind", "STOP_EXIT_CODE", "classify_start_error", "classify_exit",
    "set_process_defaults", "no_window_kwargs", "ProcessGroup",
    "keep_awake", "single_instance", "InstanceHandle", "bring_other_instance_to_front",
    "FolderLock", "LOCK_FILE_NAME", "UNKNOWN_HOLDER",
    "power_status", "total_memory_bytes", "available_memory_bytes",
    "drive_kind", "filesystem_name", "synced_folder_name", "is_online_only",
    "windows_build", "ansi_code_page", "path_fits_system_encoding", "UTF8_BUILD",
    "is_dark_mode", "is_high_contrast", "THEME_VARIABLE", "open_path", "reveal", "desktop_dir",
    "flash_taskbar", "set_dark_titlebar", "work_area_at",
]

log = logging.getLogger(__name__)

#: True on Windows. Only this module looks at it.
IS_WINDOWS = os.name == "nt"

#: Tests switch this on so that a mistake in a Windows code path cannot hide behind the fallback.
_RAISE = False

_reported: Set[str] = set()


def _failed(what: str) -> None:
    """Log the exception being handled: the first time with its traceback, afterwards quietly."""
    if _RAISE:
        raise                                    # noqa: PLE0704 - re-raises the caller's exception
    if what in _reported:
        log.debug("%s failed again", what, exc_info=True)
    else:
        _reported.add(what)
        log.warning("%s failed; carrying on without it", what, exc_info=True)


# ============================================================================ exit kinds

class ExitKind(str, enum.Enum):
    """How an engine ended, or why it never ran. The sentences are written elsewhere."""
    OK = "ok"                              # exit code 0
    ENGINE_ERROR = "engine_error"          # the engine reported an error of its own (exit 1 or 2)
    STOPPED = "stopped"                    # ended on request: by this program or from outside
    CRASH = "crash"                        # a fault inside the engine
    OUT_OF_MEMORY = "out_of_memory"        # the engine's own limit, or the PC had none left
    COULD_NOT_START = "could_not_start"    # missing, damaged, or a part it needs is missing
    BLOCKED = "blocked"                    # refused by Windows: antivirus, policy, no permission


#: Exit code this program gives to the engines it ends itself (Windows; Linux uses SIGKILL).
STOP_EXIT_CODE = 0x54570001

# Windows error codes of a failed start (``OSError.winerror``).
_WINERROR_BLOCKED = {
    5,       # ERROR_ACCESS_DENIED
    32,      # ERROR_SHARING_VIOLATION: something (a scanner) holds the file
    33,      # ERROR_LOCK_VIOLATION
    225,     # ERROR_VIRUS_INFECTED
    226,     # ERROR_VIRUS_DELETED
    740,     # ERROR_ELEVATION_REQUIRED
    1260,    # ERROR_ACCESS_DISABLED_BY_POLICY
    1920,    # ERROR_CANT_ACCESS_FILE
    4551,    # ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION (application control)
}
_WINERROR_MEMORY = {
    8,       # ERROR_NOT_ENOUGH_MEMORY
    14,      # ERROR_OUTOFMEMORY
    1450,    # ERROR_NO_SYSTEM_RESOURCES
    1455,    # ERROR_COMMITMENT_LIMIT
}

# Windows exit codes that are status codes (0x80000000 and above).
_STATUS_COULD_NOT_START = {
    0xC0000135,   # STATUS_DLL_NOT_FOUND
    0xC0000142,   # STATUS_DLL_INIT_FAILED
    0xC0000139,   # STATUS_ENTRYPOINT_NOT_FOUND
    0xC000007B,   # STATUS_INVALID_IMAGE_FORMAT
}
_STATUS_BLOCKED = {
    0xC0000022,   # STATUS_ACCESS_DENIED
    0xC0000428,   # STATUS_INVALID_IMAGE_HASH (code integrity)
}
_STATUS_MEMORY = {
    0xC0000017,   # STATUS_NO_MEMORY
    0xC000012D,   # STATUS_COMMITMENT_LIMIT
}
_STATUS_STOPPED = {
    0xC000013A,   # STATUS_CONTROL_C_EXIT: ended from outside (log-off, shutdown, Ctrl+C)
}

# Linux signal numbers (x86 and ARM). Written out because ``signal.SIGKILL`` does not exist on Windows.
_SIGHUP, _SIGINT, _SIGQUIT, _SIGKILL, _SIGTERM = 1, 2, 3, 9, 15
_SIGNALS_STOPPED = {_SIGHUP, _SIGINT, _SIGQUIT, _SIGTERM}

_MEMORY_MARKS = ("bad_alloc", "out of memory", "ran out of the memory budget",
                 "cannot allocate memory")


def _mentions_memory(log_tail: str) -> bool:
    low = (log_tail or "").lower()
    return any(mark in low for mark in _MEMORY_MARKS)


def classify_start_error(error: BaseException) -> ExitKind:
    """Why ``subprocess.Popen`` raised: COULD_NOT_START, BLOCKED or OUT_OF_MEMORY.

    Reads ``winerror`` when the error carries one (Windows) and ``errno`` otherwise.
    """
    winerror = getattr(error, "winerror", None)
    if isinstance(winerror, int) and winerror:
        if winerror in _WINERROR_BLOCKED:
            return ExitKind.BLOCKED
        if winerror in _WINERROR_MEMORY:
            return ExitKind.OUT_OF_MEMORY
        return ExitKind.COULD_NOT_START      # 2, 3, 193, 216, 267 and anything unknown
    code = getattr(error, "errno", None)
    if code in (errno.EACCES, errno.EPERM, errno.ETXTBSY):
        return ExitKind.BLOCKED
    if code in (errno.ENOMEM, errno.EAGAIN):
        return ExitKind.OUT_OF_MEMORY
    return ExitKind.COULD_NOT_START


def classify_exit(returncode: Optional[int], log_tail: str = "") -> ExitKind:
    """What an exit code means. ``log_tail`` is the last lines the engine printed.

    Works for the codes of both systems on either of them: Windows status codes arrive as
    large positive numbers (0xC0000005 is 3221225477), Linux signals as negative ones.
    A process this program ended itself shows up as :data:`STOP_EXIT_CODE` on Windows; on
    Linux it shows up as signal 9, which this function must read as "out of memory"
    (the kernel's out-of-memory killer is the usual sender), so the code that did the
    stopping reports STOPPED itself (``engine_proc.run_engine`` does).
    """
    if returncode is None:
        return ExitKind.CRASH
    code = int(returncode)
    if code == 0:
        return ExitKind.OK
    if code == STOP_EXIT_CODE:
        return ExitKind.STOPPED
    memory = _mentions_memory(log_tail)

    signal_number = 0
    if -255 <= code < 0:
        signal_number = -code
    elif 128 < code < 160:                   # the same signal, reported through a shell
        signal_number = code - 128
    if signal_number:
        if signal_number in _SIGNALS_STOPPED:
            return ExitKind.STOPPED
        if signal_number == _SIGKILL or memory:
            return ExitKind.OUT_OF_MEMORY
        return ExitKind.CRASH

    status = code & 0xFFFFFFFF
    if status >= 0x80000000:
        if status in _STATUS_STOPPED:
            return ExitKind.STOPPED
        if status in _STATUS_COULD_NOT_START:
            return ExitKind.COULD_NOT_START
        if status in _STATUS_BLOCKED:
            return ExitKind.BLOCKED
        if status in _STATUS_MEMORY or memory:
            return ExitKind.OUT_OF_MEMORY
        return ExitKind.CRASH                # 0xC0000005, 0xC00000FD, 0xC0000409 and the rest

    if memory:
        return ExitKind.OUT_OF_MEMORY
    if code == 3:                            # abort() of the Windows C runtime
        return ExitKind.CRASH
    return ExitKind.ENGINE_ERROR


# ============================================================================ ctypes layer
# Fixed-width types, so that the structures have their Windows sizes on Linux too and the
# tests can check them there. (``ctypes.wintypes.DWORD`` is 8 bytes on 64-bit Linux.)

_BOOL = ctypes.c_int
_BYTE = ctypes.c_ubyte
_INT = ctypes.c_int
_UINT = ctypes.c_uint32
_DWORD = ctypes.c_uint32
_HRESULT = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_HWND = ctypes.c_void_p
_LPVOID = ctypes.c_void_p
_LPCWSTR = ctypes.c_wchar_p
_LPWSTR = ctypes.c_wchar_p
_SIZE_T = ctypes.c_size_t
_ULONG_PTR = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_LARGE_INTEGER = ctypes.c_int64
_ULONGLONG = ctypes.c_uint64
_LPDWORD = ctypes.POINTER(_DWORD)


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", _ULONGLONG), ("WriteOperationCount", _ULONGLONG),
                ("OtherOperationCount", _ULONGLONG), ("ReadTransferCount", _ULONGLONG),
                ("WriteTransferCount", _ULONGLONG), ("OtherTransferCount", _ULONGLONG)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", _LARGE_INTEGER),
                ("PerJobUserTimeLimit", _LARGE_INTEGER),
                ("LimitFlags", _DWORD),
                ("MinimumWorkingSetSize", _SIZE_T),
                ("MaximumWorkingSetSize", _SIZE_T),
                ("ActiveProcessLimit", _DWORD),
                ("Affinity", _ULONG_PTR),
                ("PriorityClass", _DWORD),
                ("SchedulingClass", _DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    """144 bytes on 64-bit Windows. Any other size makes ``SetInformationJobObject`` fail (error 24)."""
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", _SIZE_T),
                ("JobMemoryLimit", _SIZE_T),
                ("PeakProcessMemoryUsed", _SIZE_T),
                ("PeakJobMemoryUsed", _SIZE_T)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", _LARGE_INTEGER), ("TotalKernelTime", _LARGE_INTEGER),
                ("ThisPeriodTotalUserTime", _LARGE_INTEGER),
                ("ThisPeriodTotalKernelTime", _LARGE_INTEGER),
                ("TotalPageFaultCount", _DWORD), ("TotalProcesses", _DWORD),
                ("ActiveProcesses", _DWORD), ("TotalTerminatedProcesses", _DWORD)]


_PID_SLOTS = 256


class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    _fields_ = [("NumberOfAssignedProcesses", _DWORD), ("NumberOfProcessIdsInList", _DWORD),
                ("ProcessIdList", _ULONG_PTR * _PID_SLOTS)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", _DWORD), ("PageFaultCount", _DWORD),
                ("PeakWorkingSetSize", _SIZE_T), ("WorkingSetSize", _SIZE_T),
                ("QuotaPeakPagedPoolUsage", _SIZE_T), ("QuotaPagedPoolUsage", _SIZE_T),
                ("QuotaPeakNonPagedPoolUsage", _SIZE_T), ("QuotaNonPagedPoolUsage", _SIZE_T),
                ("PagefileUsage", _SIZE_T), ("PeakPagefileUsage", _SIZE_T)]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", _DWORD), ("dwHighDateTime", _DWORD)]


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", _DWORD), ("ControlMask", _DWORD), ("StateMask", _DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", _DWORD), ("dwMemoryLoad", _DWORD),
                ("ullTotalPhys", _ULONGLONG), ("ullAvailPhys", _ULONGLONG),
                ("ullTotalPageFile", _ULONGLONG), ("ullAvailPageFile", _ULONGLONG),
                ("ullTotalVirtual", _ULONGLONG), ("ullAvailVirtual", _ULONGLONG),
                ("ullAvailExtendedVirtual", _ULONGLONG)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", _BYTE), ("BatteryFlag", _BYTE),
                ("BatteryLifePercent", _BYTE), ("SystemStatusFlag", _BYTE),
                ("BatteryLifeTime", _DWORD), ("BatteryFullLifeTime", _DWORD)]


class FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", _UINT), ("hwnd", _HWND), ("dwFlags", _DWORD),
                ("uCount", _UINT), ("dwTimeout", _DWORD)]


class HIGHCONTRASTW(ctypes.Structure):
    _fields_ = [("cbSize", _UINT), ("dwFlags", _DWORD), ("lpszDefaultScheme", _LPVOID)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32),
                ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", _DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", _DWORD)]


#: Callback of ``EnumWindows``. One calling convention exists on 64-bit Windows, so the
#: plain C type used on Linux (for the tests) describes the same thing.
_WNDENUMPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(_BOOL, _HWND, _LPARAM)

# Every Windows function this module calls: library -> name -> (return type, argument types).
# Handles are pointers (64-bit): with ctypes' default ``int`` return type they would be cut off.
_PROTOTYPES: Dict[str, Dict[str, Tuple[Any, List[Any]]]] = {
    "kernel32": {
        "SetErrorMode": (_UINT, [_UINT]),
        "SetDllDirectoryW": (_BOOL, [_LPCWSTR]),
        "CreateJobObjectW": (_HANDLE, [_LPVOID, _LPCWSTR]),
        "SetInformationJobObject": (_BOOL, [_HANDLE, _INT, _LPVOID, _DWORD]),
        "QueryInformationJobObject": (_BOOL, [_HANDLE, _INT, _LPVOID, _DWORD, _LPDWORD]),
        "AssignProcessToJobObject": (_BOOL, [_HANDLE, _HANDLE]),
        "TerminateJobObject": (_BOOL, [_HANDLE, _UINT]),
        "OpenProcess": (_HANDLE, [_DWORD, _BOOL, _DWORD]),
        "TerminateProcess": (_BOOL, [_HANDLE, _UINT]),
        "SetProcessInformation": (_BOOL, [_HANDLE, _INT, _LPVOID, _DWORD]),
        "GetProcessTimes": (_BOOL, [_HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                    ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]),
        "K32GetProcessMemoryInfo": (_BOOL, [_HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), _DWORD]),
        "CloseHandle": (_BOOL, [_HANDLE]),
        "SetThreadExecutionState": (_UINT, [_UINT]),
        "CreateMutexW": (_HANDLE, [_LPVOID, _BOOL, _LPCWSTR]),
        "GetSystemPowerStatus": (_BOOL, [ctypes.POINTER(SYSTEM_POWER_STATUS)]),
        "GlobalMemoryStatusEx": (_BOOL, [ctypes.POINTER(MEMORYSTATUSEX)]),
        "GetDriveTypeW": (_UINT, [_LPCWSTR]),
        "GetVolumePathNameW": (_BOOL, [_LPCWSTR, _LPWSTR, _DWORD]),
        "GetVolumeInformationW": (_BOOL, [_LPCWSTR, _LPWSTR, _DWORD, _LPDWORD, _LPDWORD,
                                          _LPDWORD, _LPWSTR, _DWORD]),
        "GetFileAttributesW": (_DWORD, [_LPCWSTR]),
        "GetACP": (_UINT, []),
    },
    "user32": {
        "SetProcessDPIAware": (_BOOL, []),
        "EnumWindows": (_BOOL, [_WNDENUMPROC, _LPARAM]),
        "GetClassNameW": (_INT, [_HWND, _LPWSTR, _INT]),
        "GetWindowTextW": (_INT, [_HWND, _LPWSTR, _INT]),
        "GetWindowThreadProcessId": (_DWORD, [_HWND, _LPDWORD]),
        "IsWindowVisible": (_BOOL, [_HWND]),
        "IsIconic": (_BOOL, [_HWND]),
        "ShowWindow": (_BOOL, [_HWND, _INT]),
        "SetForegroundWindow": (_BOOL, [_HWND]),
        "GetForegroundWindow": (_HWND, []),
        "FlashWindowEx": (_BOOL, [ctypes.POINTER(FLASHWINFO)]),
        "SystemParametersInfoW": (_BOOL, [_UINT, _UINT, _LPVOID, _UINT]),
        "SetWindowPos": (_BOOL, [_HWND, _HWND, _INT, _INT, _INT, _INT, _UINT]),
        "MonitorFromPoint": (_HANDLE, [POINT, _DWORD]),          # the point is passed by value
        "GetMonitorInfoW": (_BOOL, [_HANDLE, ctypes.POINTER(MONITORINFO)]),
    },
    "shcore": {
        "SetProcessDpiAwareness": (_HRESULT, [_INT]),
    },
    "dwmapi": {
        "DwmSetWindowAttribute": (_HRESULT, [_HWND, _DWORD, _LPVOID, _DWORD]),
    },
    "shell32": {
        "ILCreateFromPathW": (_LPVOID, [_LPCWSTR]),
        "SHOpenFolderAndSelectItems": (_HRESULT, [_LPVOID, _UINT, _LPVOID, _DWORD]),
        "ILFree": (None, [_LPVOID]),
        "SHGetKnownFolderPath": (_HRESULT, [ctypes.POINTER(GUID), _DWORD, _HANDLE,
                                            ctypes.POINTER(ctypes.c_void_p)]),
    },
    "ole32": {
        "CoInitializeEx": (_HRESULT, [_LPVOID, _DWORD]),
        "CoUninitialize": (None, []),
        "CoTaskMemFree": (None, [_LPVOID]),
    },
}

_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800

_libraries: Dict[str, Any] = {}
_libraries_lock = threading.Lock()


def _load_library(name: str) -> Any:
    """Load a Windows system library, from the System32 folder only. (The tests replace this.)"""
    loader = ctypes.WinDLL                           # type: ignore[attr-defined]
    try:
        return loader(name, use_last_error=True, winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
    except (OSError, TypeError):
        return loader(name, use_last_error=True)


def _dll(name: str) -> Any:
    """The named library with every prototype of :data:`_PROTOTYPES` declared on it."""
    with _libraries_lock:
        library = _libraries.get(name)
        if library is None:
            library = _load_library(name)
            for function_name, (restype, argtypes) in _PROTOTYPES[name].items():
                try:
                    function = getattr(library, function_name)
                except AttributeError:
                    log.info("%s.%s does not exist on this Windows", name, function_name)
                    continue
                function.restype = restype
                function.argtypes = list(argtypes)
            _libraries[name] = library
        return library


def _last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter else 0


def _clear_last_error() -> None:
    setter = getattr(ctypes, "set_last_error", None)
    if setter:
        setter(0)


# ============================================================================ process defaults

_SEM_FAILCRITICALERRORS = 0x0001       # no "there is no disk in the drive" box
_SEM_NOGPFAULTERRORBOX = 0x0002        # no "has stopped working" box
_SEM_NOOPENFILEERRORBOX = 0x8000
_PROCESS_SYSTEM_DPI_AWARE = 1
_S_OK = 0
_S_FALSE = 1
_E_ACCESSDENIED = -2147024891          # 0x80070005 as a signed 32-bit number

_defaults_lock = threading.Lock()
_defaults: Optional[Dict[str, bool]] = None


def set_process_defaults() -> Dict[str, bool]:
    """Set what must hold for the whole life of the program. Call first, before any window.

    On Windows:

    * error mode: a crashing engine, or a drive with no disk in it, must not put up a
      Windows box that waits for a click (an unattended batch would hang). The engines
      inherit the mode.
    * the DLL search directory inherited from the program's starter is cleared, so the
      engines do not inherit it either;
    * the process declares itself aware of the system's display scaling, or Windows
      stretches the window and blurs the text. This only works before the first window.

    Safe to call twice: the second call does nothing. Returns what was achieved (all False
    on Linux, where there is nothing to do).
    """
    global _defaults
    with _defaults_lock:
        if _defaults is not None:
            return dict(_defaults)
        done = {"error_mode": False, "dll_directory": False, "dpi_aware": False}
        if IS_WINDOWS:
            for key, step in (("error_mode", _win_set_error_mode),
                              ("dll_directory", _win_clear_dll_directory),
                              ("dpi_aware", _win_declare_dpi_aware)):
                try:
                    done[key] = bool(step())
                except Exception:
                    _failed("set_process_defaults: %s" % key)
        _defaults = done
        return dict(done)


def _win_set_error_mode() -> bool:
    kernel32 = _dll("kernel32")
    wanted = _SEM_FAILCRITICALERRORS | _SEM_NOGPFAULTERRORBOX | _SEM_NOOPENFILEERRORBOX
    previous = kernel32.SetErrorMode(wanted)
    if previous & ~wanted:                       # keep whatever was set before us
        kernel32.SetErrorMode(previous | wanted)
    return True


def _win_clear_dll_directory() -> bool:
    if _dll("kernel32").SetDllDirectoryW(None):
        return True
    log.warning("SetDllDirectoryW failed (Windows error %d)", _last_error())
    return False


def _win_declare_dpi_aware() -> bool:
    try:
        result = _dll("shcore").SetProcessDpiAwareness(_PROCESS_SYSTEM_DPI_AWARE)
        if result in (_S_OK, _E_ACCESSDENIED):   # "access denied": it was already declared
            return True
        log.info("SetProcessDpiAwareness returned %#x; trying the older call", result & 0xFFFFFFFF)
    except (OSError, AttributeError):
        log.info("shcore.SetProcessDpiAwareness is not available; trying the older call")
    return bool(_dll("user32").SetProcessDPIAware())


# ============================================================================ process group

_CREATE_NO_WINDOW = 0x08000000
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = 1      # information classes
_JOBOBJECT_BASIC_PROCESS_ID_LIST = 3
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 9

_PROCESS_TERMINATE = 0x0001
_PROCESS_VM_READ = 0x0010
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_POWER_THROTTLING = 4                    # information class of SetProcessInformation
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# What the documentation asks for to read a process's memory counters and times.
_PROCESS_MEASURE = _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ
_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_ERROR_MORE_DATA = 234

_PR_SET_PDEATHSIG = 1
_LOW_PRIORITY_NICE = 5
_SAMPLE_REUSE_SECONDS = 0.05


def no_window_kwargs() -> Dict[str, Any]:
    """Extra ``subprocess`` arguments for a helper process that must not flash a console window."""
    return {"creationflags": _CREATE_NO_WINDOW} if IS_WINDOWS else {}


class _ProcStat(NamedTuple):
    pid: int
    state: str          # "Z" is a process that has ended and waits to be collected
    session: int
    ticks: int          # user plus kernel time, in clock ticks
    start: int          # start time; with the pid it names one process for ever
    rss_pages: int


def _read_proc_stat(pid: int) -> Optional[_ProcStat]:
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    close = raw.rfind(b")")                      # the name in brackets may itself hold brackets
    fields = raw[close + 2:].split()
    try:
        return _ProcStat(pid, fields[0].decode("ascii", "replace"), int(fields[3]),
                         int(fields[11]) + int(fields[12]), int(fields[19]), int(fields[21]))
    except (IndexError, ValueError):
        return None


def _read_proc_peak(pid: int) -> int:
    """Largest resident size the process has had (VmHWM), in bytes; 0 if unknown."""
    try:
        with open("/proc/%d/status" % pid, "rb") as handle:
            for line in handle:
                if line.startswith(b"VmHWM:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _session_members(sessions: Set[int]) -> List[_ProcStat]:
    """Every process whose session is one of ``sessions`` (Linux)."""
    if not sessions:
        return []
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    found = []
    for name in names:
        if name.isdigit():
            stat = _read_proc_stat(int(name))
            if stat is not None and stat.session in sessions:
                found.append(stat)
    return found


_prctl: Any = None
_prctl_looked_up = False


def _libc_prctl() -> Any:
    """``prctl`` of the C library on Linux, else None. Looked up in the parent, used in the child."""
    global _prctl, _prctl_looked_up
    if not _prctl_looked_up:
        _prctl_looked_up = True
        if sys.platform.startswith("linux"):
            try:
                function = ctypes.CDLL(None, use_errno=True).prctl
                function.restype = ctypes.c_int
                function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                                     ctypes.c_ulong, ctypes.c_ulong]
                _prctl = function
            except (OSError, AttributeError):
                log.warning("prctl is not available: an engine may outlive this program")
    return _prctl


def _posix_child_setup(low_priority: bool) -> Callable[[], None]:
    """What a new engine does to itself between ``fork`` and ``exec``. Kept as small as possible."""
    prctl = _libc_prctl()
    parent = os.getpid()

    def setup() -> None:
        if prctl is not None:
            prctl(_PR_SET_PDEATHSIG, _SIGKILL, 0, 0, 0)
            if os.getppid() != parent:           # the parent died before the line above
                os._exit(127)
        if low_priority:
            try:
                os.nice(_LOW_PRIORITY_NICE)
            except OSError:
                pass

    return setup


class _Member:
    """A process added on Linux. ``leader``: it leads its own session, so its children can be found."""
    __slots__ = ("popen", "leader")

    def __init__(self, popen: Any, leader: bool) -> None:
        self.popen = popen
        self.leader = leader


_all_groups: "weakref.WeakSet[ProcessGroup]" = weakref.WeakSet()


class ProcessGroup:
    """Every engine this program starts goes in here, so that none can be left running.

    * :meth:`kill` ends every process in the group, and everything those started, at once.
    * If this program dies in any way (closed, crashed, ended in Task Manager), they die
      too.

    Windows: a Job Object with "kill on close". Windows closes the program's handle to the
    job when the program ends, however it ends, and that ends everything in the job. The
    job holds the engines only, never this program: a web browser opened later to show a
    report would otherwise be inside it and would be ended when the program closes.
    An engine is put into the job right after it has started (for well under a millisecond
    it is not in it yet), and Windows is asked not to slow it down to save power.

    Linux: every engine gets a session of its own and is ended with ``killpg``; the kernel
    also sends it a kill signal when the thread that started it ends ("parent-death
    signal"). So start an engine from the thread that will wait for it. That signal
    reaches the engine itself, not processes the engine started; those are ended by
    :meth:`kill` and :meth:`close`. The real engines start none.

    Use::

        process = subprocess.Popen(argv, ..., **group.popen_kwargs(low_priority=True))
        group.add(process)

    One group serves a whole batch. All methods may be called from any thread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._kills = 0
        self._peak = 0
        self._cache: Optional[Tuple[float, Optional[float], int]] = None
        self._own_peak: Dict[int, int] = {}           # pid of an added process -> its largest size
        # Windows
        self._job: Optional[int] = None
        self._loose: List[Any] = []                   # started, but could not be put in the job
        self._loose_cpu: Dict[int, float] = {}
        self._loose_cpu_done = 0.0
        # Linux
        self._members: Dict[int, _Member] = {}        # pid -> process added
        self._cpu_seen: Dict[Tuple[int, int], Tuple[int, int]] = {}   # (pid, start) -> (owner, ticks)
        self._cpu_done_ticks = 0
        if IS_WINDOWS:
            try:
                self._job = _win_create_job()
            except Exception:
                _failed("creating the Job Object")
        _all_groups.add(self)

    def __enter__(self) -> "ProcessGroup":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ starting

    def popen_kwargs(self, low_priority: bool = True) -> Dict[str, Any]:
        """Extra arguments for ``subprocess.Popen`` when starting an engine.

        Windows: no console window, and below-normal priority when asked (never "idle":
        that also lowers disk and memory priority). Linux: a new session, the parent-death
        signal, and a higher nice value when asked.
        """
        if IS_WINDOWS:
            flags = _CREATE_NO_WINDOW
            if low_priority:
                flags |= _BELOW_NORMAL_PRIORITY_CLASS
            return {"creationflags": flags}
        return {"start_new_session": True, "preexec_fn": _posix_child_setup(low_priority)}

    def add(self, popen: Any) -> bool:
        """Take a process just started into the group. True when it is certain to die with it.

        False means the group could only note the process: :meth:`kill`, :meth:`close` and
        the end of the program still end it, a crash of the program might not.
        """
        with self._lock:
            if self._closed:
                log.warning("an engine was added to a closed process group; ending it")
                _end_one_process(popen)
                return False
            try:
                if IS_WINDOWS:
                    return self._win_add(popen)
                return self._posix_add(popen)
            except Exception:
                _failed("adding an engine to the process group")
                if IS_WINDOWS:
                    self._loose.append(popen)
                else:
                    self._members[popen.pid] = _Member(popen, False)
                return False

    def _posix_add(self, popen: Any) -> bool:
        if popen.poll() is not None:
            # Over already. Whatever it may have left behind is found through its session;
            # if there is nothing, the next measurement forgets it.
            self._members[popen.pid] = _Member(popen, True)
            return True
        try:
            leader = os.getsid(popen.pid) == popen.pid
        except OSError:
            leader = False
        if not leader:
            log.warning("an engine was started without popen_kwargs(); its own children are not tracked")
        self._members[popen.pid] = _Member(popen, leader)
        return leader

    def _win_add(self, popen: Any) -> bool:
        kernel32 = _dll("kernel32")
        handle, opened = _win_process_handle(
            popen, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SET_INFORMATION)
        assigned, error = False, 0
        try:
            if handle and self._job:
                assigned = bool(kernel32.AssignProcessToJobObject(self._job, handle))
                error = _last_error()
            if handle:
                _win_full_speed(handle)
        finally:
            if handle and opened:
                kernel32.CloseHandle(handle)
        if assigned:
            return True
        if popen.poll() is not None:
            return True                               # it had ended already: nothing to hold
        if self._job:
            log.warning("the engine could not be put into the Job Object (Windows error %d); "
                        "it will be ended one by one instead", error)
        self._loose.append(popen)
        return False

    # ------------------------------------------------------------------ ending

    @property
    def kill_count(self) -> int:
        """How many times :meth:`kill` has been called (lets a caller see a kill it did not ask for)."""
        return self._kills

    @property
    def closed(self) -> bool:
        """True after :meth:`close`: whatever is added now is ended at once."""
        return self._closed

    def kill(self) -> None:
        """End every process in the group, and everything they started, now. The group stays usable."""
        with self._lock:
            self._kills += 1
            self._cache = None
            try:
                if IS_WINDOWS:
                    self._win_kill()
                else:
                    self._posix_kill()
            except Exception:
                _failed("ending the engines")

    def _posix_kill(self) -> None:
        signal_module = importlib.import_module("signal")
        sessions: Set[int] = set()
        for pid, member in list(self._members.items()):
            if member.popen.poll() is None:           # not collected yet, so the pid is still ours
                try:
                    if member.leader:
                        os.killpg(pid, signal_module.SIGKILL)
                    else:
                        os.kill(pid, signal_module.SIGKILL)
                except OSError:
                    pass
            if member.leader:
                sessions.add(pid)
        # Whatever is left in those sessions: a child of an engine that moved to its own group.
        stats = _session_members(sessions)
        strangers = self._reused_sessions(stats)
        for stat in stats:
            if stat.session in strangers or stat.state == "Z":
                continue
            try:
                os.kill(stat.pid, signal_module.SIGKILL)
            except OSError:
                pass

    def _reused_sessions(self, stats: List[_ProcStat]) -> Set[int]:
        """Sessions whose number now belongs to someone else.

        A session number is the pid of the engine that led it. Once that engine has been
        collected and nothing of its session is left, the kernel may hand the number to a
        new, unrelated process. A living session leader whose engine we have already
        collected is therefore not ours: forget that session, touch nothing in it.
        """
        strangers: Set[int] = set()
        for stat in stats:
            if stat.pid == stat.session and stat.state != "Z":
                member = self._members.get(stat.pid)
                if member is not None and member.popen.returncode is not None:
                    strangers.add(stat.session)
        for session in strangers:
            self._forget(session)
        return strangers

    def _forget(self, owner: int) -> None:
        self._members.pop(owner, None)
        for key, (key_owner, ticks) in list(self._cpu_seen.items()):
            if key_owner == owner:
                self._cpu_done_ticks += ticks
                del self._cpu_seen[key]

    def _win_kill(self) -> None:
        if self._job:
            if not _dll("kernel32").TerminateJobObject(self._job, STOP_EXIT_CODE):
                log.warning("TerminateJobObject failed (Windows error %d)", _last_error())
        for popen in self._loose:
            _end_one_process(popen)

    def close(self) -> None:
        """End everything that is left and give the group up. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self.kill()
            self._closed = True
            waiting = [member.popen for member in self._members.values()] + list(self._loose)
            job, self._job = self._job, None
            if job:
                try:
                    _dll("kernel32").CloseHandle(job)
                except Exception:
                    _failed("closing the Job Object")
        for popen in waiting:                         # collect them, so that no zombie is left
            try:
                popen.wait(timeout=3)
            except Exception:                         # noqa: BLE001 - nothing more can be done
                pass
        with self._lock:
            self._members.clear()
            self._loose = []
        _all_groups.discard(self)

    # ------------------------------------------------------------------ measuring

    def cpu_seconds(self) -> Optional[float]:
        """Processor seconds (user plus kernel) used by the whole group since it was created.

        It only ever grows. None when it cannot be measured; a caller must then not
        conclude that an engine is idle.
        """
        return self._sample()[0]

    def memory_bytes(self) -> int:
        """Memory in use now by every process in the group (resident size); 0 when unknown."""
        return self._sample()[1]

    def peak_memory_bytes(self, popen: Any = None) -> int:
        """The largest memory use seen: of the whole group, or of one added process.

        With ``popen``: the peak of that process itself. Exact on Windows, also after the
        process has ended; on Linux the largest value seen while it ran.
        """
        if popen is not None:
            # Asked when an engine has ended: look afresh, so that an engine that is over
            # is forgotten now and its number can never be taken for ours later.
            with self._lock:
                self._cache = None
        self._sample()
        with self._lock:
            if popen is None:
                return self._peak
            peak = self._own_peak.get(popen.pid, 0)
            if IS_WINDOWS:
                try:
                    peak = max(peak, _win_process_memory(popen.pid)[1])
                except Exception:
                    _failed("reading the peak memory of an engine")
            return peak

    def _sample(self) -> Tuple[Optional[float], int]:
        with self._lock:
            now = time.monotonic()
            if self._cache is not None and 0 <= now - self._cache[0] < _SAMPLE_REUSE_SECONDS:
                return self._cache[1], self._cache[2]
            try:
                cpu, memory = self._win_sample() if IS_WINDOWS else self._posix_sample()
            except Exception:
                _failed("measuring the engines")
                cpu, memory = None, 0
            self._peak = max(self._peak, memory)
            if len(self._own_peak) > 64:              # a long batch: keep the newest only
                for pid in list(self._own_peak)[:-32]:
                    del self._own_peak[pid]
            self._cache = (now, cpu, memory)
            return cpu, memory

    def _posix_sample(self) -> Tuple[Optional[float], int]:
        if not os.path.isdir("/proc"):
            return None, 0
        sessions = {pid for pid, member in self._members.items() if member.leader}
        stats = _session_members(sessions)
        strangers = self._reused_sessions(stats)
        stats = [stat for stat in stats if stat.session not in strangers]
        owners = {stat.pid: stat.session for stat in stats}
        for pid, member in self._members.items():
            if not member.leader and member.popen.returncode is None:   # collected: the pid is not ours
                stat = _read_proc_stat(pid)
                if stat is not None:
                    stats.append(stat)
                    owners[stat.pid] = pid
        page = os.sysconf("SC_PAGE_SIZE")
        alive: Set[int] = set()
        per_owner: Dict[int, int] = {}
        memory = 0
        for stat in stats:
            owner = owners[stat.pid]
            key = (stat.pid, stat.start)
            seen = self._cpu_seen.get(key)
            if seen is None or stat.ticks > seen[1]:
                self._cpu_seen[key] = (owner, stat.ticks)
            alive.add(owner)
            if stat.state == "Z":
                continue
            resident = stat.rss_pages * page
            memory += resident
            per_owner[owner] = per_owner.get(owner, 0) + resident
            self._own_peak[owner] = max(self._own_peak.get(owner, 0), _read_proc_peak(stat.pid))
        for owner, resident in per_owner.items():
            self._own_peak[owner] = max(self._own_peak.get(owner, 0), resident)
            self._peak = max(self._peak, self._own_peak[owner])
        # An engine that has been collected and has left nobody behind is forgotten, so that
        # its number can never be mistaken for ours when the kernel hands it out again.
        for pid, member in list(self._members.items()):
            if pid not in alive and member.popen.poll() is not None:
                self._forget(pid)
        ticks = self._cpu_done_ticks + sum(value[1] for value in self._cpu_seen.values())
        return ticks / float(os.sysconf("SC_CLK_TCK")), memory

    def _win_sample(self) -> Tuple[Optional[float], int]:
        kernel32 = _dll("kernel32")
        cpu: Optional[float] = 0.0
        pids: List[int] = []
        if self._job:
            accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
            if kernel32.QueryInformationJobObject(
                    self._job, _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
                    ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                cpu = (accounting.TotalUserTime + accounting.TotalKernelTime) / 1e7
            else:
                cpu = None
            listing = JOBOBJECT_BASIC_PROCESS_ID_LIST()
            listed = kernel32.QueryInformationJobObject(
                self._job, _JOBOBJECT_BASIC_PROCESS_ID_LIST,
                ctypes.byref(listing), ctypes.sizeof(listing), None)
            if listed or _last_error() == _ERROR_MORE_DATA:
                count = min(int(listing.NumberOfProcessIdsInList), _PID_SLOTS)
                pids = [int(listing.ProcessIdList[index]) for index in range(count)]
        memory = 0
        for pid in pids:
            memory += self._win_note_memory(pid)
        unmeasured = False
        for popen in list(self._loose):
            memory += self._win_note_memory(popen.pid)
            seconds = _win_process_cpu(popen.pid)
            if seconds is not None:
                self._loose_cpu[popen.pid] = max(self._loose_cpu.get(popen.pid, 0.0), seconds)
            if popen.poll() is not None:              # over: keep its total, drop the process
                self._loose_cpu_done += self._loose_cpu.pop(popen.pid, 0.0)
                self._loose.remove(popen)
            elif seconds is None:
                unmeasured = True                     # it runs, and nobody can say how hard it works
        if unmeasured:
            cpu = None                                # "unknown", never a number that looks like "idle"
        if cpu is not None:
            cpu += self._loose_cpu_done + sum(self._loose_cpu.values())
        return cpu, memory

    def _win_note_memory(self, pid: int) -> int:
        working_set, peak = _win_process_memory(pid)
        self._own_peak[pid] = max(self._own_peak.get(pid, 0), peak)
        self._peak = max(self._peak, peak)
        return working_set


def _win_create_job() -> Optional[int]:
    """A Job Object that ends its processes when its last handle is closed; None if it cannot be made."""
    kernel32 = _dll("kernel32")
    job = kernel32.CreateJobObjectW(None, None)      # no name, and not inheritable by the engines
    if not job:
        log.warning("CreateJobObjectW failed (Windows error %d); engines will be ended one by one",
                    _last_error())
        return None
    # Second try without the optional flag, should a Windows refuse the combination.
    for flags in (_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION,
                  _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = flags
        if kernel32.SetInformationJobObject(job, _JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            return job
        log.warning("SetInformationJobObject(flags %#x) failed (Windows error %d)", flags, _last_error())
    kernel32.CloseHandle(job)
    return None


def _win_full_speed(handle: int) -> None:
    """Ask Windows not to slow this process down to save power.

    Windows 11 may run the processes of a program whose window is minimised at reduced
    speed, above all on battery; an assembly could then take several times as long. The
    engine keeps its (below-normal) priority: this only switches the power throttling off.
    Older Windows does not know the request; that is not an error.
    """
    try:
        state = PROCESS_POWER_THROTTLING_STATE()
        state.Version = _PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = _PROCESS_POWER_THROTTLING_EXECUTION_SPEED     # "I decide this one myself"
        state.StateMask = 0                                               # "...and it is off"
        if not _dll("kernel32").SetProcessInformation(handle, _PROCESS_POWER_THROTTLING,
                                                      ctypes.byref(state), ctypes.sizeof(state)):
            log.info("power throttling could not be switched off for an engine (Windows error %d)",
                     _last_error())
    except Exception:
        if _RAISE:
            raise
        log.info("power throttling could not be switched off for an engine", exc_info=True)


def _win_process_handle(popen: Any, access: int) -> Tuple[Optional[int], bool]:
    """A handle to the process: (handle, True) if this call opened it and must close it."""
    handle = _dll("kernel32").OpenProcess(access, 0, popen.pid)
    if handle:
        return handle, True
    private = getattr(popen, "_handle", None)        # what subprocess itself holds
    try:
        return (int(private), False) if private is not None else (None, False)
    except (TypeError, ValueError):
        return None, False


def _win_process_memory(pid: int) -> Tuple[int, int]:
    """(memory in use now, largest so far) of one process, in bytes; zeros if unknown."""
    kernel32 = _dll("kernel32")
    handle = kernel32.OpenProcess(_PROCESS_MEASURE, 0, pid)
    if not handle:
        return 0, 0
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
        return 0, 0
    finally:
        kernel32.CloseHandle(handle)


def _win_process_cpu(pid: int) -> Optional[float]:
    kernel32 = _dll("kernel32")
    handle = kernel32.OpenProcess(_PROCESS_MEASURE, 0, pid)
    if not handle:
        return None
    try:
        created, ended, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        ticks = sum((part.dwHighDateTime << 32) | part.dwLowDateTime for part in (kernel, user))
        return ticks / 1e7
    finally:
        kernel32.CloseHandle(handle)


def _end_one_process(popen: Any) -> None:
    """End one process directly (not its children). Used when no group can do it."""
    try:
        if popen.poll() is not None:
            return
    except Exception:
        _failed("asking whether an engine has ended")
    if IS_WINDOWS:
        try:                                          # with our own exit code, if Windows lets us
            kernel32 = _dll("kernel32")
            handle = kernel32.OpenProcess(_PROCESS_TERMINATE, 0, popen.pid)
            if handle:
                try:
                    if kernel32.TerminateProcess(handle, STOP_EXIT_CODE):
                        return
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:
            _failed("TerminateProcess")
    try:
        popen.kill()                                  # the last resort
    except Exception:
        _failed("ending an engine")


@atexit.register
def _close_all_groups() -> None:
    """When the program ends in the normal way, no engine is left behind either."""
    for group in list(_all_groups):
        try:
            group.close()
        except Exception:                             # noqa: BLE001 - the program is ending
            pass


# ============================================================================ keep awake

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool) -> bool:
    """Hold off the idle sleep timer while a run is in progress; ``False`` lets the PC sleep again.

    **Call it from the main thread, for on and for off.** On Windows the request belongs to
    the calling thread and ends with it. It stops the idle timer only: not the lid, the
    power button, Sleep chosen from the Start menu or a restart for updates. The screen may
    still go dark.

    Returns True when the PC is now being kept awake. Always False on Linux (nothing is done).
    """
    if not IS_WINDOWS:
        return False
    if threading.current_thread() is not threading.main_thread():
        log.warning("keep_awake(%s) was not called from the main thread; it may not last", on)
    try:
        state = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
        if not _dll("kernel32").SetThreadExecutionState(state):
            log.warning("SetThreadExecutionState(%#x) failed", state)
            return False
        return bool(on)
    except Exception:
        _failed("keep_awake")
        return False


# ============================================================================ one copy per user

_SW_RESTORE = 9
_TK_TOPLEVEL_CLASS = "TkTopLevel"


class InstanceHandle:
    """Proof that this is the only copy running. Keep it for the life of the program."""

    def __init__(self, release: Optional[Callable[[], Any]] = None) -> None:
        self._release = release

    @property
    def exclusive(self) -> bool:
        """False when the check itself failed and the program was let through anyway."""
        return self._release is not None

    def close(self) -> None:
        release, self._release = self._release, None
        if release is not None:
            try:
                release()
            except Exception:
                _failed("releasing the single-instance mark")

    def __enter__(self) -> "InstanceHandle":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _instance_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:200] or "instance"


def single_instance(name: str) -> Optional[InstanceHandle]:
    """Claim to be the only running copy for this user. None when another copy already is.

    Windows: a named mutex in the ``Local\\`` namespace, so one copy per logged-in session
    (right for shared PCs and remote desktop). Linux: a locked file in the user's runtime
    folder. Both vanish with the process, so a crash leaves nothing behind.

    If the check itself fails, the program is let through (a handle whose ``exclusive`` is
    False): a broken check must not keep the program from starting.
    """
    safe = _instance_name(name)
    try:
        if IS_WINDOWS:
            return _win_single_instance(safe)
        return _posix_single_instance(safe)
    except Exception:
        _failed("single_instance")
        return InstanceHandle(None)


def _win_single_instance(safe: str) -> Optional[InstanceHandle]:
    kernel32 = _dll("kernel32")
    _clear_last_error()
    handle = kernel32.CreateMutexW(None, 0, "Local\\" + safe)
    error = _last_error()
    if handle and error == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    if not handle:
        if error == _ERROR_ACCESS_DENIED:            # it exists, and belongs to a copy we may not open
            return None
        log.warning("CreateMutexW failed (Windows error %d); not checking for a second copy", error)
        return InstanceHandle(None)
    return InstanceHandle(lambda: kernel32.CloseHandle(handle))


def _posix_single_instance(safe: str) -> Optional[InstanceHandle]:
    fcntl = importlib.import_module("fcntl")
    folder = os.environ.get("XDG_RUNTIME_DIR", "")
    if not folder or not os.path.isdir(folder):
        folder = tempfile.gettempdir()
        safe = "%s-%d" % (safe, os.getuid())
    descriptor = os.open(os.path.join(folder, safe + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        log.warning("locking for the single-instance check failed: %s", error)
        return InstanceHandle(None)
    return InstanceHandle(lambda: os.close(descriptor))


def bring_other_instance_to_front(title_prefix: str) -> bool:
    """Show the window of the copy that is already running. True when such a window was found.

    Looks for a visible top-level Tk window of another process whose title starts with
    ``title_prefix`` (failing that: contains it, since the title changes when a run ends),
    restores it if it is minimised and asks Windows to put it in front. Always False on
    Linux.
    """
    if not IS_WINDOWS or not title_prefix:
        return False
    try:
        user32 = _dll("user32")
        own_pid = os.getpid()
        found: List[int] = []
        contains: List[int] = []

        def visit(window: Any, _unused: Any) -> int:
            try:                                      # a callback from Windows must never raise
                if not user32.IsWindowVisible(window):
                    return 1
                owner = _DWORD(0)
                user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
                if owner.value == own_pid:
                    return 1
                class_name = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(window, class_name, 64)
                if class_name.value != _TK_TOPLEVEL_CLASS:
                    return 1
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(window, title, 512)
                if title.value.startswith(title_prefix):
                    found.append(window)
                    return 0                          # found: stop looking
                if title_prefix in title.value:
                    contains.append(window)
            except Exception:
                if _RAISE:
                    raise
                log.debug("looking at a window failed", exc_info=True)
            return 1

        callback = _WNDENUMPROC(visit)                # kept alive until EnumWindows returns
        user32.EnumWindows(callback, 0)
        if not found and not contains:
            return False
        window = (found or contains)[0]
        if user32.IsIconic(window):
            user32.ShowWindow(window, _SW_RESTORE)
        if not user32.SetForegroundWindow(window):
            log.info("Windows did not let the running copy come to the front")
        return True
    except Exception:
        _failed("bring_other_instance_to_front")
        return False


# ============================================================================ folder lock

#: Name of the lock file inside a results folder.
LOCK_FILE_NAME = "tesseract-win.lock"
#: What :meth:`FolderLock.holder_text` answers when a folder is held but the holder wrote nothing readable.
UNKNOWN_HOLDER = "unknown"

# The byte that is locked lies far behind the text, so that the text stays readable for
# everyone: on Windows a locked range cannot be read through another handle.
_LOCK_OFFSET = 0x40000000
_LOCK_TRIES = 3
_LOCK_RETRY_SECONDS = 0.05
_O_BINARY = getattr(os, "O_BINARY", 0)


def _lock_file_path(path: str) -> str:
    """The lock file for ``path``: the folder (the usual case), or the lock file itself.

    Only a name that ends in ``.lock`` and is not a folder is taken for the lock file
    itself. Anything else is a folder, also when it is not there yet or is in fact a file:
    the lock file is then looked for *inside* it, so that nothing is ever created,
    emptied or deleted under the name the caller gave.
    """
    path = os.fspath(path)
    if not path:
        return ""
    if not os.path.isdir(path) and path.lower().endswith(".lock"):
        return path
    return os.path.join(path, LOCK_FILE_NAME)


def _open_lock_file(lock_file: str) -> Tuple[Optional[int], Optional[OSError]]:
    """(descriptor, None), or (None, why not)."""
    try:
        return os.open(lock_file, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o644), None
    except PermissionError as error:
        # The file may be there without us being allowed to write it (another user's lock
        # on a shared drive). It can be locked all the same, and if it is held we must
        # still be kept out: so open it for reading.
        try:
            return os.open(lock_file, os.O_RDONLY | _O_BINARY), None
        except OSError:
            return None, error
    except OSError as error:
        return None, error


def _try_lock(descriptor: int) -> Optional[bool]:
    """True: locked. False: somebody else holds it. None: locks do not work here."""
    if IS_WINDOWS:
        msvcrt = importlib.import_module("msvcrt")
        try:
            os.lseek(descriptor, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
                return False
            log.warning("locking the results folder failed in an unexpected way: %s", error)
            return None
    fcntl = importlib.import_module("fcntl")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        log.warning("locking the results folder failed in an unexpected way: %s", error)
        return None


def _unlock(descriptor: int) -> None:
    try:
        if IS_WINDOWS:
            msvcrt = importlib.import_module("msvcrt")
            os.lseek(descriptor, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass                                          # closing the file releases it anyway


def _still_the_same_file(descriptor: int, path: str) -> bool:
    """Linux: the name may have been deleted and made again between opening and locking."""
    if IS_WINDOWS:
        return True                                   # an open file cannot be deleted there
    try:
        ours, named = os.fstat(descriptor), os.stat(path)
    except OSError:
        return False
    return (ours.st_dev, ours.st_ino) == (named.st_dev, named.st_ino)


class FolderLock:
    """Stops two runs from writing into one results folder.

    The lock is a locked byte of an open file (``tesseract-win.lock`` in the folder). It is
    held through the open handle, so it goes away with the process: a crash leaves no stale
    lock. A lock file that merely exists means nothing and is never taken for a lock.
    """

    def __init__(self) -> None:
        self._descriptor: Optional[int] = None
        self._path = ""
        #: True after :meth:`acquire` when no real lock could be taken and the run was let through.
        self.degraded = False

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(self, path: str, info_text: str = "") -> bool:
        """Take the lock for the folder ``path``. False only when another holder has it.

        ``info_text`` (PC name, user, start time; the caller's choice) is written into the
        lock file so that the other side can say who holds the folder. If the lock cannot
        be taken for an unexpected reason (the file cannot be created, or the drive does
        not support locks), that is logged, ``degraded`` is set and the answer is True:
        better an unprotected run than a program that will not start.

        **The folder must exist.** A folder that is not there cannot be locked: the answer
        is True with ``degraded`` set, and nothing is created. So make the results folder
        first, then take the lock.
        """
        self.release()
        lock_file = _lock_file_path(path)
        for attempt in range(_LOCK_TRIES):
            descriptor, error = _open_lock_file(lock_file) if lock_file else (None, OSError("no folder was named"))
            if descriptor is None:
                if isinstance(error, PermissionError) and attempt + 1 < _LOCK_TRIES:
                    time.sleep(_LOCK_RETRY_SECONDS)      # a scanner may have the file for a moment
                    continue
                log.warning("the lock file %s cannot be opened (%s); running without a lock",
                            lock_file, error)
                self.degraded = True
                return True
            state = _try_lock(descriptor)
            if state is None:
                os.close(descriptor)
                self.degraded = True
                return True
            if state and _still_the_same_file(descriptor, lock_file):
                self._descriptor, self._path = descriptor, lock_file
                self._write_info(info_text)
                return True
            if state:
                _unlock(descriptor)                   # locked a file that is no longer the lock file
            os.close(descriptor)
            if attempt + 1 < _LOCK_TRIES:             # holder_text() elsewhere may have had it for a moment
                time.sleep(_LOCK_RETRY_SECONDS)
        return False

    def _write_info(self, info_text: str) -> None:
        assert self._descriptor is not None
        try:
            os.ftruncate(self._descriptor, 0)
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            os.write(self._descriptor, (info_text or "").encode("utf-8"))
        except OSError as error:
            log.warning("could not write into the lock file %s: %s", self._path, error)

    @staticmethod
    def holder_text(path: str) -> str:
        """Who holds the folder ``path``: the holder's ``info_text``, or "" when nobody does.

        A holder that wrote nothing readable gives :data:`UNKNOWN_HOLDER`. Also answers for
        a lock held by this very process.
        """
        lock_file = _lock_file_path(path)
        if not lock_file:
            return ""
        try:
            descriptor = os.open(lock_file, os.O_RDONLY | _O_BINARY)
        except OSError:
            return ""
        try:
            state = _try_lock(descriptor)
            if state is not False:                    # got it (nobody holds it), or locks do not work
                if state:
                    _unlock(descriptor)
                return ""
            os.lseek(descriptor, 0, os.SEEK_SET)
            text = os.read(descriptor, 4096).decode("utf-8", "replace").strip()
            return text or UNKNOWN_HOLDER
        except OSError:
            return UNKNOWN_HOLDER
        finally:
            os.close(descriptor)

    def release(self) -> None:
        """Give the lock up and remove the lock file. Safe to call when nothing is held."""
        descriptor, self._descriptor = self._descriptor, None
        path, self._path = self._path, ""
        self.degraded = False
        if descriptor is None:
            return
        if IS_WINDOWS:
            # An open file cannot be deleted on Windows: close first. If another run takes
            # the file in between, the delete fails, which is right.
            _unlock(descriptor)
            _close_quietly(descriptor)
            _remove_quietly(path)
        else:
            # Delete while still holding the lock: whoever was waiting on this file then
            # sees that it is no longer the lock file and starts again with a new one.
            _remove_quietly(path)
            _close_quietly(descriptor)

    def __enter__(self) -> "FolderLock":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _remove_quietly(path: str) -> None:
    """Delete the lock file if nobody has it open. A scanner may hold it for a moment: try three times."""
    for attempt in range(3):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.05)


# ============================================================================ power and memory

_AC_OFFLINE, _AC_ONLINE = 0, 1
_BATTERY_FLAG_NONE = 128
_BYTE_UNKNOWN = 255


def power_status() -> Tuple[Optional[bool], Optional[int], Optional[bool]]:
    """``(on_battery, percent, has_battery)``; each is None when it is not known.

    A desktop PC gives ``(False, None, False)``.
    """
    try:
        if IS_WINDOWS:
            return _win_power_status()
        return _posix_power_status()
    except Exception:
        _failed("power_status")
        return None, None, None


def _win_power_status() -> Tuple[Optional[bool], Optional[int], Optional[bool]]:
    status = SYSTEM_POWER_STATUS()
    if not _dll("kernel32").GetSystemPowerStatus(ctypes.byref(status)):
        return None, None, None
    on_battery: Optional[bool] = None
    if status.ACLineStatus == _AC_OFFLINE:
        on_battery = True
    elif status.ACLineStatus == _AC_ONLINE:
        on_battery = False
    has_battery: Optional[bool] = None
    if status.BatteryFlag != _BYTE_UNKNOWN:
        has_battery = not status.BatteryFlag & _BATTERY_FLAG_NONE
    percent: Optional[int] = int(status.BatteryLifePercent) if status.BatteryLifePercent <= 100 else None
    if has_battery is False:
        percent = None
        if on_battery is None:
            on_battery = False
    return on_battery, percent, has_battery


def _read_small(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(256).strip()
    except OSError:
        return ""


def _posix_power_status(root: str = "/sys/class/power_supply") -> Tuple[Optional[bool], Optional[int], Optional[bool]]:
    try:
        supplies = sorted(os.listdir(root))
    except OSError:
        return None, None, None
    percent: Optional[int] = None
    has_battery = False
    mains: Optional[bool] = None
    discharging = False
    for name in supplies:
        folder = os.path.join(root, name)
        kind = _read_small(os.path.join(folder, "type"))
        if kind == "Battery":
            if _read_small(os.path.join(folder, "scope")) == "Device":
                continue                              # the battery of a mouse or a keyboard
            has_battery = True
            capacity = _read_small(os.path.join(folder, "capacity"))
            if capacity.isdigit() and percent is None:
                percent = min(100, int(capacity))
            if _read_small(os.path.join(folder, "status")) == "Discharging":
                discharging = True
        elif kind in ("Mains", "USB", "USB_C", "USB_PD"):
            online = _read_small(os.path.join(folder, "online"))
            if online in ("0", "1"):
                mains = bool(mains) or online == "1"
    if not has_battery:
        return False, None, False
    if discharging:
        return True, percent, True
    if mains is not None:
        return (not mains), percent, True
    return False, percent, True


def _win_memory_status() -> Optional[MEMORYSTATUSEX]:
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if _dll("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
        return status
    return None


def _posix_meminfo(key: str) -> Optional[int]:
    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def total_memory_bytes() -> Optional[int]:
    """Installed memory of this PC, in bytes; None when unknown."""
    try:
        if IS_WINDOWS:
            status = _win_memory_status()
            return int(status.ullTotalPhys) if status else None
        total = _posix_meminfo("MemTotal")
        if total is None:
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        return total
    except Exception:
        _failed("total_memory_bytes")
        return None


def available_memory_bytes() -> Optional[int]:
    """Memory a new program could use right now without swapping, in bytes; None when unknown."""
    try:
        if IS_WINDOWS:
            status = _win_memory_status()
            return int(status.ullAvailPhys) if status else None
        available = _posix_meminfo("MemAvailable")
        if available is None:
            available = _posix_meminfo("MemFree")
        return available
    except Exception:
        _failed("available_memory_bytes")
        return None


# ============================================================================ drives and folders

_DRIVE_KINDS = {2: "removable", 3: "fixed", 4: "network", 5: "removable", 6: "fixed"}
_NETWORK_FILESYSTEMS = {
    "nfs", "nfs4", "cifs", "smb3", "smb2", "smbfs", "ncpfs", "9p", "afs", "ceph", "glusterfs",
    "lustre", "davfs", "fuse.sshfs", "fuse.davfs2", "fuse.rclone", "fuse.s3fs", "fuse.smbnetfs",
    "fuse.glusterfs", "fuse.gvfsd-fuse",
}
_FILESYSTEM_NAMES = {"vfat": "FAT32", "msdos": "FAT32", "exfat": "exFAT", "ntfs": "NTFS", "ntfs3": "NTFS"}


def _absolute(path: str) -> str:
    """Absolute form of ``path`` in whichever style it is written (the tests pass Windows paths on Linux)."""
    path = os.fspath(path)
    if ntpath.isabs(path) or posixpath.isabs(path) or path.startswith("\\\\"):
        return path
    return os.path.abspath(path)


def _is_share_path(full: str) -> bool:
    r"""``\\server\share\...`` (also in its long form ``\\?\UNC\server\share\...``)."""
    full = full.replace("/", "\\")
    if full.upper().startswith("\\\\?\\UNC\\"):
        return True
    return full.startswith("\\\\") and not full.startswith(("\\\\?\\", "\\\\.\\"))


def _win_volume_root(path: str) -> str:
    r"""``C:\`` or ``\\server\share\`` for a path; "" if it has neither."""
    full = _absolute(path)
    try:
        buffer = ctypes.create_unicode_buffer(max(len(full) + 2, 262))
        if _dll("kernel32").GetVolumePathNameW(full, buffer, len(buffer)) and buffer.value:
            return buffer.value
    except Exception:
        _failed("GetVolumePathNameW")
    drive = ntpath.splitdrive(full)[0]
    return drive + "\\" if drive else ""


def _unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


def _posix_mount(path: str, table: str = "/proc/self/mountinfo") -> Optional[Tuple[str, str, str]]:
    """(mount point, file system type, source) of the mount that holds ``path``; None if unknown."""
    real = os.path.realpath(path)
    best: Optional[Tuple[str, str, str]] = None
    try:
        with open(table, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                left, separator, right = line.partition(" - ")
                before, after = left.split(), right.split()
                if not separator or len(before) < 5 or len(after) < 2:
                    continue
                mount_point = _unescape_mount(before[4])
                inside = real == mount_point or real.startswith(mount_point.rstrip("/") + "/")
                if inside and (best is None or len(mount_point) >= len(best[0])):
                    best = (mount_point, after[0], _unescape_mount(after[1]))
    except OSError:
        return None
    return best


def _posix_is_removable(source: str) -> bool:
    if not source.startswith("/dev/"):
        return False
    device = os.path.realpath(os.path.join("/sys/class/block", os.path.basename(os.path.realpath(source))))
    for folder in (device, os.path.dirname(device)):  # a partition: the flag is on the disk above it
        if _read_small(os.path.join(folder, "removable")) == "1":
            return True
    return False


def drive_kind(path: str) -> str:
    """"fixed", "removable", "network" or "unknown" for the drive that holds ``path``.

    A USB hard disk reports as fixed (harmless). The path need not exist yet.
    """
    try:
        if IS_WINDOWS:
            kind = None
            try:
                root = _win_volume_root(path)
                if root:
                    kind = _DRIVE_KINDS.get(_dll("kernel32").GetDriveTypeW(root))
            except Exception:
                _failed("GetDriveTypeW")
            if kind is None and _is_share_path(_absolute(path)):
                kind = "network"                      # \\server\share says so by itself
            return kind or "unknown"
        mount = _posix_mount(path)
        if mount is None:
            return "unknown"
        if mount[1] in _NETWORK_FILESYSTEMS:
            return "network"
        return "removable" if _posix_is_removable(mount[2]) else "fixed"
    except Exception:
        _failed("drive_kind")
        return "unknown"


def filesystem_name(path: str) -> str:
    """Name of the file system that holds ``path``: "NTFS", "FAT32", "exFAT", "ext4"...; "" if unknown.

    FAT32 matters: it cannot hold a file larger than 4 GB.
    """
    try:
        if IS_WINDOWS:
            root = _win_volume_root(path)
            if not root:
                return ""
            name = ctypes.create_unicode_buffer(64)
            if _dll("kernel32").GetVolumeInformationW(root, None, 0, None, None, None, name, 64):
                return name.value
            return ""
        mount = _posix_mount(path)
        if mount is None:
            return ""
        return _FILESYSTEM_NAMES.get(mount[1], mount[1])
    except Exception:
        _failed("filesystem_name")
        return ""


def _win_volume_label(path: str) -> str:
    try:
        root = _win_volume_root(path)
        if not root:
            return ""
        label = ctypes.create_unicode_buffer(262)
        if _dll("kernel32").GetVolumeInformationW(root, label, 262, None, None, None, None, 0):
            return label.value
    except Exception:
        _failed("reading the volume label")
    return ""


_ONEDRIVE_VARIABLES = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def _comparable(path: str) -> str:
    return re.sub(r"[\\/]+", "/", path).rstrip("/").casefold()


def synced_folder_name(path: str) -> str:
    """"OneDrive", "Dropbox", "Google Drive", "iCloud Drive" or "Nextcloud" if ``path`` lies in such a folder, else "".

    A guess by name (and, for OneDrive, by the folders Windows names in the environment).
    It will miss some, so it may drive a note on the screen but never a refusal.
    """
    try:
        full = _comparable(_absolute(path))
        for variable in _ONEDRIVE_VARIABLES:
            root = _comparable(os.environ.get(variable, ""))
            if root and (full == root or full.startswith(root + "/")):
                return "OneDrive"
        for part in full.split("/"):
            if part == "onedrive" or part.startswith("onedrive - "):
                return "OneDrive"
            if part == "dropbox" or part.startswith("dropbox ("):
                return "Dropbox"
            if part in ("google drive", "googledrive", "my drive") or part.startswith("googledrive-"):
                return "Google Drive"
            if part in ("icloud drive", "iclouddrive"):
                return "iCloud Drive"
            if part == "nextcloud":
                return "Nextcloud"
        if IS_WINDOWS and _win_volume_label(path).casefold() == "google drive":
            return "Google Drive"
        return ""
    except Exception:
        _failed("synced_folder_name")
        return ""


_FILE_ATTRIBUTE_OFFLINE = 0x00001000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def is_online_only(path: str) -> bool:
    """True when the file is a placeholder whose content is still in the cloud (OneDrive "online-only").

    Reading such a file starts a download. The answer comes from the file's attributes,
    without opening it. Always False on Linux.
    """
    if not IS_WINDOWS:
        return False
    try:
        attributes = _dll("kernel32").GetFileAttributesW(os.fspath(path))
        if attributes == _INVALID_FILE_ATTRIBUTES:
            return False
        return bool(attributes & (_FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN
                                  | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))
    except Exception:
        _failed("is_online_only")
        return False


# ============================================================================ Windows version, encoding

#: First Windows 10 build (version 1903) on which the engines' built-in UTF-8 declaration is in force.
UTF8_BUILD = 18362
_DARK_TITLEBAR_BUILD = 18985


def windows_build() -> int:
    """The Windows build number (19045, 22631...); 0 on Linux."""
    if not IS_WINDOWS:
        return 0
    try:
        version = sys.getwindowsversion()            # type: ignore[attr-defined]
        build = int(version.build)
        platform_version = getattr(version, "platform_version", None)
        if platform_version:                          # not fooled by compatibility settings
            build = max(build, int(platform_version[2]))
        return build
    except Exception:
        _failed("windows_build")
        return 0


def ansi_code_page() -> int:
    """The PC's own code page (1252, 932...), to be named in the log; 0 on Linux or if unknown."""
    if not IS_WINDOWS:
        return 0
    try:
        return int(_dll("kernel32").GetACP())
    except Exception:
        _failed("ansi_code_page")
        return 0


def _round_trips(text: str, encoding: str) -> bool:
    """True when every character of ``text`` survives the encoding unchanged.

    The comparison matters: Windows can quietly swap a character for a look-alike, and
    "encoding raised no error" would not notice.
    """
    try:
        return text.encode(encoding, "strict").decode(encoding, "strict") == text
    except (UnicodeError, LookupError):
        return False


def path_fits_system_encoding(path: str) -> bool:
    """Can the engines open this path on this PC?

    Always True on Linux and on Windows 10 build 18362 (version 1903) or later, where the
    engines read paths as UTF-8. On older builds they use the PC's own code page: True only
    if every character of the path exists in it.
    """
    if not IS_WINDOWS:
        return True
    if windows_build() >= UTF8_BUILD:
        return True
    return _round_trips(os.fspath(path), "mbcs")


# ============================================================================ appearance

#: Set to "dark" or "light" to overrule the system setting (used for screenshots and tests).
THEME_VARIABLE = "TESSERACT_WIN_THEME"
_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_SPI_GETHIGHCONTRAST = 0x0042
_HCF_HIGHCONTRASTON = 0x00000001


def is_dark_mode() -> bool:
    """True when Windows is set to dark for applications. False on Linux and when unknown.

    The environment variable ``TESSERACT_WIN_THEME`` (``dark`` or ``light``) overrules it on
    every system.
    """
    forced = os.environ.get(THEME_VARIABLE, "").strip().lower()
    if forced in ("dark", "light"):
        return forced == "dark"
    if not IS_WINDOWS:
        return False
    try:
        winreg = importlib.import_module("winreg")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        except OSError:
            return False                              # the value is missing on old builds: light
        return int(value) == 0
    except Exception:
        _failed("is_dark_mode")
        return False


def is_high_contrast() -> bool:
    """True when a Windows high-contrast theme is on (then use system colours). False on Linux."""
    if not IS_WINDOWS:
        return False
    try:
        info = HIGHCONTRASTW()
        info.cbSize = ctypes.sizeof(info)
        if _dll("user32").SystemParametersInfoW(_SPI_GETHIGHCONTRAST, info.cbSize, ctypes.byref(info), 0):
            return bool(info.dwFlags & _HCF_HIGHCONTRASTON)
        return False
    except Exception:
        _failed("is_high_contrast")
        return False


# ============================================================================ opening things

_COINIT_APARTMENTTHREADED = 0x2


def _spawn_detached(command: Any) -> bool:
    """Start a helper program and forget it. It is not put into any process group."""
    extra: Dict[str, Any] = {} if IS_WINDOWS else {"start_new_session": True}
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **extra)
    except (OSError, ValueError) as error:
        log.info("could not start %r: %s", command, error)
        return False
    threading.Thread(target=process.wait, name="osapi-open", daemon=True).start()
    return True


def open_path(path: str) -> bool:
    """Open a folder in the file manager, or a file with the program set for its kind.

    False, never an exception, when the path does not exist or nothing can open it (on
    Windows: no program is set for that kind of file); the caller can then open the folder
    instead. What it starts is not in any :class:`ProcessGroup`, so a browser opened here
    survives the program.
    """
    try:
        if not path or not os.path.exists(path):
            return False
        if IS_WINDOWS:
            starter = getattr(os, "startfile", None)
            if starter is None:
                return False
            try:
                starter(ntpath.normpath(os.fspath(path)))
                return True
            except OSError as error:
                log.info("nothing opens %s: %s", path, error)
                return False
        opener = shutil.which("xdg-open") or shutil.which("open")
        if not opener:
            return False
        return _spawn_detached([opener, os.fspath(path)])
    except Exception:
        _failed("open_path")
        return False


def reveal(path: str) -> bool:
    """Open the folder that holds ``path`` with the item selected. False when that is not possible.

    On Linux the folder is opened, without a selection.
    """
    try:
        if not path or not os.path.exists(path):
            return False
        if IS_WINDOWS:
            full = ntpath.normpath(_absolute(path))
            try:
                if _win_select_in_explorer(full):
                    return True
            except Exception:
                _failed("SHOpenFolderAndSelectItems")
            return _spawn_detached('explorer.exe /select,"%s"' % full)
        return open_path(os.path.dirname(os.path.abspath(path)))
    except Exception:
        _failed("reveal")
        return False


_FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"


def desktop_dir() -> str:
    """The user's Desktop folder; the home folder if there is no Desktop. Always an existing folder.

    On Windows the Desktop is often moved into OneDrive, so ``~\\Desktop`` may not exist:
    Windows is asked where it is.
    """
    home = os.path.expanduser("~")
    found = ""
    try:
        found = _win_known_folder(_FOLDERID_DESKTOP) if IS_WINDOWS else _posix_desktop(home)
    except Exception:
        _failed("desktop_dir")
    for candidate in (found, os.path.join(home, "Desktop"), home):
        if candidate and os.path.isdir(candidate):
            return candidate
    return home


def _win_known_folder(folder_id: str) -> str:
    uuid = importlib.import_module("uuid")
    identifier = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    address = ctypes.c_void_p()
    ole32 = _dll("ole32")
    try:
        result = _dll("shell32").SHGetKnownFolderPath(ctypes.byref(identifier), 0, None, ctypes.byref(address))
        if result != _S_OK or not address.value:
            return ""
        return ctypes.wstring_at(address.value)
    finally:
        if address.value:
            ole32.CoTaskMemFree(address)              # the caller frees what the shell allocated


def _posix_desktop(home: str) -> str:
    """What ``~/.config/user-dirs.dirs`` names as the Desktop (it is translated on many systems)."""
    config = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "user-dirs.dirs")
    try:
        with open(config, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.match(r'\s*XDG_DESKTOP_DIR\s*=\s*"(.*)"\s*$', line)
                if match:
                    return match.group(1).replace("$HOME", home)
    except OSError:
        pass
    return ""


def _win_select_in_explorer(full: str) -> bool:
    shell32, ole32 = _dll("shell32"), _dll("ole32")
    initialised = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    try:
        item = shell32.ILCreateFromPathW(full)
        if not item:
            return False
        try:
            return shell32.SHOpenFolderAndSelectItems(item, 0, None, 0) >= 0
        finally:
            shell32.ILFree(item)
    finally:
        if initialised in (_S_OK, _S_FALSE):          # every successful initialise needs its uninitialise
            ole32.CoUninitialize()


# ============================================================================ the window

_FLASHW_TRAY = 0x00000002
_FLASHW_TIMERNOFG = 0x0000000C
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER = 0x0001, 0x0002, 0x0004
_SWP_NOACTIVATE, _SWP_FRAMECHANGED = 0x0010, 0x0020


def _tk_frame_handle(tk_root: Any) -> int:
    """The Windows handle of the outer frame of a Tk top-level window.

    ``winfo_id()`` is the inner window: flashing it or colouring its title bar does nothing.
    """
    return int(str(tk_root.wm_frame()), 16)


def flash_taskbar(tk_root: Any) -> bool:
    """Flash the taskbar button until the window is looked at. Main thread only.

    Does nothing, and answers False, when the window is already in front, and on Linux.
    Never steals the keyboard.
    """
    if not IS_WINDOWS:
        return False
    try:
        user32 = _dll("user32")
        window = _tk_frame_handle(tk_root)
        if user32.GetForegroundWindow() == window:
            return False
        info = FLASHWINFO()
        info.cbSize = ctypes.sizeof(info)
        info.hwnd = window
        info.dwFlags = _FLASHW_TRAY | _FLASHW_TIMERNOFG
        info.uCount = 0
        info.dwTimeout = 0
        user32.FlashWindowEx(ctypes.byref(info))     # its answer is the old state, not success
        return True
    except Exception:
        _failed("flash_taskbar")
        return False


_MONITOR_DEFAULTTONEAREST = 2


def work_area_at(x: int, y: int) -> Optional[Tuple[int, int, int, int]]:
    """The usable rectangle ``(left, top, right, bottom)`` of the monitor at a screen point.

    "Usable" leaves out the taskbar. Tk only knows the main screen; with this, tooltips and
    dialogs stay on the monitor they belong to (``widgets.set_work_area_provider`` takes
    this function as it is). None on Linux and when Windows cannot say.
    """
    if not IS_WINDOWS:
        return None
    try:
        user32 = _dll("user32")
        monitor = user32.MonitorFromPoint(POINT(int(x), int(y)), _MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return int(work.left), int(work.top), int(work.right), int(work.bottom)
    except Exception:
        _failed("work_area_at")
        return None


def set_dark_titlebar(tk_root: Any, dark: bool) -> bool:
    """Make the title bar of a Tk window dark (or light again). Main thread only.

    False when this Windows cannot do it (builds before 17763), and on Linux.
    """
    if not IS_WINDOWS:
        return False
    try:
        window = _tk_frame_handle(tk_root)
        value = ctypes.c_int(1 if dark else 0)
        dwmapi = _dll("dwmapi")
        attributes = [_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD]
        if windows_build() < _DARK_TITLEBAR_BUILD:
            attributes.reverse()
        for attribute in attributes:
            if dwmapi.DwmSetWindowAttribute(window, attribute, ctypes.byref(value),
                                            ctypes.sizeof(value)) == _S_OK:
                break
        else:
            return False
        # The title bar is repainted only when the frame changes.
        _dll("user32").SetWindowPos(window, None, 0, 0, 0, 0,
                                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER
                                    | _SWP_NOACTIVATE | _SWP_FRAMECHANGED)
        return True
    except Exception:
        _failed("set_dark_titlebar")
        return False
