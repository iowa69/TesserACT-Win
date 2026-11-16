# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""What the window knows, and every decision that needs no Tk.

:class:`AppState` holds the list of samples, the settings of the Trimming and Assembly
pages (``model.TrimSettings`` and ``model.AssemblySettings``, standard at every launch),
the results folder and what was found out about it. The pages read from it and write to
it, and tell each other through :meth:`AppState.notify`.

The decisions are plain functions, so they can be tested without a display:

- :func:`start_status`: may Start be pressed, the count on it, and the reason beside it;
- :func:`step_lines`: the small lines under the step names ("40 ready", "standard", "changed");
- :func:`inspect_folder` (slow, for a worker thread) and :func:`judge_folder` (instant):
  everything of audit part A-4 about the results folder;
- :func:`suggest_results_dir`, :func:`build_config`, :func:`left_out_rows`.

:class:`TaskRunner` runs slow work in a thread and hands the answer back to the thread
that calls :meth:`TaskRunner.poll` (the window's own), which is the only one to touch Tk.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import logging
import os
import queue
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import appdirs, branding, fsutil, osapi, pairing, results, settings_map
from ..model import (AssemblySettings, EnginePaths, Existing, ExistingResult, RunConfig, RunStatus,
                     Sample, SampleState, TrimSettings, default_threads)
from . import texts

__all__ = [
    "Words", "TaskRunner", "AppState", "FolderFacts", "FolderVerdict", "StartStatus",
    "clean_folder_text", "is_inside", "inspect_folder", "judge_folder", "suggest_results_dir",
    "start_status", "step_lines", "build_config", "left_out_rows", "status_text",
    "trim_changed", "assembly_changed", "trim_more_changed", "assembly_more_changed",
    "settings_problems", "short_date", "first_sentence", "row_id_of", "demo_samples",
]

log = logging.getLogger(__name__)


# ===========================================================================================
# WORDS THAT ARE NOT IN texts.py YET
# Every other word of these pages comes from texts.py. The sentences below were needed by
# the shell and the Samples page and are kept here, in one block, until they move there.
# The wording follows docs/superpowers/audits/ux-windows.md (rows in brackets).
# ===========================================================================================

class Words:
    HELP_DEFAULT = "Rest the mouse on any option to read what it does."
    # the list
    CHECKING_ROW = "Checking..."
    CHECKING_FILES = "Checking the files..."
    LOOKING = "Looking for FASTQ files... {n} found."                                    # 2.1
    NONE_PAIRED = ("None of these files has a partner. This program needs paired-end reads: two "
                   "files per sample. Single-end reads cannot be assembled here.")            # 2.7
    NOT_ON_DISK = ("Those items are not files on a disk yet. Save or extract them first, then "
                   "add them.")                                                               # 2.24
    NOT_ADDED_TITLE = "Not added"
    NOT_ADDED_BODY = "These files were not added to the list."
    LEFT_OUT_PREFIX = "Left out: "
    EXAMPLE_MISSING = ("The example is missing from this copy of the program. Extract the zip "
                       "again to get it back.")
    FASTQ_FILES = "FASTQ files"
    ALL_FILES = "All files"
    CHOOSE_FILES = "Choose FASTQ files"
    CHOOSE_READS_FOLDER = "Choose the folder that holds your FASTQ files"
    CHOOSE_RESULTS_FOLDER = "Choose the results folder"
    # beside a disabled Start
    RUN_IN_PROGRESS = "A run is in progress."
    PROGRAM_PART_MISSING = "Part of the program is missing or blocked: see the red bar at the top."
    CHECKING_FOLDER = "Checking the results folder..."
    FOLDER_ON_SAMPLES_STEP = " See the Samples step."
    # the results folder (A-4)
    RESULTS_NEW_FOLDER = "Results will go into a new folder here: {path}"                     # 4.2
    RESULTS_INSIDE_PROGRAM = ("This folder is inside the program's own folder. If you delete or "
                              "replace the program later, the results go with it. A folder "
                              "elsewhere is safer.")                                          # 4.4
    RESULTS_NOT_WRITABLE = ("The program cannot write in this folder. Choose another one, for "
                            "example a new folder on drive C: or D:. On some PCs Windows protects "
                            "Documents and Desktop against programs it does not know.")       # 4.5
    RESULTS_SYNCED = ("This folder is synced to the cloud ({name}). It works, but several GB will "
                      "be uploaded and the run may be slower. A folder outside {name} is better.")  # 4.6
    RESULTS_NETWORK = ("This folder is on the network. It works, but it is slower, and the run "
                       "stops if the connection drops. A folder on this PC is safer.")        # 4.7
    RESULTS_REMOVABLE = ("This is a removable drive. Do not unplug it during the run. A folder on "
                         "this PC is faster.")                                                # 4.8
    RESULTS_FAT32 = ("This drive cannot store files larger than 4 GB, and {sample} needs more. "
                     "Choose another drive.")                                                 # 4.8
    RESULTS_NO_SPACE = "Not enough free space: this run needs about {need} and {drive} has {free} free."  # 4.9
    RESULTS_NO_SPACE_ADVICE = " Choose another folder or free some space."
    RESULTS_NO_SPACE_ADVICE_KEEP = (" Choose another folder, free some space, or untick 'Keep the "
                                    "trimmed reads' on the Trimming page (it then needs about {less}).")
    RESULTS_TOO_LONG = ("This folder's path is too long for Windows. Choose a folder with a "
                        "shorter path, for example D:\\Results.")                             # 4.10
    RESULTS_ENCODING = ("This folder's path contains characters that this version of Windows "
                        "cannot pass on. Choose a folder whose path uses plain letters.")     # 4.11
    RESULTS_INVALID = "This is not a folder the program can use. Use Browse to choose one."   # 4.12
    RESULTS_IN_USE = ("This results folder is being used by another run ({holder}). Choose "
                      "another folder, or wait until that run has finished.")                 # 4.13
    RESULTS_IN_USE_UNKNOWN = ("This results folder is being used by another run. Choose another "
                              "folder, or wait until that run has finished.")
    RESULTS_NEWER = ("This results folder was made by a newer version of %s. Use that version, "
                     "or choose another folder." % branding.APP_NAME)                         # 4.14
    THIS_DRIVE = "this drive"
    DRIVE = "drive {letter}"
    # rows that the results folder leaves out (A-3)
    SAME_NAME_OTHER_READS = ("A different sample called {name} is already in the results folder "
                             "(made {when}from other read files). Rename this sample or choose "
                             "another results folder.")                                       # 3.6
    FOREIGN_FOLDER = ("The folder {name}{sep}{folder} already exists and was not made by this "
                      "program. Rename this sample or choose another results folder.")        # 3.7
    # boxes
    ABOUT_TITLE = "About %s" % branding.APP_NAME
    LOGS_NOTE = ("If something goes wrong, save a problem report and attach it to an e-mail. It holds "
                 "the program's logs and the logs of failed samples, with the names of your folders "
                 "and files. It never holds reads or assemblies.")
    UNFINISHED_RUN = ("This results folder holds a run that did not finish. You can continue it "
                      "on the Run step, or press Start here to begin a new run in the same folder.")
    PROGRAM_FAULT = ("Something went wrong inside %s. If this keeps happening, please send a "
                     "problem report (About, Save a problem report)." % branding.APP_NAME)        # 9.14
    PROGRAM_FAULT_RUNNING = ("Something went wrong inside %s. The run is still going. If this keeps "
                             "happening, please send a problem report (About, Save a problem "
                             "report)." % branding.APP_NAME)
    REPORT_TITLE = "Problem report"
    REPORT_SAVED = ("The problem report was saved as {name} in {folder}. Attach that file to an "
                    "e-mail when you ask for help.")
    REPORT_NOT_SAVED = ("The problem report could not be saved: the program could not write to "
                        "your Desktop or to your own folder. Use Open log folder instead and send "
                        "the files you find there.")
    RUN_PAGE_MISSING = ("The Run step could not be loaded, so nothing can be started. Extract "
                        "the zip again; if that does not help, see the log folder (About).")
    OVERWRITE_OTHER_SETTINGS = "made with other settings"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def short_date(iso: str) -> str:
    """ "3 Nov 2026" from an ISO time; "" when it cannot be read. Never the PC's own format."""
    try:
        moment = datetime.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return ""
    return "%d %s %d" % (moment.day, _MONTHS[moment.month - 1], moment.year)


_SENTENCE_END = re.compile(r"(?<=[.?!])\s+(?=[A-Z])")


def first_sentence(text: str) -> str:
    """The first sentence of a message, for the narrow place beside Start."""
    parts = _SENTENCE_END.split(text.strip(), maxsplit=1)
    return parts[0] if parts else ""


_KEEP_CAPITAL = ("Read ", "Windows ", "Lane ", "FASTQ", "FASTA")


def status_text(state: SampleState, note: str) -> str:
    """The Status cell: "Ready", "Ready. <note>", or "Left out: <reason>" in full."""
    note = (note or "").strip()
    if state is SampleState.LEFT_OUT:
        if not note:
            return texts.SAMPLE_STATE_LABEL[SampleState.LEFT_OUT]
        first = note.split(" ", 1)[0]
        plain_word = (first[:1].isupper() and (len(first) == 1 or first[1:].islower())
                      and not note.startswith(_KEEP_CAPITAL))
        return Words.LEFT_OUT_PREFIX + (note[:1].lower() + note[1:] if plain_word else note)
    ready = texts.SAMPLE_STATE_LABEL[SampleState.READY]
    return "%s. %s" % (ready, note) if note else ready


# =========================================================================== work in threads

class TaskRunner:
    """Slow work off the window's thread; the answers come back through :meth:`poll`.

    ``submit(work, done)`` starts ``work(report)`` in a daemon thread. ``report(item)`` may
    be called by the work as often as it likes; each item reaches ``progress(item)``. When
    the work has ended, ``done(result, error)`` is called (``error`` is the exception, or
    None). ``progress`` and ``done`` run in the thread that calls :meth:`poll`.

    With a ``key`` only the newest job of that key is answered: the answers of older jobs
    are dropped, so a slow check of a folder the user has already left cannot overwrite
    the check of the folder chosen since.
    """

    def __init__(self) -> None:
        self._answers: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._newest: Dict[str, int] = {}
        self._serial = 0
        self.closed = False

    def submit(self, work: Callable[[Callable[[Any], None]], Any],
               done: Optional[Callable[[Any, Optional[BaseException]], Any]] = None, *,
               progress: Optional[Callable[[Any], Any]] = None, key: Optional[str] = None) -> None:
        with self._lock:
            self._serial += 1
            serial = self._serial
            if key is not None:
                self._newest[key] = serial
            self._pending += 1

        def report(item: Any) -> None:
            if progress is not None:
                self._answers.put(("progress", key, serial, progress, item))

        def run() -> None:
            result: Any = None
            error: Optional[BaseException] = None
            try:
                result = work(report)
            except BaseException as exc:              # the answer must always come back
                error = exc
                log.exception("background work failed")
            self._answers.put(("done", key, serial, done, (result, error)))

        threading.Thread(target=run, name="tesseract-win-task", daemon=True).start()

    def is_newest(self, key: Optional[str], serial: int) -> bool:
        return key is None or self._newest.get(key) == serial

    def poll(self, limit: int = 400) -> int:
        """Hand out the answers that have arrived. Returns how many were handled."""
        handled = 0
        while handled < limit:
            try:
                kind, key, serial, callback, payload = self._answers.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if kind == "done":
                with self._lock:
                    self._pending -= 1
            if self.closed or callback is None or not self.is_newest(key, serial):
                continue
            try:
                if kind == "done":
                    callback(*payload)
                else:
                    callback(payload)
            except Exception:
                log.exception("an answer from background work could not be shown")
        return handled

    @property
    def busy(self) -> bool:
        """True while work is running or an answer is waiting to be handed out."""
        with self._lock:
            return self._pending > 0 or not self._answers.empty()

    def close(self) -> None:
        """The window is closing: later answers are thrown away."""
        self.closed = True


# =========================================================================== settings

def _effective_trim(trim: TrimSettings) -> TrimSettings:
    """The settings with every number behind an unticked box put back to the standard."""
    standard = TrimSettings()
    out = dataclasses.replace(trim)
    if not (out.trim_low_quality_ends or out.trim_low_quality_starts):
        out.end_quality = standard.end_quality
    if not out.remove_poor_reads:
        out.min_mean_quality, out.base_quality = standard.min_mean_quality, standard.base_quality
        out.max_low_percent = standard.max_low_percent
    if not out.remove_short_reads:
        out.min_length = standard.min_length
    if not out.fixed_trim:
        out.trim_front, out.trim_tail = standard.trim_front, standard.trim_tail
    if not out.own_adapters:
        out.adapter_r1, out.adapter_r2 = standard.adapter_r1, standard.adapter_r2
    else:
        out.adapter_r1, out.adapter_r2 = out.adapter_r1.strip(), out.adapter_r2.strip()
    return out


def _effective_assembly(assembly: AssemblySettings) -> AssemblySettings:
    standard = AssemblySettings()
    out = dataclasses.replace(assembly)
    if not out.know_genome_size:
        out.genome_size_mb = standard.genome_size_mb
    if not out.drop_short_contigs:
        out.min_contig = standard.min_contig
    return out


#: What stands behind "More options" on each page.
TRIM_MORE_FIELDS = ("end_quality", "min_mean_quality", "base_quality", "max_low_percent", "min_length",
                    "trim_low_quality_starts", "trim_polyx", "fixed_trim", "trim_front", "trim_tail",
                    "own_adapters", "adapter_r1", "adapter_r2")
ASSEMBLY_MORE_FIELDS = ("mode", "scaffold", "polish", "force", "threads")


def trim_changed(trim: TrimSettings) -> bool:
    """Does trimming differ from the standard in a way that counts? A number behind an
    unticked box does not."""
    return _effective_trim(trim) != TrimSettings()


def assembly_changed(assembly: AssemblySettings) -> bool:
    return _effective_assembly(assembly) != AssemblySettings()


def trim_more_changed(trim: TrimSettings) -> bool:
    """Is something behind "More options" of the Trimming page not standard?"""
    now, standard = _effective_trim(trim), TrimSettings()
    return any(getattr(now, name) != getattr(standard, name) for name in TRIM_MORE_FIELDS)


def assembly_more_changed(assembly: AssemblySettings) -> bool:
    now, standard = _effective_assembly(assembly), AssemblySettings()
    return any(getattr(now, name) != getattr(standard, name) for name in ASSEMBLY_MORE_FIELDS)


# =========================================================================== the results folder

@dataclass
class FolderFacts:
    """What a look at the results folder found (:func:`inspect_folder`). Facts only; the
    sentences are made by :func:`judge_folder`."""
    asked: str = ""                    # the text that was checked
    path: str = ""                     # the folder that will be used
    appended: bool = False             # a TesserACT_results folder was added to what was asked
    valid: bool = True
    writable: bool = True
    free: Optional[int] = None
    drive: str = ""                    # "D:" on Windows, "" elsewhere
    kind: str = "unknown"              # fixed, removable, network, unknown
    filesystem: str = ""
    synced: str = ""                   # "OneDrive", ...
    inside_program: bool = False
    encoding_ok: bool = True
    holder: str = ""                   # who is running there now
    newer: bool = False
    existing: Dict[str, ExistingResult] = field(default_factory=dict)
    failed_before: List[str] = field(default_factory=list)
    clashes: Dict[str, str] = field(default_factory=dict)     # name -> when the other one was made
    foreign: Dict[str, str] = field(default_factory=dict)     # name -> "assembly" | "trimming"
    signature: Tuple[Any, ...] = ()    # the list the facts were found for


@dataclass
class FolderVerdict:
    """What the Samples page shows under the results folder, and whether Start may run."""
    pending: bool = False              # the folder has not been looked at yet
    problem: str = ""                  # in full; Start is disabled while it stands
    short: str = ""                    # the same for the narrow place beside Start
    info: str = ""                     # "Each sample gets its own folder here. Needs about ..."
    notes: List[str] = field(default_factory=list)
    existing: str = ""                 # "12 of these samples already have results here."
    need: int = 0
    free: Optional[int] = None


_DRIVE_ONLY = re.compile(r"^[A-Za-z]:$")
_BAD_IN_PATH = re.compile(r'[<>"|?*\x00-\x1f]')


def clean_folder_text(text: str) -> str:
    """A typed or pasted folder as a path: quotes and spaces around it removed (audit 4.12)."""
    cleaned = (text or "").strip()
    while len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    cleaned = cleaned.strip("\"'").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("~"):
        cleaned = os.path.expanduser(cleaned)
    if _DRIVE_ONLY.match(cleaned):
        return cleaned + os.sep
    try:
        return os.path.normpath(cleaned)
    except (TypeError, ValueError):
        return cleaned


def _is_usable_path(path: str) -> bool:
    if not path or not os.path.isabs(path) or "\x00" in path:
        return False
    rest = os.path.splitdrive(path)[1]
    if osapi.IS_WINDOWS and (_BAD_IN_PATH.search(rest) or ":" in rest):
        return False
    return True


def _nearest_existing(path: str) -> str:
    current = path
    for _ in range(64):
        try:
            if os.path.exists(current):
                return current
        except (OSError, ValueError):
            return ""
        parent = os.path.dirname(current)
        if not parent or parent == current:
            return ""
        current = parent
    return ""


def _can_write(path: str) -> bool:
    """A real write test of the folder, or of the place where it will be made.

    A folder that does not exist yet is made for the test and taken away again, because
    on Windows "may make a folder here" and "may make a file here" are different rights.
    """
    try:
        if os.path.isdir(path):
            return fsutil.is_writable_dir(path)
        base = _nearest_existing(path)
        if not base or not os.path.isdir(base):
            return False
        made: List[str] = []
        current = path
        while current and current != base and not os.path.exists(current):
            made.append(current)
            current = os.path.dirname(current)
        try:
            os.makedirs(path, exist_ok=True)
            return fsutil.is_writable_dir(path)
        except OSError:
            return False
        finally:
            for folder in made:                       # deepest first; only what was made here
                try:
                    os.rmdir(folder)
                except OSError:
                    break
    except (OSError, ValueError):
        return False


def is_inside(path: str, parent: str) -> bool:
    """Is ``path`` the folder ``parent`` or something inside it? Letter case as the PC sees it."""
    return _inside(path, parent)


def _inside(path: str, parent: str) -> bool:
    try:
        a = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(parent))
        return a == b or a.startswith(b.rstrip(os.sep) + os.sep)
    except (OSError, ValueError):
        return False


