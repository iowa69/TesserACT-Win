# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""What is on disk in the results folder: decision D1, and the rules that keep it safe.

Layout (every name is built here and nowhere else)::

    <results>/
      run_summary.html  run_summary.tsv  run_log.txt  last_run.json  tesseract-win.lock
      assemblies/<sample>.fasta
      <sample>/
        sample_status.json     written last; the only proof that a sample is finished
        trimming/  assembly/   the finished result
        _in_progress/          the attempt being made (its own trimming/, assembly/, joined/,
                               and attempt.json, the mark that the folder is ours)
        last_failed_attempt/   logs of a failed attempt, replaced each time
        new_result/            only when the old result could not be replaced (a file was open)

How a result is put in place (the order a caller follows for a good attempt)::

    begin_attempt()  ...the engines work in _in_progress...  commit_attempt()  collect()  write_status()

``commit_attempt`` moves the old ``trimming``/``assembly`` aside *inside* ``_in_progress`` and the
new ones into place. ``write_status`` is the point of no return: only after the new status file is
on disk is the old result deleted. A stop, crash or power cut anywhere before that is undone by
the next ``begin_attempt``, ``abandon_attempt`` or ``recover_interrupted``: the earlier result,
its status file and its file in ``assemblies`` come back exactly as they were. While a swap is
under way there is no status file, so :func:`is_assembled` cannot say yes to half a result.

When ``commit_attempt`` says ``in_place`` is False the result is in ``<sample>/new_result``: the
caller skips ``collect`` (there is nothing new in place to collect) and calls ``write_status``,
which then writes beside the new result and never vouches for the folders still in place.

Reading functions never change anything, so they are safe on a folder another run is using.
``save_last_run`` may be called from two threads at once (the window notes a shutdown while the
batch thread records a change). No Tk, no engines; every write goes through :mod:`fsutil`.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from . import branding, fsutil, versions
from .model import EndReason, ExistingResult, RunConfig, RunStatus, Sample, SampleOutcome

__all__ = [
    # paths
    "summary_html_path", "summary_tsv_path", "run_log_path", "last_run_path", "lock_path",
    "assemblies_dir", "collected_fasta", "sample_dir", "status_path", "trimming_dir",
    "assembly_dir", "in_progress_dir", "in_progress_trimming_dir", "in_progress_assembly_dir",
    "in_progress_joined_dir", "last_failed_dir", "new_result_dir", "contigs_path",
    "report_html_path", "trimming_report_html_path", "trimmed_read_names", "is_reserved_name",
    # status files
    "NewerFormat", "AttemptIncomplete", "SampleRecord", "write_status", "read_status",
    "is_assembled", "all_records", "DONE_STATUSES", "whole_number", "seconds_of",
    # existing results
    "reads_of", "same_reads", "existing_results", "name_clashes", "foreign_content",
    # attempts
    "CommitResult", "begin_attempt", "commit_attempt", "abandon_attempt", "kept_log_path", "collect",
    "recover_interrupted",
    # the memory of a batch
    "LastRun", "save_last_run", "load_last_run", "pending_samples", "failed_samples",
    "was_interrupted",
    # the folder as a whole
    "is_results_folder", "foreign_and_not_empty", "made_by_newer_version", "count_assemblies",
]

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- names (decision D1)

SUMMARY_HTML = "run_summary.html"
SUMMARY_TSV = "run_summary.tsv"
RUN_LOG = "run_log.txt"
LAST_RUN = "last_run.json"
LOCK_FILE = "tesseract-win.lock"
ASSEMBLIES = "assemblies"
STATUS_FILE = "sample_status.json"
TRIMMING = "trimming"
ASSEMBLY = "assembly"
IN_PROGRESS = "_in_progress"
JOINED = "joined"
LAST_FAILED = "last_failed_attempt"
NEW_RESULT = "new_result"
CONTIGS = "contigs.fasta"
REPORT_HTML = "report.html"
ASSEMBLY_LOG = "assembly.log"
PREFLIGHT = "preflight.txt"
SUMMARY_TXT = "SUMMARY.txt"
TRIMMING_REPORT_HTML = "trimming_report.html"
TRIMMING_REPORT_JSON = "trimming_report.json"
TRIMMING_LOG = "trimming.log"

# Private to this module, inside _in_progress while a swap is under way.
_ATTEMPT_NOTE = "attempt.json"     # written by begin_attempt: this _in_progress is ours
_JOURNAL = "commit.json"
_REPLACED = "replaced"
_NEW_RESULT_NOTE = "README.txt"
_OWNS_FOLDERS = "folders_are_ours"   # in a status file that says failed; see SampleRecord.owns_folders

# fsutil names its temporary file after the process, so two threads writing one file at the same
# moment would share it. last_run.json is the one file two threads may write; this keeps them apart.
_LAST_RUN_LOCK = threading.Lock()

#: A finished sample: these two values of :class:`RunStatus` are what "says done" means.
DONE_STATUSES = (RunStatus.DONE, RunStatus.DONE_NOTES)

_FASTQ_ENDINGS = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
_HALF_COPY = re.compile(r"\.fasta\.tmp\d+$")      # the temporary name collect() copies under
_IGNORED_IN_EMPTY_FOLDER = {"desktop.ini", "thumbs.db", ".ds_store"}
_SCAN_LIMIT = 2000        # sub-folders looked into when deciding whether a folder is ours


# ----- the results folder itself

def summary_html_path(results_dir: str) -> str:
    return os.path.join(results_dir, SUMMARY_HTML)


def summary_tsv_path(results_dir: str) -> str:
    return os.path.join(results_dir, SUMMARY_TSV)


def run_log_path(results_dir: str) -> str:
    return os.path.join(results_dir, RUN_LOG)


def last_run_path(results_dir: str) -> str:
    return os.path.join(results_dir, LAST_RUN)


def lock_path(results_dir: str) -> str:
    return os.path.join(results_dir, LOCK_FILE)


def assemblies_dir(results_dir: str) -> str:
    return os.path.join(results_dir, ASSEMBLIES)


def collected_fasta(results_dir: str, name: str) -> str:
    """``assemblies/<sample>.fasta``: the copy of a finished sample's contigs."""
    return os.path.join(results_dir, ASSEMBLIES, name + ".fasta")


# ----- one sample

def sample_dir(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name)


def status_path(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, STATUS_FILE)


def trimming_dir(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, TRIMMING)


def assembly_dir(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, ASSEMBLY)


def in_progress_dir(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, IN_PROGRESS)


def in_progress_trimming_dir(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, IN_PROGRESS, TRIMMING)


def in_progress_assembly_dir(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, IN_PROGRESS, ASSEMBLY)


def in_progress_joined_dir(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, IN_PROGRESS, JOINED)


def last_failed_dir(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, LAST_FAILED)


def new_result_dir(results_dir: str, name: str) -> str:
    """Where a new result waits when the old one could not be replaced (audit row 5.6)."""
    return os.path.join(results_dir, name, NEW_RESULT)


def contigs_path(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, ASSEMBLY, CONTIGS)


def report_html_path(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, ASSEMBLY, REPORT_HTML)


def trimming_report_html_path(results_dir: str, name: str) -> str:
    return os.path.join(results_dir, name, TRIMMING, TRIMMING_REPORT_HTML)