def list_signature(samples: Iterable[Sample]) -> Tuple[Any, ...]:
    """What of the list matters to the results folder: names and read files of the rows that would run."""
    return tuple((s.name, tuple(s.lanes)) for s in samples if s.ready)


def inspect_folder(asked: str, samples: Sequence[Sample], trim: TrimSettings,
                   assembly: AssemblySettings, *, add_subfolder: bool = True) -> FolderFacts:
    """Look at the results folder. It touches the disk, so it belongs in a worker thread.

    ``samples`` are copies of the rows that would run. Nothing of the caller is changed and
    nothing is left behind: a folder that does not exist yet is not made (that is why the
    runner's own ``batch.preflight_batch``, which makes it, is not asked here; the Run page
    asks it when a run begins). With ``add_subfolder``, a folder that holds other things and
    is not one of our results folders gets ``TesserACT_results`` added (audit 4.2).
    """
    facts = FolderFacts(asked=asked, path=clean_folder_text(asked), signature=list_signature(samples))
    path = facts.path
    if not _is_usable_path(path):
        facts.valid = False
        return facts
    try:
        if os.path.exists(path) and not os.path.isdir(path):
            facts.valid = False
            return facts
        if not _nearest_existing(path):
            facts.valid = False                       # a drive that is not there, or not ready
            return facts
        if (add_subfolder and os.path.basename(path).casefold() != branding.RESULTS_DIR_NAME.casefold()
                and results.foreign_and_not_empty(path)):
            path = facts.path = os.path.join(path, branding.RESULTS_DIR_NAME)
            facts.appended = True
    except (OSError, ValueError):
        facts.valid = False
        return facts

    facts.drive = os.path.splitdrive(path)[0] if osapi.IS_WINDOWS else ""
    facts.encoding_ok = osapi.path_fits_system_encoding(path)
    facts.kind = osapi.drive_kind(path)
    facts.filesystem = osapi.filesystem_name(path)
    facts.synced = osapi.synced_folder_name(path)
    facts.inside_program = (appdirs.is_frozen() and _inside(path, appdirs.app_dir())
                            and not _inside(path, appdirs.example_dir()))
    facts.writable = _can_write(path)
    facts.free = fsutil.free_bytes(path)

    ready = [s for s in samples if s.ready]
    if os.path.isdir(path):
        try:
            facts.newer = results.made_by_newer_version(path)
            if not facts.newer:
                facts.holder = osapi.FolderLock.holder_text(path)
                found = results.existing_results(
                    path, ready, lambda s: settings_map.settings_fingerprint(trim, assembly, s.trim))
                # a result of that name made from other read files is another isolate (decision
                # D3): the row is left out and never reaches the overwrite-or-skip question
                facts.existing = {name: item for name, item in found.items() if item.same_reads}
                for name in results.name_clashes(path, ready):
                    record = results.read_status(path, name)
                    facts.clashes[name] = record.finished_at if record is not None else ""
                for name, item in found.items():
                    if not item.same_reads:
                        facts.clashes.setdefault(name, item.finished_at)
                for sample in ready:
                    if sample.name in facts.existing or sample.name in facts.clashes:
                        continue
                    kind = results.foreign_content(path, sample.name)
                    if kind:
                        facts.foreign[sample.name] = kind
                        continue
                    record = results.read_status(path, sample.name)
                    if record is not None and record.status is RunStatus.FAILED and not record.assembled:
                        facts.failed_before.append(sample.name)
        except results.NewerFormat:
            facts.newer = True
        except Exception:                             # a folder that vanished half-way, and the like
            log.warning("the results folder %s could not be read in full", path, exc_info=True)

    return facts