def trimmed_read_names(name: str) -> Tuple[str, str]:
    """The bare file names of a sample's trimmed reads (decision D1)."""
    return (name + "_trimmed_R1.fastq.gz", name + "_trimmed_R2.fastq.gz")


_RESERVED = {ASSEMBLIES, SUMMARY_HTML, SUMMARY_TSV, RUN_LOG, LAST_RUN, LOCK_FILE}


def is_reserved_name(name: str) -> bool:
    """True for a sample name that would collide with one of the results folder's own entries."""
    return name.casefold() in {entry.casefold() for entry in _RESERVED}


# --------------------------------------------------------------------------- status files

class NewerFormat(Exception):
    """A file in the results folder was written by a newer program. The folder must be refused."""

    def __init__(self, path: str, found: int):
        super().__init__("%s has format %s; this program understands up to %d"
                         % (path, found, versions.RESULTS_FORMAT))
        self.path = path
        self.found = found


class AttemptIncomplete(Exception):
    """There is no complete assembly to put in place or to vouch for (no ``contigs.fasta``)."""


@dataclass
class SampleRecord:
    """What ``sample_status.json`` says about a sample.

    ``reads`` holds ``(file name, size in bytes)`` for every read file the result was made from:
    names, not paths, so a results folder that was moved still matches. The paths inside
    ``outcome`` (``folder``, ``report_html``, ``log``) are rebuilt for where the folder is now.
    """
    name: str
    status: RunStatus
    outcome: SampleOutcome
    finished_at: str = ""
    trimmed: bool = False
    reads: List[Tuple[str, int]] = field(default_factory=list)
    settings_fingerprint: str = ""
    settings_text: str = ""
    program_version: str = ""
    tesseract_version: str = ""
    scepter_version: str = ""
    format: int = versions.RESULTS_FORMAT
    #: A later attempt that failed and left this finished result in place:
    #: ``{"status", "note", "next_step", "finished_at"}``. None when there was none.
    last_attempt: Optional[Dict[str, Any]] = None
    #: ``assembly/contigs.fasta`` was there when the record was read.
    assembly_present: bool = False
    #: ``assembly`` and ``trimming`` beside the status file are this program's own: the status
    #: says done, or it says failed after a finished result (whose contigs had been taken away).
    owns_folders: bool = False

    @property
    def done(self) -> bool:
        """The status file says the sample finished."""
        return self.status in DONE_STATUSES

    @property
    def assembled(self) -> bool:
        """Decision D2: the status file says done **and** ``assembly/contigs.fasta`` exists."""
        return self.done and self.assembly_present


def _int_or_none(value: Any) -> Optional[int]:
    """A whole number from whatever a file or a caller holds (71, 71.0, "71"); None for the rest."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _seconds(value: Any) -> float:
    """A length of time from a file or a caller: a finite number that is not negative, else 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        number = float(value)
    except OverflowError:
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


whole_number = _int_or_none          # the same rules for the summaries
seconds_of = _seconds


def _inside(folder: str, path: str) -> Optional[List[str]]:
    """The parts of ``path`` below ``folder``, or None when it is not inside it."""
    if not path:
        return None
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(folder))
    except ValueError:          # another drive on Windows
        return None
    if os.altsep:
        rel = rel.replace(os.altsep, os.sep)
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    if not parts or parts[0] == "..":
        return None
    return parts


def _portable(folder: str, path: str, done: bool) -> str:
    """A path as it is stored: relative to the sample folder, where the file will be found later.

    A path that still points into ``_in_progress`` is mapped to where its file ends up: the sample
    folder for a finished sample, ``last_failed_attempt`` for a failed one.
    """
    parts = _inside(folder, path)
    if parts is None:
        return path
    if parts[0] == IN_PROGRESS:
        parts = parts[1:] if done else [LAST_FAILED] + parts[1:]
    return "/".join(parts)


def _status_document(outcome: SampleOutcome, folder: str, *, reads: Iterable[Tuple[str, int]],
                     settings_fingerprint: str, settings_text: str, trimmed: bool) -> Dict[str, Any]:
    done = outcome.status in DONE_STATUSES
    stored = outcome.to_dict()
    stored["trimmed"] = bool(trimmed)
    stored["folder"] = ""
    stored["report_html"] = _portable(folder, outcome.report_html, done)
    stored["log"] = _portable(folder, outcome.log, done)
    finished = outcome.finished_at or fsutil.now_iso()
    stored["finished_at"] = finished
    return {
        "format": versions.RESULTS_FORMAT,
        "program": branding.APP_NAME,
        "program_version": versions.APP_VERSION,
        "tesseract_version": versions.TESSERACT_VERSION,
        "scepter_version": versions.SCEPTER_VERSION,
        "name": outcome.name,
        "status": outcome.status.value,
        "finished_at": finished,
        "trimmed": bool(trimmed),
        "reads": [{"name": os.path.basename(str(path).replace("\\", "/")), "size": int(size),
                   "path": str(path)} for path, size in reads],
        "settings_fingerprint": settings_fingerprint,
        "settings_text": settings_text,
        "outcome": stored,
        "last_attempt": None,
    }