def suggest_results_dir(samples: Sequence[Sample]) -> str:
    """Audit 4.1: ``TesserACT_results`` beside the reads, if that place can be written and is
    not synced to a cloud; otherwise "". It tests the folder, so call it from a worker thread."""
    folders: List[str] = []
    for sample in samples:
        for pair in sample.lanes:
            for path in pair:
                if path:
                    folder = os.path.dirname(path)
                    if folder and folder not in folders:
                        folders.append(folder)
    if not folders:
        return ""
    base = folders[0]
    if len(folders) > 1:
        try:
            common = os.path.commonpath(folders)
        except ValueError:                            # different drives
            common = ""
        if common and os.path.dirname(common) != common:      # not the root of a drive
            base = common
    try:
        if not os.path.isdir(base) or osapi.synced_folder_name(base):
            return ""
        if osapi.drive_kind(base) in ("network", "removable"):
            return ""
        if not fsutil.is_writable_dir(base):
            return ""
    except (OSError, ValueError):
        return ""
    return os.path.join(base, branding.RESULTS_DIR_NAME)


# =========================================================================== the state

_ROW_ID = "_window_row_id"


def row_id_of(sample: Sample) -> str:
    """The id of the table row of a sample ("" for a sample that is not in the list)."""
    return sample.__dict__.get(_ROW_ID, "")