def write_status(results_dir: str, outcome: SampleOutcome, *,
                 reads: Iterable[Tuple[str, int]], settings_fingerprint: str,
                 settings_text: str, trimmed: bool) -> str:
    """Record how a sample ended. Returns the path of the file written, or "" if none was.

    * **Done** (``DONE``, ``DONE_NOTES``), straight after :func:`commit_attempt` and
      :func:`collect`: writes ``sample_status.json`` atomically. This is the moment the new result
      becomes the result; only now is the replaced one deleted, with the rest of ``_in_progress``.
      If the commit left the result in ``new_result`` (the old one could not be replaced), the
      file is written there instead, however often this is called, and the status of the result
      still in place is left alone: a "done" never vouches for folders it does not describe.
      Raises :class:`AttemptIncomplete` when there is no ``contigs.fasta`` to vouch for.
    * **Failed**: an in-flight swap is undone first. If a finished result is in place, its status
      file stays (the failure is noted in it under ``last_attempt``); otherwise a status file
      saying "failed" is written, so the next run and the summary know why. (If it replaces a
      "done" whose ``contigs.fasta`` was taken away, it records that the folders are still ours.)
    * Anything else (stopped, skipped, not started): an in-flight swap is undone; nothing is written.
    """
    name = outcome.name
    folder = sample_dir(results_dir, name)
    target = status_path(results_dir, name)
    work = in_progress_dir(results_dir, name)
    document_args = dict(reads=list(reads), settings_fingerprint=settings_fingerprint,
                         settings_text=settings_text, trimmed=trimmed)

    if outcome.status in DONE_STATUSES:
        in_flight = _read_journal(work) is not None and not os.path.lexists(target)
        waiting = new_result_dir(results_dir, name)
        if in_flight:
            if not os.path.isfile(contigs_path(results_dir, name)):
                raise AttemptIncomplete("no %s in %s" % (CONTIGS, assembly_dir(results_dir, name)))
            fsutil.atomic_write_json(target, _status_document(outcome, folder, **document_args))
            # The new result is proven. What it replaced, and anything stale, can go now.
            _remove_marked(work, _ATTEMPT_NOTE)
            if _is_our_new_result(waiting):
                _remove_marked(waiting, _NEW_RESULT_NOTE)
            return target
        if _is_our_new_result(waiting) and os.path.isfile(os.path.join(waiting, ASSEMBLY, CONTIGS)):
            # The newest attempt is the one waiting in new_result (a later one that went into place
            # would have removed it above): this "done" is about it, not about what is in place.
            beside = os.path.join(waiting, STATUS_FILE)
            fsutil.atomic_write_json(beside, _status_document(outcome, waiting, **document_args))
            return beside
        if not os.path.isfile(contigs_path(results_dir, name)):
            raise AttemptIncomplete("no %s in %s" % (CONTIGS, assembly_dir(results_dir, name)))
        fsutil.atomic_write_json(target, _status_document(outcome, folder, **document_args))
        return target

    _undo_in_flight_commit(results_dir, name)
    if outcome.status is not RunStatus.FAILED:
        return ""
    earlier = fsutil.read_json(target)
    if isinstance(earlier, dict):
        found = _int_or_none(earlier.get("format"))
        if found is not None and found > versions.RESULTS_FORMAT:
            raise NewerFormat(target, found)
        if (earlier.get("status") in tuple(s.value for s in DONE_STATUSES)
                and os.path.isfile(contigs_path(results_dir, name))):
            earlier["last_attempt"] = {
                "status": outcome.status.value, "note": outcome.note,
                "next_step": outcome.next_step,
                "finished_at": outcome.finished_at or fsutil.now_iso(),
            }
            fsutil.atomic_write_json(target, earlier)
            return target
    document = _status_document(outcome, folder, **document_args)
    if isinstance(earlier, dict) and (earlier.get("status") in tuple(s.value for s in DONE_STATUSES)
                                      or earlier.get(_OWNS_FOLDERS) is True):
        # A finished result was here and its contigs.fasta is gone. What is left of its folders is
        # still ours to replace; without this note the next attempt would take them for a
        # stranger's and the sample would be left out for good (audit row 3.7).
        document[_OWNS_FOLDERS] = True
    fsutil.atomic_write_json(target, document)
    return target