class AppState:
    """Everything the pages share. Changed only from the window's own thread."""

    def __init__(self) -> None:
        self.samples: List[Sample] = []
        self.trim = TrimSettings()                    # standard at every launch (decision D4)
        self.assembly = AssemblySettings()
        self.results_dir = ""                         # the folder in the field, cleaned
        self.results_suggested = False                # the program filled it in (audit 4.1)
        self.results_by_user = False                  # the user chose or typed it
        self.facts: Optional[FolderFacts] = None
        self.results_appended = False                 # a TesserACT_results folder was added (audit 4.2)
        #: demonstrations: ``fixed_verdict(state)`` stands in for the look at the disk
        self.fixed_verdict: Optional[Callable[["AppState"], FolderVerdict]] = None
        self.checking: set = set()                    # row ids whose files are being looked into
        self.searching = False                        # a folder search is under way
        self.engines: Optional[EnginePaths] = None
        self.engine_problem = ""                      # the red bar; Start stays disabled while it stands
        self.running = False
        self.demo = False
        self.example_loaded = False
        self.windows_rules = osapi.IS_WINDOWS         # path-length rule of Windows (tests can set it)
        self._listeners: List[Callable[[str], Any]] = []
        self._serial = 0

    # -- telling the pages
    def subscribe(self, listener: Callable[[str], Any]) -> None:
        self._listeners.append(listener)

    def notify(self, what: str) -> None:
        """``what``: samples, trim, assembly, results, engines, running."""
        for listener in list(self._listeners):
            try:
                listener(what)
            except Exception:
                log.exception("a page could not follow a change (%s)", what)

    # -- rows
    def adopt(self, sample: Sample) -> str:
        """Give a sample its row id (kept for as long as it is in the list)."""
        existing = row_id_of(sample)
        if existing:
            return existing
        self._serial += 1
        sample.__dict__[_ROW_ID] = "s%d" % self._serial
        return sample.__dict__[_ROW_ID]

    def sample(self, row_id: str) -> Optional[Sample]:
        for sample in self.samples:
            if row_id_of(sample) == row_id:
                return sample
        return None

    def set_samples(self, samples: Sequence[Sample]) -> None:
        self.samples = list(samples)
        for sample in self.samples:
            self.adopt(sample)
        alive = {row_id_of(s) for s in self.samples}
        self.checking &= alive

    # -- what will run
    def _folder_reason(self, sample: Sample) -> str:
        facts = self.facts
        if facts is None or not self.results_dir or facts.path != self.results_dir:
            return ""
        if sample.name in facts.clashes:
            when = short_date(facts.clashes[sample.name])
            return Words.SAME_NAME_OTHER_READS.format(name=sample.name, when="on %s " % when if when else "")
        if sample.name in facts.foreign:
            return Words.FOREIGN_FOLDER.format(name=sample.name, sep="\\" if osapi.IS_WINDOWS else os.sep,
                                               folder=facts.foreign[sample.name])
        return ""

    def view(self, sample: Sample) -> Tuple[SampleState, str]:
        """State and note of a row as shown: its own, or Left out because of what is
        already in the results folder (decision D3, audit 3.7)."""
        if sample.ready:
            reason = self._folder_reason(sample)
            if reason:
                return SampleState.LEFT_OUT, reason
        return sample.state, sample.note

    def ready(self) -> List[Sample]:
        return [s for s in self.samples if self.view(s)[0] is not SampleState.LEFT_OUT]

    def left_out(self) -> List[Sample]:
        return [s for s in self.samples if self.view(s)[0] is SampleState.LEFT_OUT]

    def any_trim(self) -> bool:
        """Is at least one sample that will run set to be trimmed?"""
        return any(s.trim for s in self.ready())

    def trimming_off(self) -> bool:
        """True when there are samples and not one of them has Trim ticked: the Trimming
        page is then greyed. An empty list leaves the page usable, to look around."""
        return bool(self.samples) and not any(s.trim for s in self.samples)

    def has_name_clash(self) -> bool:
        return any(pairing.is_name_clash(s) for s in self.samples)

    # -- the results folder
    def set_results_dir(self, path: str, *, suggested: bool = False, by_user: bool = False) -> None:
        self.results_dir = path
        self.results_appended = False
        self.results_suggested = suggested and bool(path)
        self.results_by_user = by_user and bool(path)

    def facts_current(self) -> bool:
        """Are the facts about the folder still about this folder and this list?"""
        facts = self.facts
        return (facts is not None and facts.path == self.results_dir
                and facts.signature == list_signature(self.samples))