def _record_from(data: Any, results_dir: str, name: str, path: str) -> Optional[SampleRecord]:
    if not isinstance(data, dict):
        return None
    found = _int_or_none(data.get("format"))
    if found is None:
        return None
    if found > versions.RESULTS_FORMAT:
        raise NewerFormat(path, found)
    try:
        status = RunStatus(data.get("status"))
    except ValueError:
        return None
    folder = sample_dir(results_dir, name)
    stored = data.get("outcome")
    stored = dict(stored) if isinstance(stored, dict) else {}
    stored["name"] = name
    stored["status"] = status.value
    try:
        outcome = SampleOutcome.from_dict(stored)
    except (TypeError, ValueError):
        outcome = SampleOutcome(name=name, status=status)
    for number in ("contigs", "total_length", "largest", "n50", "depth"):
        setattr(outcome, number, _int_or_none(getattr(outcome, number)))
    outcome.note = outcome.note if isinstance(outcome.note, str) else ""
    outcome.next_step = outcome.next_step if isinstance(outcome.next_step, str) else ""
    outcome.notes = [n for n in outcome.notes if isinstance(n, str)] \
        if isinstance(outcome.notes, list) else []
    outcome.seconds = _seconds(outcome.seconds)
    finished = data.get("finished_at")
    finished = finished if isinstance(finished, str) else ""
    outcome.finished_at = finished or (outcome.finished_at if isinstance(outcome.finished_at, str) else "")
    outcome.trimmed = bool(data.get("trimmed", outcome.trimmed))
    outcome.folder = folder
    report = report_html_path(results_dir, name)
    outcome.report_html = report if os.path.isfile(report) else ""
    kept_log = outcome.log if isinstance(outcome.log, str) else ""
    if kept_log and not os.path.isabs(kept_log):
        kept_log = os.path.join(folder, *kept_log.split("/"))
    outcome.log = kept_log if kept_log and os.path.isfile(kept_log) else ""

    reads: List[Tuple[str, int]] = []
    recorded = data.get("reads")
    for item in recorded if isinstance(recorded, list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            size = _int_or_none(item.get("size"))
            reads.append((item["name"], -1 if size is None else size))
    last_attempt = data.get("last_attempt")

    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    return SampleRecord(
        name=name, status=status, outcome=outcome, finished_at=outcome.finished_at,
        trimmed=outcome.trimmed, reads=reads,
        settings_fingerprint=text("settings_fingerprint"), settings_text=text("settings_text"),
        program_version=text("program_version"), tesseract_version=text("tesseract_version"),
        scepter_version=text("scepter_version"), format=found,
        last_attempt=last_attempt if isinstance(last_attempt, dict) else None,
        assembly_present=os.path.isfile(contigs_path(results_dir, name)),
        owns_folders=status in DONE_STATUSES or data.get(_OWNS_FOLDERS) is True)


def read_status(results_dir: str, name: str) -> Optional[SampleRecord]:
    """The sample's status file, or None when it is missing or damaged.

    Raises :class:`NewerFormat` for a file written by a newer program.
    """
    path = status_path(results_dir, name)
    return _record_from(fsutil.read_json(path), results_dir, name, path)


def is_assembled(results_dir: str, name: str) -> bool:
    """Decision D2: the status file says done **and** ``assembly/contigs.fasta`` exists."""
    record = read_status(results_dir, name)
    return record is not None and record.assembled


def all_records(results_dir: str) -> List[SampleRecord]:
    """Every sample in the folder that has a readable status file, naturally sorted by name."""
    records: List[SampleRecord] = []
    try:
        entries = list(os.scandir(results_dir))
    except OSError:
        return records
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        path = os.path.join(entry.path, STATUS_FILE)
        data = fsutil.read_json(path)
        if data is None:
            continue
        record = _record_from(data, results_dir, entry.name, path)
        if record is not None:
            records.append(record)
    records.sort(key=lambda r: fsutil.natural_key(r.name))
    return records


# --------------------------------------------------------------------------- existing results

def reads_of(sample: Sample) -> List[Tuple[str, int]]:
    """``(path, size)`` for every read file of a sample, in lane order; size -1 if unreadable.

    This is the ``reads`` argument of :func:`write_status`.
    """
    out: List[Tuple[str, int]] = []
    for pair in sample.lanes:
        for path in pair:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = -1
            out.append((path, size))
    return out


def _read_key(path_or_name: str) -> str:
    return os.path.basename(str(path_or_name).replace("\\", "/")).casefold()


def same_reads(recorded: Sequence[Tuple[str, int]], current: Sequence[Tuple[str, int]]) -> bool:
    """Were two sets of read files the same files? File names and sizes, never full paths.

    Names are compared without regard to case (Windows does not distinguish them). A size that
    is unknown (-1, the file cannot be reached just now) is not held against a matching name.
    """
    if len(recorded) != len(current):
        return False
    old = sorted((_read_key(n), int(s)) for n, s in recorded)
    new = sorted((_read_key(n), int(s)) for n, s in current)
    for (old_name, old_size), (new_name, new_size) in zip(old, new):
        if old_name != new_name:
            return False
        if old_size >= 0 and new_size >= 0 and old_size != new_size:
            return False
    return True


def _version_tuple(text: str) -> Tuple[int, ...]:
    parts = []
    for piece in text.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _engine_difference(record: SampleRecord) -> str:
    """"" when the result was made with today's engines, else words naming the version."""
    differing = []
    if record.tesseract_version != versions.TESSERACT_VERSION:
        differing.append(("TesserACT", record.tesseract_version, versions.TESSERACT_VERSION))
    if record.trimmed and record.scepter_version != versions.SCEPTER_VERSION:
        differing.append(("scepter", record.scepter_version, versions.SCEPTER_VERSION))
    if not differing:
        return ""
    older = all(_version_tuple(then) < _version_tuple(now) for _, then, now in differing)
    newer = all(_version_tuple(then) > _version_tuple(now) for _, then, now in differing)
    which = "an earlier version" if older else "a later version" if newer else "another version"
    named = ", ".join("%s %s" % (engine, then or "unknown") for engine, then, _ in differing)
    return "%s (%s)" % (which, named)


FingerprintFor = Union[Callable[[Sample], str], Mapping[str, str], str]


def _fingerprint(settings_fingerprint_for: FingerprintFor, sample: Sample) -> str:
    if callable(settings_fingerprint_for):
        return settings_fingerprint_for(sample)
    if isinstance(settings_fingerprint_for, str):
        return settings_fingerprint_for
    return settings_fingerprint_for.get(sample.name, "")


def existing_results(results_dir: str, samples: Iterable[Sample],
                     settings_fingerprint_for: FingerprintFor) -> Dict[str, ExistingResult]:
    """The samples of the list that are already assembled here (decision D2), by name.

    ``settings_fingerprint_for(sample)`` gives the fingerprint of the settings now on the pages
    for that sample (a plain string, or a dict by sample name, is accepted too). A result made
    with another version of an engine counts as made with different settings (audit row 11.4);
    ``settings_summary`` then says how it was made, e.g. ``"not trimmed; mode Careful"`` or
    ``"an earlier version (TesserACT 1.6.1)"``.
    """
    found: Dict[str, ExistingResult] = {}
    for sample in samples:
        record = read_status(results_dir, sample.name)
        if record is None or not record.assembled:
            continue
        same_fingerprint = record.settings_fingerprint == _fingerprint(settings_fingerprint_for, sample)
        engines = _engine_difference(record)
        same_settings = same_fingerprint and not engines
        pieces = []
        if not same_fingerprint:
            pieces.append(record.settings_text or "other settings")
        if engines:
            pieces.append(engines)
        found[sample.name] = ExistingResult(
            name=sample.name, finished_at=record.finished_at,
            same_reads=same_reads(record.reads, reads_of(sample)),
            same_settings=same_settings,
            settings_summary="" if same_settings else "; ".join(pieces))
    return found


def name_clashes(results_dir: str, samples: Iterable[Sample]) -> List[str]:
    """Names whose finished result here was made from **different** read files (decision D3).

    Such a sample is another isolate with the same name: it must be left out, never skipped and
    never overwritten. File names and sizes are compared, not full paths. A sample whose read
    files cannot be reached just now is not reported: that is a different problem with its own
    message.
    """
    clashes: List[str] = []
    for sample in samples:
        record = read_status(results_dir, sample.name)
        if record is None or not record.assembled:
            continue
        current = reads_of(sample)
        if any(size < 0 for _, size in current):
            if sorted(_read_key(n) for n, _ in record.reads) == sorted(_read_key(p) for p, _ in current):
                continue
        if not same_reads(record.reads, current):
            clashes.append(sample.name)
    return clashes


def foreign_content(results_dir: str, name: str) -> str:
    """Audit row 3.7: ``"assembly"`` or ``"trimming"`` when the sample's folder already holds a
    folder of that name that this program did not make; "" otherwise.

    Ours means: the folder carries a status file that says done (or failed after a finished
    result: :attr:`SampleRecord.owns_folders`), or a swap of ours is under way.
    """
    folder = sample_dir(results_dir, name)
    present = [sub for sub in (ASSEMBLY, TRIMMING) if os.path.lexists(os.path.join(folder, sub))]
    if not present:
        return ""
    record = read_status(results_dir, name)
    if record is not None and record.owns_folders:
        return ""
    if _read_journal(os.path.join(folder, IN_PROGRESS)) is not None:
        return ""
    return present[0]


# --------------------------------------------------------------------------- attempts

@dataclass(frozen=True)
class CommitResult:
    """What :func:`commit_attempt` did.

    ``in_place`` True: ``trimming`` and ``assembly`` in the sample folder now hold the new result
    (call :func:`collect`, then :func:`write_status`). False: the old result could not be replaced
    and is untouched; the new one is in ``folder`` (``<sample>/new_result``). ``reason`` is then
    ``"in_use"`` (a file of the old result is open in another program) or ``"not_ours"`` (the
    sample folder holds an ``assembly`` or ``trimming`` folder this program did not make).
    """
    in_place: bool
    folder: str
    reason: str = ""
    blocked_by: str = ""


def _rename(src: str, dst: str) -> None:
    """A plain rename, retried. Never a copy: half a copy must not be mistaken for a folder."""
    fsutil.retry(lambda: os.rename(src, dst))


def _read_journal(work: str) -> Optional[Dict[str, Any]]:
    """The note a swap leaves in ``_in_progress`` until it is proven or undone; None if no swap.

    Should the note itself be lost while replaced folders are still parked, a cautious stand-in
    is returned: with it an undo puts back what is parked and deletes nothing it cannot prove new.
    """
    data = fsutil.read_json(os.path.join(work, _JOURNAL))
    if isinstance(data, dict) and isinstance(data.get("had"), dict):
        return data
    try:
        with os.scandir(os.path.join(work, _REPLACED)) as parked:
            if any(True for _ in parked):
                return {"had": {ASSEMBLY: True, TRIMMING: True, "status": True, "collected": True},
                        "inferred": True}
    except OSError:
        pass
    return None


def _holds_nothing_but(folder: str, prefix: str) -> bool:
    """True for a real folder that is empty or holds only files named ``prefix...``.

    That is what a crash leaves between the making of a folder and the writing of the note that
    marks it as ours (the note is written under a temporary name first). Nothing is in it yet.
    """
    if os.path.islink(folder):
        return False
    try:
        with os.scandir(folder) as entries:
            return all(entry.name.startswith(prefix) and entry.is_file(follow_symlinks=False)
                       for entry in entries)
    except OSError:
        return False


def _is_our_new_result(folder: str) -> bool:
    """May ``<sample>/new_result`` be replaced or deleted? Only if this program made it (rule R7):
    it carries our note, or it holds nothing yet."""
    try:
        with open(os.path.join(folder, _NEW_RESULT_NOTE), "r", encoding="utf-8") as handle:
            if handle.readline().startswith(branding.APP_NAME):
                return True
    except (OSError, ValueError):
        pass
    return _holds_nothing_but(folder, _NEW_RESULT_NOTE + ".tmp")


def _is_link(path: str) -> bool:
    is_junction = getattr(os.path, "isjunction", None)          # Python 3.12 and later
    return os.path.islink(path) or bool(is_junction and is_junction(path))


def _remove_marked(folder: str, mark: str) -> bool:
    """Delete a folder of ours, its mark last. True if the folder is gone afterwards.

    Deleting gigabytes of reads takes time. If it is cut short (Stop now, then the PC is switched
    off) what is left must still be recognisable as ours, or the sample could never be run in
    this results folder again. So everything else goes first and the note that marks it goes
    last; if something cannot be deleted now, the mark stays for the next try.
    """
    if not os.path.lexists(folder):
        return True
    if _is_link(folder) or not os.path.isdir(folder):
        return fsutil.remove_tree(folder)
    try:
        with os.scandir(folder) as entries:
            others = [entry.path for entry in entries if entry.name != mark]
    except OSError:
        return False
    for path in others:
        if not fsutil.remove_tree(path):
            return False
    return fsutil.remove_tree(folder)


def _put_back_or_drop(live: str, parked: str) -> None:
    """Take a folder of the new attempt out of the sample folder again."""
    if os.path.lexists(parked):
        if not fsutil.remove_tree(live):
            raise PermissionError(13, "could not remove the folder of an unfinished attempt", live)
    else:
        _rename(live, parked)


def _roll_back(results_dir: str, name: str, journal: Dict[str, Any]) -> None:
    """Undo a swap that never got its status file. Safe to run again after being cut short."""
    folder = sample_dir(results_dir, name)
    work = os.path.join(folder, IN_PROGRESS)
    aside = os.path.join(work, _REPLACED)
    had = journal.get("had", {})
    for sub in (ASSEMBLY, TRIMMING):
        live = os.path.join(folder, sub)
        old = os.path.join(aside, sub)
        parked = os.path.join(work, sub)
        if had.get(sub):
            if os.path.lexists(old):
                if os.path.lexists(live):       # the new one had been moved in
                    _put_back_or_drop(live, parked)
                _rename(old, live)
        elif os.path.lexists(live):             # there was none before: this is the new one
            _put_back_or_drop(live, parked)
    old_status = os.path.join(aside, STATUS_FILE)
    target = os.path.join(folder, STATUS_FILE)
    parked_status = had.get("status") and os.path.lexists(old_status)
    # The copy in assemblies/ may already be the new contigs: make it match what is back in place.
    # This comes before the status file, so that a second cut still finds the undo unfinished.
    collected = collected_fasta(results_dir, name)
    earlier = fsutil.read_json(old_status if parked_status else target)
    earlier_done = (isinstance(earlier, dict)
                    and earlier.get("status") in tuple(s.value for s in DONE_STATUSES)
                    and os.path.isfile(contigs_path(results_dir, name)))
    try:
        if earlier_done:
            _copy_contigs(results_dir, name)
        elif not had.get("collected") and os.path.lexists(collected):
            fsutil.retry(lambda: os.remove(collected), attempts=3)
    except OSError as exc:
        log.warning("could not bring %s back in line after undoing a swap: %s", collected, exc)
    if parked_status:
        fsutil.replace_file(old_status, target)             # last: from here it counts again
    note = os.path.join(work, _JOURNAL)
    if os.path.lexists(note):
        fsutil.retry(lambda: os.remove(note), attempts=3)


def _undo_in_flight_commit(results_dir: str, name: str) -> bool:
    """If a swap was begun and never proven by a status file, put the earlier state back."""
    work = in_progress_dir(results_dir, name)
    journal = _read_journal(work)
    if journal is None or os.path.lexists(status_path(results_dir, name)):
        return False
    _roll_back(results_dir, name, journal)
    log.info("%s: an unfinished swap was undone; the earlier state is back", name)
    return True


def _sweep_temporary(folder: str, names: Tuple[str, ...] = (STATUS_FILE,)) -> None:
    """Delete the half-written file a crash or a full disk can leave (``sample_status.json.tmp...``).

    Never the real one: that is only ever replaced by a complete file.
    """
    prefixes = tuple(name + ".tmp" for name in names)
    try:
        with os.scandir(folder) as entries:
            stale = [entry.path for entry in entries
                     if entry.name.startswith(prefixes) and entry.is_file(follow_symlinks=False)]
        for path in stale:
            os.remove(path)
    except OSError:
        pass


def _sweep_half_copies(folder: str) -> None:
    """Delete what a crash in the middle of :func:`collect` left in ``assemblies``
    (``<sample>.fasta.tmp1234``): half a FASTA must not lie about in the folder people hand on."""
    try:
        with os.scandir(folder) as entries:
            stale = [entry.path for entry in entries
                     if _HALF_COPY.search(entry.name) and entry.is_file(follow_symlinks=False)]
        for path in stale:
            os.remove(path)
    except OSError:
        pass


def recover_interrupted(results_dir: str) -> List[str]:
    """Put right every sample whose swap was cut short by a crash or a power cut.

    Call it once at the start of a run, **after taking the lock** and before anything else writes
    (it changes the folder, so it must not run against a folder another run is using). Earlier
    results come back as they were; leftovers of the cut-short attempt are deleted, and so are the
    half-written copies of ``last_run.json``, of the two summaries and of a FASTA in ``assemblies``
    that a crash or a full disk left behind (``<file>.tmp1234``). Returns the names of the
    samples whose earlier state it brought back.
    """
    healed: List[str] = []
    try:
        entries = list(os.scandir(results_dir))
    except OSError:
        return healed
    with _LAST_RUN_LOCK:                 # not under the feet of a save from the other thread
        _sweep_temporary(results_dir, (LAST_RUN, SUMMARY_HTML, SUMMARY_TSV))
    _sweep_half_copies(assemblies_dir(results_dir))
    for entry in entries:
        work = os.path.join(entry.path, IN_PROGRESS)
        try:
            if not entry.is_dir() or _read_journal(work) is None:
                continue
            undone = _undo_in_flight_commit(results_dir, entry.name)
            _remove_marked(work, _ATTEMPT_NOTE)
            _sweep_temporary(entry.path)
            if undone:                   # not for a proven result whose clean-up had to wait
                healed.append(entry.name)
        except OSError as exc:
            log.warning("%s: could not undo an unfinished swap: %s", entry.name, exc)
    healed.sort(key=fsutil.natural_key)
    return healed


def _is_our_attempt(results_dir: str, name: str) -> bool:
    """May ``<sample>/_in_progress`` be deleted? Only if this program made it (rule R7).

    Ours: it carries the note ``begin_attempt`` leaves in it, or the note of a swap, or the sample
    folder carries a status file. A folder with nothing in it (or only that note, half-written)
    is no loss either way.
    """
    work = in_progress_dir(results_dir, name)
    if os.path.isfile(os.path.join(work, _ATTEMPT_NOTE)) or _read_journal(work) is not None:
        return True
    if os.path.isfile(status_path(results_dir, name)):
        return True
    return _holds_nothing_but(work, _ATTEMPT_NOTE + ".tmp")


def begin_attempt(results_dir: str, name: str) -> str:
    """Start an attempt: returns the path of a fresh ``_in_progress`` folder.

    The sample folder is created if needed. A leftover ``_in_progress`` is deleted (rule R3); if
    it holds a swap that was cut short, the earlier result is put back first. The new folder
    holds nothing but a small note (``attempt.json``) that marks it as ours; the caller creates
    the sub-folders it uses (``in_progress_trimming_dir`` and so on).

    Raises ``OSError`` when the leftover cannot be deleted, or when a folder called
    ``_in_progress`` is there that this program did not make (``FileExistsError``).
    """
    folder = fsutil.ensure_dir(sample_dir(results_dir, name))
    work = in_progress_dir(results_dir, name)
    _undo_in_flight_commit(results_dir, name)
    if os.path.lexists(work):
        if not _is_our_attempt(results_dir, name):
            raise FileExistsError(17, "a folder called _in_progress that this program did not make "
                                      "is in the way", work)
        if not _remove_marked(work, _ATTEMPT_NOTE):
            raise PermissionError(13, "the leftover of an earlier attempt could not be deleted", work)
    _sweep_temporary(folder)
    waiting = new_result_dir(results_dir, name)
    if _is_our_new_result(waiting) and not os.path.isfile(os.path.join(waiting, ASSEMBLY, CONTIGS)):
        _remove_marked(waiting, _NEW_RESULT_NOTE)   # a new_result of ours that a crash left unfinished
    fsutil.retry(lambda: os.mkdir(work))
    fsutil.atomic_write_json(os.path.join(work, _ATTEMPT_NOTE), {
        "program": branding.APP_NAME, "program_version": versions.APP_VERSION,
        "sample": name, "started": fsutil.now_iso()})
    return work


def _delete_trimmed_reads(folder: str) -> None:
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        if entry.name.lower().endswith(_FASTQ_ENDINGS) and entry.is_file(follow_symlinks=False):
            try:
                fsutil.retry(lambda path=entry.path: os.remove(path), attempts=3)
            except OSError as exc:
                log.warning("could not delete the trimmed reads %s: %s", entry.path, exc)


def _new_result_note(name: str) -> str:
    return "\n".join([
        "%s: the newest result of sample %s." % (branding.APP_NAME, name),
        "",
        "This sample was assembled again, but the new result could not take the place of the",
        "old one: a file inside the folders \"assembly\" or \"trimming\" beside this folder was",
        "open in another program (for example Excel), or those folders were not made by",
        "%s." % branding.APP_NAME,
        "",
        "The old result was left exactly as it was. The new result is in this folder.",
        "",
        "To put it in place: close the other program, then run the sample again and choose",
        "Overwrite. This folder is removed when that run succeeds.",
        "",
    ])


def _leave_as_new_result(results_dir: str, name: str, reason: str, blocked_by: str) -> CommitResult:
    work = in_progress_dir(results_dir, name)
    waiting = new_result_dir(results_dir, name)
    if os.path.lexists(waiting):
        if not _is_our_new_result(waiting):
            raise FileExistsError(17, "a folder called new_result that this program did not make "
                                      "is in the way", waiting)
        if not _remove_marked(waiting, _NEW_RESULT_NOTE):
            raise PermissionError(13, "an older new_result folder could not be replaced", waiting)
    fsutil.retry(lambda: os.mkdir(waiting))
    fsutil.atomic_write_text(os.path.join(waiting, _NEW_RESULT_NOTE), _new_result_note(name))
    moved: List[Tuple[str, str]] = []
    try:
        for sub in (TRIMMING, ASSEMBLY):
            source = os.path.join(work, sub)
            if os.path.isdir(source):
                _rename(source, os.path.join(waiting, sub))
                moved.append((source, os.path.join(waiting, sub)))
    except OSError:
        # Not even that worked. Leave no half new_result behind: its note would be a lie.
        for source, destination in reversed(moved):
            try:
                _rename(destination, source)
            except OSError:
                pass
        _remove_marked(waiting, _NEW_RESULT_NOTE)
        raise
    _remove_marked(work, _ATTEMPT_NOTE)
    return CommitResult(in_place=False, folder=waiting, reason=reason, blocked_by=blocked_by)


def commit_attempt(results_dir: str, name: str, *, keep_trimmed_reads: bool) -> CommitResult:
    """Move a complete attempt from ``_in_progress`` into ``trimming`` and ``assembly``.

    The old folders are replaced only now, by renames, each retried. They are not deleted yet:
    they wait inside ``_in_progress`` until :func:`write_status` has proven the new result, so
    anything that goes wrong before that (in here, in :func:`collect`, a crash) is undone and the
    earlier result is intact. An untrimmed attempt replaces an old ``trimming`` folder by nothing:
    it would describe reads this assembly was not made from.

    With ``keep_trimmed_reads`` False the trimmed read files are deleted; the trimming reports
    and log are kept.

    If an old folder cannot be moved (a file in it is open in another program: audit row 5.6) the
    old result is left exactly as it was, the new one goes to ``<sample>/new_result`` and the
    result says so instead of raising. Raises :class:`AttemptIncomplete` when the attempt has no
    ``assembly/contigs.fasta``; nothing is touched then.
    """
    folder = sample_dir(results_dir, name)
    work = in_progress_dir(results_dir, name)
    new_assembly = os.path.join(work, ASSEMBLY)
    new_trimming = os.path.join(work, TRIMMING)
    if not os.path.isfile(os.path.join(new_assembly, CONTIGS)):
        raise AttemptIncomplete("no %s in %s" % (CONTIGS, new_assembly))
    _undo_in_flight_commit(results_dir, name)      # a caller that commits twice starts clean

    if not keep_trimmed_reads and os.path.isdir(new_trimming):
        _delete_trimmed_reads(new_trimming)

    target = os.path.join(folder, STATUS_FILE)
    old = {sub: os.path.join(folder, sub) for sub in (ASSEMBLY, TRIMMING)}
    had = {sub: os.path.lexists(path) for sub, path in old.items()}
    had["status"] = os.path.lexists(target)
    had["collected"] = os.path.lexists(collected_fasta(results_dir, name))

    if had[ASSEMBLY] or had[TRIMMING]:
        record = read_status(results_dir, name)
        if record is None or not record.owns_folders:
            # Folders with our names that carry no proof of being ours are never replaced (R7).
            blocked = old[ASSEMBLY] if had[ASSEMBLY] else old[TRIMMING]
            return _leave_as_new_result(results_dir, name, "not_ours", blocked)

    aside = os.path.join(work, _REPLACED)
    journal = {"format": versions.RESULTS_FORMAT, "had": had, "started": fsutil.now_iso()}
    fsutil.atomic_write_json(os.path.join(work, _JOURNAL), journal)
    blocked_by = ""
    try:
        fsutil.ensure_dir(aside)
        if had["status"]:
            blocked_by = target
            fsutil.replace_file(target, os.path.join(aside, STATUS_FILE))   # from here: not assembled
        for sub in (ASSEMBLY, TRIMMING):
            if had[sub]:
                blocked_by = old[sub]
                _rename(old[sub], os.path.join(aside, sub))
        blocked_by = folder
        if os.path.isdir(new_trimming):
            _rename(new_trimming, old[TRIMMING])
        _rename(new_assembly, old[ASSEMBLY])
    except OSError as exc:
        log.warning("%s: the old result could not be replaced (%s); it is kept, and the new one "
                    "goes to %s", name, exc, NEW_RESULT)
        _roll_back(results_dir, name, journal)
        return _leave_as_new_result(results_dir, name, "in_use", blocked_by)
    return CommitResult(in_place=True, folder=folder)


def _kept_log(file_name: str) -> bool:
    return file_name.lower().endswith(".log") or file_name in (PREFLIGHT, SUMMARY_TXT)


def _clear_kept_logs(destination: str) -> None:
    """Empty ``last_failed_attempt`` of what this program put there: the logs of the failure before.

    Only files with the names we keep are deleted, and the folders this leaves empty (rule R7).
    Whatever else someone put there stays. Normally nothing else is there and the folder goes.
    """
    if not os.path.lexists(destination):
        return
    if _is_link(destination) or not os.path.isdir(destination):
        log.warning("%s is not a folder of this program and is left alone", destination)
        return
    for root, _dirs, files in os.walk(destination, topdown=False):
        for path in [os.path.join(root, file_name) for file_name in files if _kept_log(file_name)]:
            try:
                fsutil.retry(lambda path=path: os.remove(path), attempts=3)
            except OSError as exc:
                log.warning("could not replace the log %s: %s", path, exc)
        try:
            os.rmdir(root)                   # only works when nothing is left in it
        except OSError:
            pass


def _keep_logs(work: str, destination: str) -> List[str]:
    wanted: List[Tuple[str, str]] = []
    for root, dirs, files in os.walk(work):
        if root == work:
            dirs[:] = [d for d in dirs if d not in (_REPLACED, JOINED)]
        for file_name in sorted(files):
            if _kept_log(file_name):
                source = os.path.join(root, file_name)
                wanted.append((source, os.path.join(destination, os.path.relpath(source, work))))
    _clear_kept_logs(destination)
    kept: List[str] = []
    for source, target in wanted:
        try:
            fsutil.ensure_dir(os.path.dirname(target))
            fsutil.retry(lambda s=source, t=target: shutil.copyfile(s, t), attempts=3)
            kept.append(target)
        except OSError as exc:
            log.warning("could not keep the log %s: %s", source, exc)
    return kept


def abandon_attempt(results_dir: str, name: str, keep_logs: bool) -> List[str]:
    """Give up the attempt in ``_in_progress`` and delete that folder.

    A swap that was begun and not proven (``commit_attempt`` without a finished
    ``write_status``) is undone first, so the earlier result is back in place. With ``keep_logs``
    true the attempt's ``*.log`` files, ``preflight.txt`` and ``SUMMARY.txt`` are first copied to
    ``<sample>/last_failed_attempt`` under the same relative names (``assembly/assembly.log``,
    ``trimming/trimming.log``...), replacing the logs that folder held (a file there that is not
    one of our logs is left alone). Returns the paths of the copies. Nothing else is deleted
    (rule R7), and this function does not raise for a file it cannot copy or delete: it is the
    clean-up after a failure and must not hide that failure.
    """
    work = in_progress_dir(results_dir, name)
    kept: List[str] = []
    if not os.path.lexists(work):
        return kept
    if not _is_our_attempt(results_dir, name):
        log.warning("%s: %s was not made by this program and is left alone", name, work)
        return kept
    try:
        _undo_in_flight_commit(results_dir, name)
    except OSError as exc:
        log.warning("%s: could not undo an unfinished swap: %s", name, exc)
        return kept                      # keep _in_progress: it still holds the earlier result
    if keep_logs and os.path.isdir(work):
        try:
            kept = _keep_logs(work, last_failed_dir(results_dir, name))
        except OSError as exc:
            log.warning("%s: could not keep the logs of the failed attempt: %s", name, exc)
    if not _remove_marked(work, _ATTEMPT_NOTE):
        log.warning("%s: %s could not be deleted now; the next attempt deletes it", name, work)
    _sweep_temporary(sample_dir(results_dir, name))
    return kept


def _assembly_in_place_is_ours(results_dir: str, name: str) -> bool:
    """Is ``<sample>/assembly`` a result of this program: one being swapped in right now (between
    ``commit_attempt`` and ``write_status``), or one its status file calls done?"""
    target = status_path(results_dir, name)
    if not os.path.lexists(target):
        return _read_journal(in_progress_dir(results_dir, name)) is not None
    stored = fsutil.read_json(target)
    return isinstance(stored, dict) and stored.get("status") in tuple(s.value for s in DONE_STATUSES)


def kept_log_path(results_dir: str, name: str, path: str) -> str:
    """Where ``abandon_attempt(keep_logs=True)`` put a log of the attempt it gave up.

    ``path`` is the log as the attempt knew it, inside ``_in_progress`` (for example
    ``<sample>/_in_progress/assembly/assembly.log``). Returns the path of its copy under
    ``<sample>/last_failed_attempt``, or "" when ``path`` is not inside ``_in_progress`` or no
    copy is there. Use it to point ``SampleOutcome.log`` at a file that still exists.
    """
    parts = _inside(sample_dir(results_dir, name), path)
    if not parts or parts[0] != IN_PROGRESS or len(parts) < 2:
        return ""
    kept = os.path.join(last_failed_dir(results_dir, name), *parts[1:])
    return kept if os.path.isfile(kept) else ""


def collect(results_dir: str, name: str) -> str:
    """Copy ``<sample>/assembly/contigs.fasta`` to ``assemblies/<sample>.fasta``, atomically.

    The copy is made under a temporary name and renamed over the old file, so ``assemblies``
    never holds half a file. Returns the path of the copy.

    Call it after a :func:`commit_attempt` that said ``in_place``. Raises ``OSError`` when there
    is no ``contigs.fasta``, and :class:`AttemptIncomplete` when the one in place is not a result
    of this program (after a commit that said ``not_ours``, or with no commit at all): a file
    somebody else made is never passed off as this sample's assembly.
    """
    if os.path.isfile(contigs_path(results_dir, name)) and not _assembly_in_place_is_ours(results_dir, name):
        raise AttemptIncomplete("%s was not made by this program; nothing was copied to %s"
                                % (contigs_path(results_dir, name), ASSEMBLIES))
    return _copy_contigs(results_dir, name)


def _copy_contigs(results_dir: str, name: str) -> str:
    source = contigs_path(results_dir, name)
    target = collected_fasta(results_dir, name)
    folder = fsutil.ensure_dir(assemblies_dir(results_dir))
    stale = os.path.basename(target) + ".tmp"
    try:
        for entry in os.scandir(folder):
            if entry.name.startswith(stale) and entry.is_file():
                os.remove(entry.path)
    except OSError:
        pass
    temporary = "%s.tmp%d" % (target, os.getpid())
    try:
        with open(source, "rb") as reader, open(temporary, "wb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            try:
                os.fsync(writer.fileno())
            except OSError:
                pass
        fsutil.replace_file(temporary, target)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return target


def count_assemblies(results_dir: str) -> int:
    """How many ``.fasta`` files the ``assemblies`` folder holds."""
    try:
        return sum(1 for entry in os.scandir(assemblies_dir(results_dir))
                   if entry.name.lower().endswith(".fasta") and entry.is_file())
    except OSError:
        return 0


# --------------------------------------------------------------------------- last_run.json

@dataclass
class LastRun:
    """The memory of a batch (decision D10), as read back from ``last_run.json``."""
    config: RunConfig
    states: Dict[str, RunStatus] = field(default_factory=dict)
    notes: Dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    saved_at: str = ""                       # when the file was last rewritten
    ended_at: Optional[str] = None           # None: the run never recorded its end
    end_reason: Optional[EndReason] = None
    interrupted: Optional[str] = None        # the cause, when Windows gave notice (e.g. "shutdown")
    program_version: str = ""

    def status_of(self, name: str) -> RunStatus:
        return self.states.get(name, RunStatus.WAITING)


StateValue = Union[RunStatus, str, Tuple[Any, str], Mapping[str, Any]]


def _state_entry(value: StateValue) -> Dict[str, str]:
    note = ""
    status: Any = value
    if isinstance(value, Mapping):
        status, note = value.get("status", RunStatus.WAITING), value.get("note", "")
    elif isinstance(value, (tuple, list)):
        status = value[0] if value else RunStatus.WAITING
        note = value[1] if len(value) > 1 else ""
    if isinstance(status, RunStatus):
        status = status.value
    return {"status": str(status), "note": str(note or "")}


def save_last_run(results_dir: str, config: RunConfig, states: Mapping[str, StateValue], *,
                  started_at: str, ended_at: Optional[str] = None,
                  end_reason: Union[EndReason, str, None] = None,
                  interrupted: Optional[str] = None) -> None:
    """Rewrite ``last_run.json`` atomically. Call it at every change of state.

    ``states`` maps a sample name to ``(RunStatus, note)`` (a bare status, or a dict with
    ``status`` and ``note``, is accepted too). ``ended_at`` stays None for as long as the run is
    going: a file that never got its end is how a crash or a power cut is recognised.
    ``interrupted`` names the cause when Windows gave notice of a shutdown. Safe to call from
    two threads at once.
    """
    if isinstance(end_reason, EndReason):
        end_reason = end_reason.value
    with _LAST_RUN_LOCK:            # two threads may save at once; see _LAST_RUN_LOCK
        fsutil.atomic_write_json(last_run_path(results_dir), {
            "format": versions.RESULTS_FORMAT,
            "program": branding.APP_NAME,
            "program_version": versions.APP_VERSION,
            "tesseract_version": versions.TESSERACT_VERSION,
            "scepter_version": versions.SCEPTER_VERSION,
            "config": config.to_dict(),
            "states": {name: _state_entry(value) for name, value in states.items()},
            "started_at": started_at,
            "saved_at": fsutil.now_iso(),
            "ended_at": ended_at,
            "end_reason": end_reason,
            "interrupted": interrupted,
        })


def load_last_run(results_dir: str) -> Optional[LastRun]:
    """The remembered batch, or None when there is none or the file is damaged.

    ``config.results_dir`` is the folder the file was read from, not the one written in it: a
    results folder that was moved (to a stick, to another PC) is continued where it is now.
    Raises :class:`NewerFormat` for a file written by a newer program.
    """
    path = last_run_path(results_dir)
    data = fsutil.read_json(path)
    if not isinstance(data, dict):
        return None
    found = _int_or_none(data.get("format"))
    if found is None:
        return None
    if found > versions.RESULTS_FORMAT:
        raise NewerFormat(path, found)
    try:
        config = RunConfig.from_dict(data["config"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    config.results_dir = results_dir
    states: Dict[str, RunStatus] = {}
    notes: Dict[str, str] = {}
    raw_states = data.get("states")
    for name, entry in (raw_states.items() if isinstance(raw_states, dict) else []):
        entry = entry if isinstance(entry, dict) else {"status": entry}
        try:
            states[name] = RunStatus(entry.get("status"))
        except ValueError:
            states[name] = RunStatus.WAITING       # unknown: treat as not done yet
        note = entry.get("note", "")
        notes[name] = note if isinstance(note, str) else ""
    try:
        end_reason: Optional[EndReason] = EndReason(data.get("end_reason"))
    except ValueError:
        end_reason = None

    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    return LastRun(config=config, states=states, notes=notes, started_at=text("started_at"),
                   saved_at=text("saved_at"), ended_at=text("ended_at") or None,
                   end_reason=end_reason, interrupted=text("interrupted") or None,
                   program_version=text("program_version"))


def pending_samples(last_run: LastRun) -> List[Sample]:
    """The samples a Continue should run, in their order.

    Those that were stopped or not started, those still waiting, and those that were in the
    middle of a stage when the program died. Finished, failed and skipped samples are not here.
    """
    pending: List[Sample] = []
    for sample in last_run.config.samples:
        status = last_run.status_of(sample.name)
        if status.is_running or status in (RunStatus.WAITING, RunStatus.STOPPED, RunStatus.NOT_STARTED):
            pending.append(sample)
    return pending


def failed_samples(last_run: LastRun) -> List[Sample]:
    """The samples "Try the failed samples again" should run, in their order."""
    return [s for s in last_run.config.samples if last_run.status_of(s.name) is RunStatus.FAILED]


def was_interrupted(last_run: Optional[LastRun]) -> bool:
    """True for a run that never recorded its end: a crash, a power cut, a Windows restart."""
    return last_run is not None and not last_run.ended_at


# --------------------------------------------------------------------------- the folder as a whole

def _generated_by_us(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return ('name="generator" content="%s' % branding.APP_NAME) in head


def _subfolders(path: str) -> List[str]:
    out: List[str] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        out.append(entry.path)
                except OSError:
                    continue
                if len(out) >= _SCAN_LIMIT:
                    break
    except OSError:
        pass
    return out


def is_results_folder(path: str) -> bool:
    """Does this folder hold our files: a lock file, ``last_run.json``, our summary page, or a
    sample folder with a status file or an attempt in progress?

    A folder made by a newer version of the program is one of ours too; this never raises.
    """
    if not path or not os.path.isdir(path):
        return False
    if os.path.isfile(lock_path(path)):
        return True
    remembered = fsutil.read_json(last_run_path(path))
    if isinstance(remembered, dict) and ("config" in remembered
                                         or remembered.get("program") == branding.APP_NAME):
        return True
    if _generated_by_us(summary_html_path(path)):
        return True
    for folder in _subfolders(path):
        status = fsutil.read_json(os.path.join(folder, STATUS_FILE))
        if isinstance(status, dict) and "format" in status:
            return True
        if os.path.isfile(os.path.join(folder, IN_PROGRESS, _ATTEMPT_NOTE)):
            return True
    return False


def foreign_and_not_empty(path: str) -> bool:
    """Audit row 4.2: the folder exists, holds something, and is not one of our results folders.

    The caller then appends ``branding.RESULTS_DIR_NAME`` so that the results get a folder of
    their own. A folder that does not exist yet, or is empty, is used as it is.
    """
    if not path or not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as entries:
            holds_something = any(entry.name.casefold() not in _IGNORED_IN_EMPTY_FOLDER
                                  for entry in entries)
    except OSError:
        return False
    return holds_something and not is_results_folder(path)


def made_by_newer_version(results_dir: str) -> bool:
    """Audit row 4.14: something in the folder was written by a newer version of the program."""
    try:
        load_last_run(results_dir)
        all_records(results_dir)
    except NewerFormat:
        return True
    return False