# =========================================================================== verdicts

def _drive_words(facts: FolderFacts) -> str:
    if facts.drive and len(facts.drive) == 2:
        return Words.DRIVE.format(letter=facts.drive.upper())
    return Words.THIS_DRIVE


def judge_folder(state: AppState) -> FolderVerdict:
    """The sentences under the results folder, from the facts and the list as they are now."""
    if state.fixed_verdict is not None:
        return state.fixed_verdict(state)
    verdict = FolderVerdict()
    if not state.results_dir:
        return verdict
    facts = state.facts
    if facts is None or facts.path != state.results_dir:
        verdict.pending = True
        verdict.info = texts.RESULTS_FOLDER_NOTE
        return verdict
    ready = state.ready()
    keep = state.trim.keep_trimmed_reads
    need = pairing.estimate_disk_need(ready, keep) if ready else 0
    verdict.need, verdict.free = need, facts.free
    problem = ""
    if not facts.valid:
        problem = Words.RESULTS_INVALID
    elif facts.newer:
        problem = Words.RESULTS_NEWER
    elif facts.holder:
        problem = (Words.RESULTS_IN_USE_UNKNOWN if facts.holder == osapi.UNKNOWN_HOLDER
                   else Words.RESULTS_IN_USE.format(holder=facts.holder))
    elif not facts.encoding_ok:
        problem = Words.RESULTS_ENCODING
    elif not facts.writable:
        problem = Words.RESULTS_NOT_WRITABLE
    elif (state.windows_rules and ready
          and len(pairing.longest_result_path(facts.path, ready)) > pairing.MAX_RESULT_PATH):
        problem = Words.RESULTS_TOO_LONG
    else:
        if facts.filesystem.upper() == "FAT32":
            for sample in ready:
                if sample.size_bytes // 2 >= 4 * 1024 ** 3:
                    problem = Words.RESULTS_FAT32.format(sample=sample.name)
                    break
        if not problem and ready and facts.free is not None and need > facts.free:
            problem = Words.RESULTS_NO_SPACE.format(need=fsutil.format_bytes(need), drive=_drive_words(facts),
                                                    free=fsutil.format_bytes(facts.free))
            less = pairing.estimate_disk_need(ready, False)
            if keep and any(s.trim for s in ready) and less < need:
                problem += Words.RESULTS_NO_SPACE_ADVICE_KEEP.format(less=fsutil.format_bytes(less))
            else:
                problem += Words.RESULTS_NO_SPACE_ADVICE
    verdict.problem = problem
    verdict.short = first_sentence(problem)

    info = [texts.RESULTS_FOLDER_NOTE]
    if facts.valid and ready and facts.free is not None:
        info.append(texts.space_line(fsutil.format_bytes(need), facts.drive, fsutil.format_bytes(facts.free)))
    verdict.info = " ".join(info)
    if facts.valid:
        if facts.appended or state.results_appended:
            verdict.notes.append(Words.RESULTS_NEW_FOLDER.format(path=facts.path))
        elif state.results_suggested:
            verdict.notes.append(texts.RESULTS_FOLDER_SUGGESTED)
        if facts.inside_program:
            verdict.notes.append(Words.RESULTS_INSIDE_PROGRAM)
        if facts.synced:
            verdict.notes.append(Words.RESULTS_SYNCED.format(name=facts.synced))
        elif facts.kind == "network":
            verdict.notes.append(Words.RESULTS_NETWORK)
        elif facts.kind == "removable":
            verdict.notes.append(Words.RESULTS_REMOVABLE)
        names = {s.name for s in ready}
        verdict.existing = texts.existing_line(len([n for n in facts.existing if n in names]),
                                               len([n for n in facts.failed_before if n in names]))
    return verdict


def settings_problems(state: AppState) -> List[Tuple[str, str, str]]:
    """What ``settings_map`` refuses, as ``(step key, field, plain message)``. Trimming is
    looked at only when a sample will be trimmed."""
    found: List[Tuple[str, str, str]] = []
    if state.any_trim():
        found.extend(("trimming", name, message) for name, message in settings_map.validate_trim(state.trim))
    found.extend(("assembly", name, message) for name, message in settings_map.validate_assembly(state.assembly))
    return found


@dataclass
class StartStatus:
    """The Start button: whether it can be pressed, its count, and the words beside it."""
    enabled: bool
    count: int                        # samples that would run
    reason: str = ""                  # why it is disabled
    link: str = ""                    # "2 left out: why?"
    hint: str = ""                    # the first-time hint, when nothing else is to be said
    left_out: int = 0

    @property
    def label(self) -> str:
        return texts.start_label(self.count)


def start_status(state: AppState, step_key: str = "samples") -> StartStatus:
    """Decide the Start button for the list, folder and settings as they are (audit A-6)."""
    total = len(state.samples)
    ready = len(state.ready())
    left = total - ready
    _plain, link = texts.ready_counter(ready, left)

    def no(reason: str) -> StartStatus:
        return StartStatus(False, ready, reason, left_out=left)

    if state.running:
        return no(Words.RUN_IN_PROGRESS)
    if state.engine_problem:
        return no(Words.PROGRAM_PART_MISSING)
    reason = texts.start_reason(total, ready, bool(state.results_dir))
    if reason:
        if state.searching and total == 0:
            return no(Words.CHECKING_FILES)
        return no(reason)
    if state.checking or state.searching:
        return no(Words.CHECKING_FILES)
    verdict = judge_folder(state)
    if verdict.pending:
        return no(Words.CHECKING_FOLDER)
    if verdict.problem:
        return no(verdict.short + (Words.FOLDER_ON_SAMPLES_STEP if step_key != "samples" else ""))
    problems = settings_problems(state)
    if problems:
        return no(problems[0][2])
    hint = texts.FIRST_TIME_HINT if (step_key == "samples" and not left) else ""
    return StartStatus(True, ready, "", link, hint, left)


def step_lines(state: AppState) -> Dict[str, Tuple[str, str]]:
    """The small lines under the first three step names: text and colour kind."""
    ready = len(state.ready())
    if state.trimming_off():
        trimming = (texts.STEP_NO_TRIMMING, "muted")
    elif trim_changed(state.trim):
        trimming = (texts.STEP_CHANGED, "accent")
    else:
        trimming = (texts.STEP_STANDARD, "muted")
    assembly = (texts.STEP_CHANGED, "accent") if assembly_changed(state.assembly) else (texts.STEP_STANDARD, "muted")
    return {"samples": (texts.step_ready(ready), "muted"), "trimming": trimming, "assembly": assembly}


def build_config(state: AppState, existing: Existing = Existing.SKIP) -> RunConfig:
    """The batch as the runner gets it: copies of the samples that will run, in list order,
    and copies of the settings, so that nothing the window does later can change a run."""
    samples = []
    for sample in state.ready():
        state_now, note = state.view(sample)
        samples.append(Sample(name=sample.name, lanes=[tuple(pair) for pair in sample.lanes], trim=sample.trim,
                              size_bytes=sample.size_bytes, state=state_now, note=note))
    return RunConfig(results_dir=state.results_dir, samples=samples, trim=dataclasses.replace(state.trim),
                     assembly=dataclasses.replace(state.assembly), existing=existing)


def left_out_rows(state: AppState) -> List[Tuple[str, str]]:
    """``(sample name, reason)`` for every row that will not run, for the summaries."""
    return [(sample.name, state.view(sample)[1]) for sample in state.left_out()]


def recheck_files(state: AppState, is_online_only: Optional[Callable[[str], bool]] = None) -> List[Sample]:
    """Audit 2.21: when Start is pressed, are the read files still there?

    A sample with a file that has gone is looked at again and becomes Left out with the
    reason. Only the existence of files is tested for the others, which is quick. Returns
    the samples that changed.
    """
    changed: List[Sample] = []
    for sample in state.ready():
        missing = False
        for pair in sample.lanes:
            for path in pair:
                try:
                    if not path or not os.path.isfile(path):
                        missing = True
                except (OSError, ValueError):
                    missing = True
        if missing:
            pairing.check_sample(sample, is_online_only)
            changed.append(sample)
    if changed:
        pairing.mark_name_clashes(state.samples)
    return changed


def checked_copy(sample: Sample, is_online_only: Optional[Callable[[str], bool]] = None) -> Sample:
    """Look into the files of a sample without touching the row itself (worker thread):
    a copy is checked and handed back."""
    twin = copy.copy(sample)
    twin.lanes = list(sample.lanes)
    return pairing.check_sample(twin, is_online_only)


# =========================================================================== demonstration

def demo_samples() -> List[Sample]:
    """Eight made-up rows in mixed states, for screenshots (``--demo``). No file exists."""
    folder = "D:\\Sequencing\\Run 2026-11-12 (Klebsiella)\\"
    mb = 1024 ** 2
    rows: List[Sample] = []

    def pair(stem: str, number: int, lanes: int = 1) -> List[Tuple[str, str]]:
        return [(folder + "%s_S%d_L%03d_R1_001.fastq.gz" % (stem, number, lane),
                 folder + "%s_S%d_L%03d_R2_001.fastq.gz" % (stem, number, lane)) for lane in range(1, lanes + 1)]

    rows.append(Sample("KP_001", pair("KP_001", 1), True, 412 * mb))
    rows.append(Sample("KP_002", pair("KP_002", 2, 4), True, 1557 * mb))
    rows.append(Sample("KP_003", pair("KP_003", 3), False, 401 * mb))
    rows.append(Sample("Klebsiella_pneumoniae_ward7_isolate_2026_0041_rerun",
                       pair("Klebsiella pneumoniae ward7 isolate 2026 0041 (rerun)", 4), True, 4403 * mb,
                       SampleState.READY_NOTE, pairing.HUGE))
    rows.append(Sample("SA_17", [(folder + "SA_17_trimmed_R1.fastq.gz", folder + "SA_17_trimmed_R2.fastq.gz")],
                       False, 251 * mb, SampleState.READY_NOTE, pairing.ALREADY_TRIMMED))
    rows.append(Sample("EC_09", [(folder + "EC_09_R1.fastq.gz", "")], True, 198 * mb,
                       SampleState.LEFT_OUT, pairing.NO_READ2))
    rows.append(Sample("KP_007", pair("KP_007", 7), True, 419 * mb))
    rows.append(Sample("PA_12", [(folder + "PA_12_1.fq.gz", folder + "PA_12_2.fq.gz")], True, 11 * mb,
                       SampleState.READY_NOTE, pairing.TINY))
    return rows


def demo_verdict(state: AppState) -> FolderVerdict:
    """What a real folder check would say for the demonstration rows."""
    if not state.results_dir or not state.samples:
        return FolderVerdict(info=texts.RESULTS_FOLDER_NOTE)
    need = pairing.estimate_disk_need(state.ready(), state.trim.keep_trimmed_reads)
    free = 212 * 1024 ** 3
    return FolderVerdict(
        info="%s %s" % (texts.RESULTS_FOLDER_NOTE, texts.space_line(fsutil.format_bytes(need), "D:",
                                                                    fsutil.format_bytes(free))),
        notes=[texts.RESULTS_FOLDER_SUGGESTED], existing=texts.existing_line(2), need=need, free=free)


def demo_threads() -> int:
    return default_threads()
