# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Find read files, pair them, group lanes, name the samples and take a quick look inside.

Everything here works on paths and returns ``model.Sample`` objects. No Tk, no engines.
The sentences in ``Sample.note``, ``Ignored.reason`` and ``PeekResult.problem`` are
written for the user: the window shows them as they are.

How a row gets its state (first match wins):

1. what the file names say (:func:`scan`): no partner, a lane with one file, the same file
   twice, compressed and plain files mixed, "Undetermined" reads -> Left out;
2. what the first kilobytes say (:func:`check_sample`, which reads files and so belongs in
   a worker thread): missing, online only, empty, not FASTQ, ... -> Left out;
3. the names in the list (:func:`mark_name_clashes`): two samples that would run under one
   name -> both Left out, until one is renamed;
4. otherwise Ready, with a note when there is something harmless worth knowing.

A file that is missing its partner is kept as a lane with an empty string in the other
place, for example ``("D:/run/KP_1_R1.fastq.gz", "")``.
"""

from __future__ import annotations

import enum
import os
import re
import stat
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple, Union

from . import branding, fsutil
from .model import Sample, SampleState
from .settings_map import TRIMMING_REPORT_HTML, trimmed_names

__all__ = [
    "FASTQ_EXTENSIONS", "PEEK_BYTES", "MAX_RESULT_PATH", "DISK_MARGIN_BYTES", "RESULT_BYTES_PER_SAMPLE",
    "Ignored", "ScanResult", "PeekStatus", "PeekResult", "MergeResult",
    "is_fastq_name", "scan", "peek", "assess_sample", "check_sample",
    "is_undetermined", "has_no_partner", "is_name_clash",
    "merge", "mark_name_clashes", "make_names_different", "name_problem", "rename",
    "sample_disk_need", "estimate_disk_need", "longest_result_path",
    # sentences and limits, for anyone who needs to recognise or reuse them
    "NO_READ1", "NO_READ2", "NO_PARTNER", "NO_FILES", "SAME_FILE", "PAIR_COMPRESSION",
    "LANES_COMPRESSION", "UNDETERMINED", "NAMES_DIFFER", "ALREADY_TRIMMED", "TINY", "HUGE", "BROKEN_NAME",
    "LOPSIDED", "TINY_PAIR_BYTES", "HUGE_PAIR_BYTES",
]

FASTQ_EXTENSIONS = (".fastq.gz", ".fq.gz", ".fastq", ".fq")

# Names of decision D1 that this module has to recognise or measure.
_STATUS_FILE = "sample_status.json"
_RESULTS_MARKERS = (_STATUS_FILE, "last_run.json", "tesseract-win.lock")
_OWN_WORK_FOLDERS = ("_in_progress", "last_failed_attempt", "new_result")

_EXT_RE = re.compile(r"\.(?:fastq|fq)(?:\.gz)?\Z", re.IGNORECASE)
# <name>_S<n>_L<lane>_R1_001, the form bcl2fastq and the instruments write. "_S<n>" and
# "_L<lane>" may each be absent (--no-lane-splitting; older CASAVA names). "I" marks index reads.
# (re.DOTALL and \Z: a name may hold any character, a line end too where the system allows it.)
_ILLUMINA_RE = re.compile(
    r"^(?P<name>.+?)(?:_S(?P<s>\d+))?(?:_L(?P<lane>\d{3}))?_(?P<kind>[RI])(?P<read>[12])_(?P<chunk>\d{3})\Z",
    re.DOTALL)
_LOOSE_INDEX_RE = re.compile(r"^.+_I[12]\Z", re.DOTALL)
# Read markers, matched at the end of the name only, most specific first.
_LOOSE_FORMS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("_R", re.compile(r"^(?P<name>.+)_[Rr](?P<read>[12])\Z", re.DOTALL)),
    (".R", re.compile(r"^(?P<name>.+)\.[Rr](?P<read>[12])\Z", re.DOTALL)),
    ("_", re.compile(r"^(?P<name>.+)_(?P<read>[12])\Z", re.DOTALL)),
    (".", re.compile(r"^(?P<name>.+)\.(?P<read>[12])\Z", re.DOTALL)),
)
# The endings settings_map.trimmed_names() gives this program's own trimmed reads.
_OWN_TRIMMED_RE = re.compile(r"_trimmed_R[12]\Z")

_ARCHIVE_RE = re.compile(r"\.(?:zip|7z|rar|tar|tgz|tbz2?|txz|tar\.(?:gz|bz2|xz|zst))$", re.IGNORECASE)
_OTHER_COMPRESSION_RE = re.compile(r"\.(?:fastq|fq)\.(?:bz2|xz|zst|lz4|lzma|z|ora)$|\.ora$", re.IGNORECASE)
_PARTIAL_DOWNLOAD_RE = re.compile(r"\.(?:crdownload|part|partial|download|tmp)$", re.IGNORECASE)


# --------------------------------------------------------------------------- sentences

NO_READ2 = ("No Read 2 file found. Each sample needs two files in the same folder, "
            "for example X_R1.fastq.gz and X_R2.fastq.gz.")
NO_READ1 = ("No Read 1 file found. Each sample needs two files in the same folder, "
            "for example X_R1.fastq.gz and X_R2.fastq.gz.")
NO_PARTNER = ("This file has no partner. Each sample needs two files in the same folder, "
              "for example X_R1.fastq.gz and X_R2.fastq.gz.")
SAME_FILE = "Read 1 and Read 2 are the same file. A sample needs two different files."
PAIR_COMPRESSION = ("One of the two files is compressed (.gz) and the other is not. Compress or "
                    "decompress one of them so that both are the same, then add them again.")
LANES_COMPRESSION = ("Some files of this sample are compressed (.gz) and some are not, so its lanes "
                     "cannot be joined. Make all of them the same, then add them again.")
UNDETERMINED = ("These are reads the sequencer could not assign to any sample. "
                "They are not an isolate, so they are left out.")
NAMES_DIFFER = ("These two files do not belong together: their read names differ. "
                "Check that they are Read 1 and Read 2 of the same sample.")
ALREADY_TRIMMED = "Already trimmed by %s, so Trim is not ticked." % branding.APP_NAME
TINY = "Very little data. The assembly will probably be poor or fail."
HUGE = "Very large for a bacterial isolate. It will be slow and may need more memory than this PC has."
LOPSIDED = "Read 1 and Read 2 differ a lot in size. One of them may be incomplete."
NO_FILES = "This sample has no read files."
BROKEN_NAME = ("The name of this file or of its folder contains a damaged character that programs "
               "cannot pass on. Rename it using plain letters, then add it again.")

_NAME_CHANGED = "Name changed from '%s' to '%s' so that Windows and other programs accept it."
_NAME_CHANGED_RE = re.compile(
    r"Name changed from '.*?' to '.*?' so that Windows and other programs accept it\. ?")
_CLASH_RE = re.compile(r"^(?:Two|\d+) samples (?:are called|have the same name) ")
_RESERVED_NAME = ("The name %s is used by the program for its own files in the results folder. "
                  "Rename this sample (double-click the name).")
_RESERVED_RE = re.compile(r"^The name .+ is used by the program for its own files ")
#: Names of decision D1 that stand directly in the results folder; a sample cannot have them.
_RESERVED_NAMES = frozenset(("assemblies", "run_summary.html", "run_summary.tsv", "run_log.txt",
                             "last_run.json", "tesseract-win.lock"))

#: A pair smaller than this (compressed; four times as much for plain files) gets the note TINY.
TINY_PAIR_BYTES = 20 * 1024 ** 2
#: A pair larger than this (compressed; four times as much for plain files) gets the note HUGE.
HUGE_PAIR_BYTES = 4 * 1024 ** 3
_PLAIN_FACTOR = 4
_LOPSIDED_RATIO = 1.5
_LOPSIDED_MIN_BYTES = 1024 ** 2

# Where mark_name_clashes() keeps the (state, note) a sample had before it was marked.
# An attribute outside the dataclass fields: it is not saved, compared or shown.
_BEFORE_CLASH = "_before_name_clash"


# --------------------------------------------------------------------------- file names

class _Name(NamedTuple):
    """What a FASTQ file name says."""
    ext: str                 # as spelled, for example ".FASTQ.gz"
    gz: bool
    sample: str              # the sample name before cleaning
    read: int                # 1 or 2; 0 when the name has no read marker
    form: str                # "illumina", "_R", ".R", "_", "." or ""
    snum: str                # the digits of _S<n>, or ""
    lane: Optional[int]
    chunk: Optional[int]
    index: bool              # an index read (_I1_, _I2_)
    own_trimmed: bool        # ends like this program's own trimmed reads


def is_fastq_name(filename: str) -> bool:
    """True for names ending in .fastq, .fq, .fastq.gz or .fq.gz, in any letter case."""
    return _parse_name(os.path.basename(filename)) is not None


def _parse_name(filename: str) -> Optional[_Name]:
    found = _EXT_RE.search(filename)
    if not found or found.start() == 0:
        return None
    stem, ext = filename[:found.start()], filename[found.start():]
    gz = ext.lower().endswith(".gz")
    own = bool(_OWN_TRIMMED_RE.search(stem))
    m = _ILLUMINA_RE.match(stem)
    if m:
        lane = m.group("lane")
        return _Name(ext, gz, m.group("name"), int(m.group("read")), "illumina", m.group("s") or "",
                     int(lane) if lane else None, int(m.group("chunk")), m.group("kind") == "I", own)
    if _LOOSE_INDEX_RE.match(stem):
        return _Name(ext, gz, stem, 0, "", "", None, None, True, own)
    for form, pattern in _LOOSE_FORMS:
        m = pattern.match(stem)
        if m:
            return _Name(ext, gz, m.group("name"), int(m.group("read")), form, "", None, None, False, own)
    return _Name(ext, gz, stem, 0, "", "", None, None, False, own)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


_DIGIT_RUNS = re.compile(r"(\d+)")


def _natural(text: str) -> List[Any]:
    """``fsutil.natural_key`` for any text at all.

    That function raises ``ValueError`` on a character such as "²" or "①" standing alone
    between two numbers ("KP1²3"): ``str.isdigit`` says yes to it and ``int`` says no. A
    file name must never stop a search, so the same key is built here when that happens.
    """
    try:
        return fsutil.natural_key(text)
    except ValueError:
        return [int(part) if part.isdecimal() else part.casefold() for part in _DIGIT_RUNS.split(text)]


def _is_gz(path: str) -> bool:
    return path.lower().endswith(".gz")


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if _norm(a) == _norm(b):
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _files(sample: Sample) -> List[str]:
    return [p for pair in sample.lanes for p in pair if p]


def _writable_text(path: str) -> bool:
    """False for a path with half a character in it (an unpaired UTF-16 unit on Windows, a
    byte that is not UTF-8 elsewhere). Such a path cannot be saved in ``last_run.json`` or
    a status file, which are UTF-8, and does not reach the engines unchanged."""
    try:
        path.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _undetermined_name(filename: str) -> bool:
    """Undetermined_S0_L001_R1_001.fastq.gz and the like: the reads no sample claimed."""
    name = _parse_name(filename)
    return name is not None and name.sample.casefold() == "undetermined"


# --------------------------------------------------------------------------- scan results

@dataclass
class Ignored:
    """A file or folder that did not become a row, and why.

    ``kind``: not_fastq, archive, compression, partial_download, index, own_trimmed,
    results_folder, missing, unreadable.
    """
    path: str
    reason: str
    kind: str = "not_fastq"


@dataclass
class ScanResult:
    """What :func:`scan` found. ``samples`` and ``ignored`` are in natural order."""
    samples: List[Sample] = field(default_factory=list)
    ignored: List[Ignored] = field(default_factory=list)
    fastq_files: int = 0          # FASTQ files seen, whatever became of them
    other_files: int = 0          # other files met inside searched folders (counted, not listed)
    stopped: bool = False         # the search was stopped before it was complete

    @property
    def ready(self) -> List[Sample]:
        return [s for s in self.samples if s.ready]

    @property
    def left_out(self) -> List[Sample]:
        return [s for s in self.samples if not s.ready]

    @property
    def none_paired(self) -> bool:
        """True when files were found and not one of them has a partner (a single-end run)."""
        return bool(self.samples) and all(has_no_partner(s) for s in self.samples)

    def summary(self) -> str:
        """One line for under the table, e.g. "Found 24 samples (48 files). Not added: ..."."""
        ready = self.ready
        files = sum(len(_files(s)) for s in ready)
        if ready:
            head = "Found %s (%s)." % (_plural(len(ready), "sample"), _plural(files, "file"))
        else:
            head = "No complete pair of FASTQ files was found."
        parts: List[str] = []
        other = self.other_files + sum(1 for i in self.ignored if i.kind in _OTHER_FILE_KINDS)
        if other:
            parts.append(_plural(other, "other file"))
        undetermined = sum(len(_files(s)) for s in self.samples if is_undetermined(s))
        if undetermined:
            parts.append(_plural(undetermined, "Undetermined file"))
        alone = sum(len(_files(s)) for s in self.samples
                    if has_no_partner(s) and not is_undetermined(s))
        if alone:
            parts.append("%s with no partner" % _plural(alone, "file"))
        rest = sum(1 for s in self.left_out if not is_undetermined(s) and not has_no_partner(s))
        if rest:
            parts.append("%s with a problem" % _plural(rest, "sample"))
        skipped = sum(1 for i in self.ignored if i.kind == "results_folder")
        if skipped:
            parts.append(_plural(skipped, "results folder"))
        if parts:
            head += " Not added: %s." % ", ".join(parts)
        if self.stopped:
            head += " The search was stopped early."
        return head


_OTHER_FILE_KINDS = ("not_fastq", "archive", "compression", "partial_download", "index")


def _plural(number: int, noun: str) -> str:
    return "%d %s%s" % (number, noun, "" if number == 1 else "s")


class _File(NamedTuple):
    path: str
    folder: str
    name: _Name
    explicit: bool


def _why_not_fastq(filename: str) -> Ignored:
    """The reason for a file the user chose on purpose that cannot be used."""
    if _PARTIAL_DOWNLOAD_RE.search(filename):
        return Ignored("", "%s is still being downloaded or copied. Add it when that has finished."
                       % filename, "partial_download")
    if _OTHER_COMPRESSION_RE.search(filename):
        return Ignored("", "%s: this kind of compression is not supported. Convert the file to "
                       ".fastq.gz first." % filename, "compression")
    if _ARCHIVE_RE.search(filename):
        return Ignored("", "%s is an archive. Extract it first, then add the FASTQ files that are "
                       "inside." % filename, "archive")
    return Ignored("", "%s is not a FASTQ file. FASTQ files end in .fastq, .fq, .fastq.gz or .fq.gz."
                   % filename, "not_fastq")


def _hidden(entry: "os.DirEntry[str]") -> bool:
    """Hidden and system folders, and the work folders of this program."""
    name = entry.name
    if name.startswith(".") or name.startswith("$") or name in _OWN_WORK_FOLDERS:
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x6)          # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


# Windows reparse tags of the two kinds of folder that only point somewhere else. Other
# reparse points are real folders (a OneDrive folder is one) and are searched.
_LINK_TAGS = (0xA0000003, 0xA000000C)      # IO_REPARSE_TAG_MOUNT_POINT (junction), _SYMLINK


def _real_folder(entry: "os.DirEntry[str]") -> bool:
    """A folder that is not a link: links and junctions are never followed."""
    try:
        if not entry.is_dir(follow_symlinks=False):
            return False
        is_junction = getattr(entry, "is_junction", None)          # Python 3.12 and later
        if is_junction is not None and is_junction():
            return False
        return getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0) not in _LINK_TAGS
    except OSError:
        return False


def _is_results_folder(folder: str, entries: Sequence["os.DirEntry[str]"]) -> bool:
    """A results folder of this program, or one sample's folder inside it."""
    if os.path.basename(folder.rstrip("\\/")).casefold() == branding.RESULTS_DIR_NAME.casefold():
        return True
    names = {e.name.casefold() for e in entries}
    if any(marker in names for marker in _RESULTS_MARKERS):
        return True
    for entry in entries:
        if _real_folder(entry) and os.path.isfile(os.path.join(entry.path, _STATUS_FILE)):
            return True
    return False


def _walk(root: str, files: Dict[str, _File], own_trimmed: Dict[str, Ignored], result: ScanResult,
          progress: Optional[Callable[[int], Any]], should_stop: Optional[Callable[[], bool]]) -> None:
    """Search one folder tree. Own trimmed reads go to ``own_trimmed``, not to ``files``."""
    stack = [root]
    while stack:
        if should_stop is not None and should_stop():
            result.stopped = True
            return
        folder = stack.pop()
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda e: e.name)
        except OSError:
            result.ignored.append(Ignored(folder, "The folder %s could not be opened."
                                          % (os.path.basename(folder) or folder), "unreadable"))
            continue
        if _is_results_folder(folder, entries):
            result.ignored.append(Ignored(
                folder, "%s is a results folder of %s, so it was not searched. To assemble reads "
                "that are inside it, add them with Add files."
                % (os.path.basename(folder.rstrip("\\/")) or folder, branding.APP_NAME),
                "results_folder"))
            continue
        for entry in entries:
            try:
                if _real_folder(entry):
                    if not _hidden(entry):
                        stack.append(entry.path)
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if entry.name.startswith("."):
                # Not data: the "._KP_R1.fastq.gz" files a Mac leaves beside every file it
                # copies to a stick or a share, and other hidden files. Taken as reads they
                # would put a second, unreadable KP in the list.
                result.other_files += 1
                continue
            name = _parse_name(entry.name)
            if name is None:
                if _OTHER_COMPRESSION_RE.search(entry.name):
                    reason = _why_not_fastq(entry.name)
                    result.ignored.append(Ignored(entry.path, reason.reason, reason.kind))
                else:
                    result.other_files += 1
                continue
            if name.own_trimmed:
                own_trimmed.setdefault(_norm(entry.path), Ignored(
                    entry.path, "%s was trimmed by %s and is left where it is. To assemble it, add "
                    "it with Add files." % (entry.name, branding.APP_NAME), "own_trimmed"))
                continue
            files.setdefault(_norm(entry.path), _File(entry.path, folder, name, False))
        if progress is not None:
            progress(len(files) + len(own_trimmed))


def _slot_key(item: _File) -> Tuple[Any, ...]:
    n = item.name
    return (_norm(item.folder), n.form, n.sample, n.snum, n.lane, n.chunk)


def _pull_partners(files: Dict[str, _File]) -> None:
    """A file chosen by itself brings its partner along, if that is in the same folder."""
    have = {(_slot_key(f), f.name.read) for f in files.values()}
    listings: Dict[str, List[_File]] = {}
    for item in list(files.values()):
        if not item.explicit or item.name.read == 0 or item.name.index:
            continue
        other = 3 - item.name.read
        key = _slot_key(item)
        if (key, other) in have:
            continue
        folder_key = _norm(item.folder)
        if folder_key not in listings:
            found: List[_File] = []
            try:
                with os.scandir(item.folder) as listing:
                    for entry in listing:
                        name = _parse_name(entry.name)
                        try:
                            if name is not None and entry.is_file():
                                found.append(_File(entry.path, item.folder, name, True))
                        except OSError:
                            continue
            except OSError:
                pass
            listings[folder_key] = found
        candidates = [c for c in listings[folder_key]
                      if _slot_key(c) == key and c.name.read == other and not c.name.index]
        if not candidates:
            continue
        candidates.sort(key=lambda c: (c.name.ext.casefold() != item.name.ext.casefold(),
                                       c.name.gz != item.name.gz, c.path))
        partner = candidates[0]
        files.setdefault(_norm(partner.path), partner)
        have.add((key, other))


def _pair_up(first: List[_File], second: List[_File]) -> List[Tuple[Optional[_File], Optional[_File]]]:
    """Pair the Read 1 and Read 2 candidates of one place: same spelling first, then same kind."""
    first = sorted(first, key=lambda f: f.path)
    second = sorted(second, key=lambda f: f.path)
    pairs: List[Tuple[Optional[_File], Optional[_File]]] = []
    rules: Tuple[Callable[[_File, _File], bool], ...] = (
        lambda a, b: a.name.ext.casefold() == b.name.ext.casefold(),
        lambda a, b: a.name.gz == b.name.gz,
        lambda a, b: True,
    )
    for rule in rules:
        for a in list(first):
            for b in second:
                if rule(a, b):
                    pairs.append((a, b))
                    first.remove(a)
                    second.remove(b)
                    break
    pairs += [(a, None) for a in first]
    pairs += [(None, b) for b in second]
    return pairs


_Placed = Tuple[Tuple[int, int], Optional[_File], Optional[_File]]


def _split_copies(placed: List[_Placed]) -> List[List[_Placed]]:
    """One sample, unless two pairs claim the same lane (a compressed and a plain copy)."""
    places = [p[0] for p in placed]
    if len(set(places)) == len(places):
        return [placed]
    by_kind: Dict[Tuple[str, str], List[_Placed]] = {}
    for item in placed:
        kind = tuple((f.name.ext.casefold() if f else "") for f in item[1:])
        by_kind.setdefault(kind, []).append(item)        # type: ignore[arg-type]
    groups: List[List[_Placed]] = []
    for group in by_kind.values():
        places = [p[0] for p in group]
        if len(set(places)) == len(places):
            groups.append(group)
        else:
            groups.extend([item] for item in group)
    return groups


def _make_sample(raw_name: str, placed: List[_Placed]) -> Sample:
    placed = sorted(placed, key=lambda p: (p[0], (p[1] or p[2]).path))     # type: ignore[union-attr]
    lanes = [(a.path if a else "", b.path if b else "") for _, a, b in placed]
    members = [f for _, a, b in placed for f in (a, b) if f]
    sample = Sample(name=fsutil.safe_name(raw_name), lanes=lanes,
                    trim=not all(f.name.own_trimmed for f in members),
                    size_bytes=sum(_size(f.path) for f in members))
    problem = _static_problem(sample)
    if not problem and any(_same_file(a, b) for a, b in lanes):
        problem = SAME_FILE
    if problem:
        sample.state, sample.note = SampleState.LEFT_OUT, problem
    else:
        note = _static_note(sample)
        sample.state, sample.note = (SampleState.READY_NOTE, note) if note else (SampleState.READY, "")
    return sample


def scan(paths: Union[str, "os.PathLike[str]", Iterable[Union[str, "os.PathLike[str]"]]], *,
         progress: Optional[Callable[[int], Any]] = None,
         should_stop: Optional[Callable[[], bool]] = None) -> ScanResult:
    """Turn files and folders into samples. Reads names and sizes only, never file content.

    ``paths`` are files (Add files, a drop) and folders (Add folder). Folders are searched
    through all their sub-folders, except: hidden and system folders, links to folders, any
    results folder of this program (one named ``branding.RESULTS_DIR_NAME``, or holding
    ``last_run.json``, or whose sub-folders hold ``sample_status.json``) and any sample
    folder inside one. This program's own trimmed reads (``..._trimmed_R1`` / ``_R2``) are
    taken only when they were given as files; such a sample has ``trim`` set to False.
    Files whose name starts with a dot (a Mac's ``._KP_R1.fastq.gz``) are passed over in
    a folder and counted with the other files; given as files they are taken.

    Accepted: ``.fastq``, ``.fq``, ``.fastq.gz``, ``.fq.gz`` in any letter case. The two
    files of a sample are found by name, in the same folder: ``_R1``/``_R2``,
    ``.R1``/``.R2``, ``_1``/``_2``, ``.1``/``.2`` at the end of the name, and Illumina's
    ``<name>_S<n>_L<lane>_R1_001``. The sample name is what stands before the read marker
    (for the Illumina form also without ``_S<n>`` and ``_L<lane>``), made safe with
    ``fsutil.safe_name``. Lanes are grouped only for the Illumina form, any number of
    them, in lane order. A file given by itself brings in its partner from the same folder.

    Rows that cannot run come back as samples with state Left out and the reason in
    ``note``: no partner, a lane with one file, the same file twice, compressed and plain
    files mixed, "Undetermined" reads. Index reads and files that are not FASTQ go to
    ``ignored`` (other files inside a searched folder are only counted). Name clashes are
    not judged here: see :func:`merge` and :func:`mark_name_clashes`.

    ``progress(n)`` is called after each folder with the number of FASTQ files seen so
    far; ``should_stop()`` returning True ends the search early (``stopped`` is then True).
    """
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    result = ScanResult()
    files: Dict[str, _File] = {}
    own_trimmed: Dict[str, Ignored] = {}      # met in a folder; dropped again if also given as files
    for raw in paths:
        if not os.fspath(raw):
            continue                              # abspath("") would be the working directory
        path = os.path.abspath(os.fsdecode(raw))
        base = os.path.basename(path) or path
        if os.path.isdir(path):
            _walk(path, files, own_trimmed, result, progress, should_stop)
        elif os.path.isfile(path):
            name = _parse_name(base)
            if name is None:
                reason = _why_not_fastq(base)
                result.ignored.append(Ignored(path, reason.reason, reason.kind))
            else:
                files[_norm(path)] = _File(path, os.path.dirname(path), name, True)
        else:
            result.ignored.append(Ignored(path, "%s was not found." % base, "missing"))
    _pull_partners(files)
    left_alone = [item for key, item in own_trimmed.items() if key not in files]
    result.ignored.extend(left_alone)
    result.fastq_files = len(files) + len(left_alone)

    slots: Dict[Tuple[Any, ...], Dict[int, List[_File]]] = {}
    samples: List[Sample] = []
    for item in files.values():
        if item.name.index:
            result.ignored.append(Ignored(item.path, "%s holds index reads (the barcodes), not the "
                                          "reads of a sample." % os.path.basename(item.path), "index"))
        elif item.name.read == 0:
            samples.append(_make_sample(item.name.sample, [((0, 0), item, None)]))
        else:
            slots.setdefault(_slot_key(item), {1: [], 2: []})[item.name.read].append(item)

    grouped: Dict[Tuple[Any, ...], List[_Placed]] = {}
    raw_names: Dict[Tuple[Any, ...], str] = {}
    for key, sides in slots.items():
        folder, form, raw_name, snum, lane, chunk = key
        # A file without a lane number is never joined to files that have one: it may
        # already be all the lanes put together by hand.
        sample_key = (folder, form, raw_name, snum, lane is not None) if form == "illumina" else key
        raw_names[sample_key] = raw_name
        for a, b in _pair_up(sides[1], sides[2]):
            grouped.setdefault(sample_key, []).append(((lane or 0, chunk or 0), a, b))
    for sample_key, placed in grouped.items():
        for group in _split_copies(placed):
            samples.append(_make_sample(raw_names[sample_key], group))

    samples.sort(key=lambda s: (_natural(s.name), _natural(s.read1 or s.read2)))
    result.samples = samples
    result.ignored.sort(key=lambda i: _natural(i.path))
    return result


# --------------------------------------------------------------------------- what the names say

def _lane_label(sample: Sample, index: int) -> str:
    """"Lane 3", from the file name when it has a lane number, else by position."""
    present = sample.lanes[index][0] or sample.lanes[index][1]
    name = _parse_name(os.path.basename(present)) if present else None
    if name is not None and name.lane is not None:
        return "Lane %d" % name.lane
    return "Lane %d" % (index + 1)


def _static_problem(sample: Sample) -> str:
    """The reason this sample cannot run, as far as its file names tell. "" if none."""
    files = _files(sample)
    if not files:
        return NO_FILES
    if not all(_writable_text(p) for p in files):
        return BROKEN_NAME
    if any(_undetermined_name(os.path.basename(p)) for p in files):
        return UNDETERMINED
    several = len(sample.lanes) > 1
    for index, (read1, read2) in enumerate(sample.lanes):
        if read1 and read2:
            continue
        if not read1 and not read2:
            return NO_FILES
        if several:
            return "%s has no Read %d file." % (_lane_label(sample, index), 2 if read1 else 1)
        name = _parse_name(os.path.basename(read1 or read2))
        if name is None or name.read == 0:
            return NO_PARTNER
        return NO_READ2 if read1 else NO_READ1
    for read1, read2 in sample.lanes:
        if _norm(read1) == _norm(read2):
            return SAME_FILE
    for read1, read2 in sample.lanes:
        if _is_gz(read1) != _is_gz(read2):
            return PAIR_COMPRESSION if not several else LANES_COMPRESSION
    if len({_is_gz(p) for p in files}) > 1:
        return LANES_COMPRESSION
    return ""


def _raw_name(sample: Sample) -> str:
    files = _files(sample)
    name = _parse_name(os.path.basename(files[0])) if files else None
    return name.sample if name is not None else ""


def _static_note(sample: Sample) -> str:
    """Harmless things the file names tell: a cleaned name, reads this program trimmed."""
    notes: List[str] = []
    raw = _raw_name(sample)
    if raw and raw != sample.name and fsutil.safe_name(raw) == sample.name:
        notes.append(_NAME_CHANGED % (raw, sample.name))
    names = [_parse_name(os.path.basename(p)) for p in _files(sample)]
    if names and all(n is not None and n.own_trimmed for n in names):
        notes.append(ALREADY_TRIMMED)
    return " ".join(notes)


def is_undetermined(sample: Sample) -> bool:
    """True for the "Undetermined" reads of a run (recognised by file name)."""
    return any(_undetermined_name(os.path.basename(p)) for p in _files(sample))


def has_no_partner(sample: Sample) -> bool:
    """True when a file of the sample is missing its Read 1 or Read 2."""
    return any(bool(a) != bool(b) for a, b in sample.lanes)


def is_name_clash(sample: Sample) -> bool:
    """True while the sample is Left out only because another sample has its name."""
    return sample.state is SampleState.LEFT_OUT and bool(_CLASH_RE.match(sample.note))


def _left_out_for_its_name(sample: Sample) -> bool:
    """Marked by :func:`mark_name_clashes`: a shared name, or a name the program needs itself."""
    return sample.state is SampleState.LEFT_OUT and bool(
        _CLASH_RE.match(sample.note) or _RESERVED_RE.match(sample.note))


# --------------------------------------------------------------------------- peek

#: Bytes read from the start of a file, compressed or not. Never more.
PEEK_BYTES = 64 * 1024
_PEEK_TEXT = 256 * 1024          # most text looked at after decompression
_PEEK_RECORDS = 400
_LONG_READ_BASES = 1000
_OTHER_MAGIC = (b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd", b"PK\x03\x04", b"7z\xbc\xaf\x27\x1c")
_BOM = b"\xef\xbb\xbf"


class PeekStatus(str, enum.Enum):
    OK = "ok"
    PATH_TOO_LONG = "path_too_long"
    MISSING = "missing"
    ONLINE_ONLY = "online_only"            # a cloud placeholder; it was not opened
    UNREADABLE = "unreadable"              # exists, but cannot be opened (still copying, in use, no rights)
    EMPTY = "empty"                        # 0 bytes, or a gzip file with nothing in it
    DAMAGED = "damaged"                    # starts like gzip but cannot be decompressed
    OTHER_COMPRESSION = "other_compression"
    FASTA = "fasta"
    NOT_FASTQ = "not_fastq"
    WRONG_EXTENSION = "wrong_extension"    # gzip data without .gz, or plain data with .gz
    WINDOWS_LINE_ENDS = "windows_line_ends"
    LONG_READS = "long_reads"
    INTERLEAVED = "interleaved"


_PEEK_SENTENCES: Dict[PeekStatus, str] = {
    PeekStatus.PATH_TOO_LONG: "The path of this file is too long for Windows. Move the file to a "
                              "folder with a shorter path.",
    PeekStatus.ONLINE_ONLY: "This file is stored online only. In Explorer, right-click it and choose "
                            "'Always keep on this device', then add it again.",
    PeekStatus.UNREADABLE: "This file cannot be opened. It may still be copying, another program may "
                           "be using it, or you may not be allowed to read it.",
    PeekStatus.EMPTY: "This file is empty.",
    PeekStatus.DAMAGED: "This file is damaged: it cannot be decompressed. Copy or download it again.",
    PeekStatus.OTHER_COMPRESSION: "This kind of compression is not supported. Convert the file to "
                                  ".fastq.gz first.",
    PeekStatus.FASTA: "This is a FASTA file (sequences without quality values). The program needs "
                      "FASTQ reads.",
    PeekStatus.NOT_FASTQ: "Not a FASTQ file: the content does not look like sequencing reads.",
    PeekStatus.WINDOWS_LINE_ENDS: "This file has Windows line ends, which the trimmer and the "
                                  "assembler cannot read reliably. It was probably opened and saved "
                                  "in a text editor. Use the original file from the sequencer.",
    PeekStatus.LONG_READS: "These look like long reads (Nanopore or PacBio). This program assembles "
                           "Illumina paired-end reads only.",
    PeekStatus.INTERLEAVED: "Both reads seem to be in this one file. This program needs them in two "
                            "files.",
}
_GZIP_WITHOUT_GZ = ("This file is compressed, but its name does not end in .gz. Rename it so that it "
                    "ends in .fastq.gz, then add it again.")
_GZ_NOT_GZIP = ("The name of this file ends in .gz, but the file is not compressed. Rename it so that "
                "it ends in .fastq, then add it again.")
_BYTE_ORDER_MARK = ("This file was changed by a text editor (it starts with a hidden mark). Use the "
                    "original file from the sequencer.")
_CUT_SHORT = ("This file is incomplete: it stops in the middle. If it is still being copied, wait "
              "until the copy has finished. Otherwise copy or download it again.")
# When every file of a sample has the same trouble, one sentence speaks for all of them.
_PEEK_PLURAL: Dict[PeekStatus, str] = {
    PeekStatus.PATH_TOO_LONG: "The paths of these files are too long for Windows. Move the files to "
                              "a folder with a shorter path.",
    PeekStatus.ONLINE_ONLY: "These files are stored online only. In Explorer, select them, right-click "
                            "and choose 'Always keep on this device', then add them again.",
    PeekStatus.UNREADABLE: "These files cannot be opened. They may still be copying, another program "
                           "may be using them, or you may not be allowed to read them.",
    PeekStatus.EMPTY: "These files are empty.",
}


def _missing_sentence(path: str, plural: bool = False,
                      splitdrive: Optional[Callable[[str], Tuple[str, str]]] = None) -> str:
    drive = (splitdrive or os.path.splitdrive)(path)[0]
    head = "Files not found." if plural else "File not found."
    if len(drive) == 2 and drive.endswith(":"):
        return "%s Is drive %s still connected?" % (head, drive.upper())
    if drive:
        return "%s Is the network folder still connected?" % head
    return "%s %s been moved, renamed or deleted?" % (head, "Have they" if plural else "Has it")


@dataclass
class PeekResult:
    """What the first kilobytes of one file say. ``problem`` is "" when nothing is wrong."""
    path: str
    status: PeekStatus = PeekStatus.OK
    problem: str = ""                       # a plain sentence about this file
    size: int = 0                           # bytes on disk
    gzip_name: bool = False                 # the name ends in .gz
    gzip_content: Optional[bool] = None     # the content is gzip; None when the file was not opened
    windows_line_ends: bool = False
    records: int = 0                        # complete reads seen in the part that was looked at
    first_read_name: str = ""               # without "@", the comment and a final /1 or /2
    longest_read: int = 0                   # bases, among the reads seen

    @property
    def ok(self) -> bool:
        return self.status is PeekStatus.OK


def _fail(result: PeekResult, status: PeekStatus, sentence: str = "") -> PeekResult:
    result.status = status
    result.problem = sentence or _PEEK_SENTENCES[status]
    return result


def _inflate_start(raw: bytes) -> Tuple[bytes, bool, bool]:
    """Decompress the start of a gzip file.

    Returns (text, the data ended cleanly, the data stopped inside a compressed block).
    The last is True when ``raw`` ran out before the block it was in was complete, which
    for a file that was read to its end means that the file is cut short.
    """
    out = b""
    data = raw
    clean_end = False
    while data and len(out) < _PEEK_TEXT:
        inflater = zlib.decompressobj(31)
        try:
            out += inflater.decompress(data, _PEEK_TEXT - len(out))
        except zlib.error:
            if out:                 # something unreadable after a good part: not judged here
                return out, False, False
            raise
        if not inflater.eof:
            return out, False, len(out) < _PEEK_TEXT
        data = inflater.unused_data
        clean_end = True
    return out, clean_end and not data, False


def _read_name(header: bytes) -> str:
    words = header[1:].split(None, 1)
    name = words[0].decode("utf-8", "replace") if words else ""
    return name[:-2] if name.endswith(("/1", "/2")) else name


def _mate_number(header: bytes) -> int:
    """1 or 2 when the header says which read of the pair it is ("/1", or " 2:N:0:..."); else 0."""
    words = header[1:].split(None, 2)
    if not words:
        return 0
    if words[0].endswith((b"/1", b"/2")):
        return int(words[0][-1:])
    if len(words) > 1 and words[1][:2] in (b"1:", b"2:"):
        return int(words[1][:1])
    return 0


def _same_read(first: str, second: str) -> bool:
    """Do the first read names of two files belong to one pair?

    Equal names do; so do names that differ only in a final 1 and 2 after a separator
    ("SRR1.7.1" and "SRR1.7.2"), which the engine pairs as well.
    """
    if first == second:
        return True
    return (len(first) == len(second) and len(first) > 2 and first[:-1] == second[:-1]
            and {first[-1], second[-1]} == {"1", "2"} and first[-2] in "._:-")


def _look_inside(result: PeekResult, raw: bytes) -> PeekResult:
    whole = len(raw) >= result.size
    if result.gzip_content:
        try:
            text, clean_end, cut = _inflate_start(raw)
        except zlib.error:
            return _fail(result, PeekStatus.DAMAGED)
        if whole and cut:
            # All of a small file was read and its compressed data has no end: a copy that
            # stopped early. The trimmer would take such a file without a word.
            return _fail(result, PeekStatus.DAMAGED, _CUT_SHORT)
        whole = whole and clean_end
        if not text:
            return _fail(result, PeekStatus.EMPTY if whole else PeekStatus.DAMAGED)
    else:
        if raw.startswith(_OTHER_MAGIC):
            return _fail(result, PeekStatus.OTHER_COMPRESSION)
        text = raw
    byte_order_mark = text.startswith(_BOM)
    if byte_order_mark:
        text = text[len(_BOM):]
    lines = text.split(b"\n")
    tail = lines.pop()                       # what follows the last line end: nothing, or a cut line
    if whole and tail:
        lines.append(tail)
        tail = b""
    result.windows_line_ends = any(line.endswith(b"\r") for line in lines[:8])
    lines = [line[:-1] if line.endswith(b"\r") else line for line in lines]
    first = lines[0] if lines else tail
    if first.startswith(b">"):
        return _fail(result, PeekStatus.FASTA)
    if not first.startswith(b"@"):
        return _fail(result, PeekStatus.NOT_FASTQ)
    headers: List[bytes] = []
    for start in range(0, min(len(lines) - 3, 4 * _PEEK_RECORDS), 4):
        header, bases, plus, quality = lines[start:start + 4]
        # A read with no bases left ("@name", "", "+", "") is a read: tools that trim without
        # a length filter write them, and both engines and the read check accept them.
        if not header.startswith(b"@") or not plus.startswith(b"+") or len(bases) != len(quality):
            break
        result.records += 1
        result.longest_read = max(result.longest_read, len(bases))
        if len(headers) < 2:
            headers.append(header)
    if not result.records:
        # One read that does not fit in the part looked at can only be a long read.
        bases = lines[1] if len(lines) > 1 else (tail if lines else b"")
        if not whole and len(lines) < 4 and len(bases) > _LONG_READ_BASES:
            return _fail(result, PeekStatus.LONG_READS)
        return _fail(result, PeekStatus.NOT_FASTQ)
    result.first_read_name = _read_name(headers[0])
    if result.gzip_content != result.gzip_name:
        return _fail(result, PeekStatus.WRONG_EXTENSION,
                     _GZIP_WITHOUT_GZ if result.gzip_content else _GZ_NOT_GZIP)
    if byte_order_mark:
        return _fail(result, PeekStatus.WINDOWS_LINE_ENDS, _BYTE_ORDER_MARK)
    if result.windows_line_ends:
        return _fail(result, PeekStatus.WINDOWS_LINE_ENDS)
    if result.longest_read > _LONG_READ_BASES:
        return _fail(result, PeekStatus.LONG_READS)
    # Read 1 and Read 2 of one pair following each other: same name, numbered 1 then 2.
    if len(headers) == 2 and result.first_read_name == _read_name(headers[1]) \
            and (_mate_number(headers[0]), _mate_number(headers[1])) == (1, 2):
        return _fail(result, PeekStatus.INTERLEAVED)
    return result


def _default_max_path() -> Optional[int]:
    return 259 if os.name == "nt" else None


def peek(path: str, is_online_only: Optional[Callable[[str], bool]] = None, *,
         max_path: Optional[int] = None) -> PeekResult:
    """A quick look at one read file: its first 64 KB at most, never the whole file.

    Finds: a missing file; a file stored online only; an empty file (0 bytes, or a gzip
    file with nothing in it); a file that cannot be opened or decompressed; content that
    is not FASTQ (the first read must be four lines, "@" and "+", bases and qualities of
    equal length); gzip content under a name without ``.gz`` and the reverse; Windows line
    ends; long reads; both reads in one file. A compressed file small enough to be read
    whole (64 KB) that stops in the middle is found too. A larger file that is cut off
    further down is not: that is the read check's job.

    ``is_online_only(path) -> bool`` says whether the file is a cloud placeholder (OneDrive
    "online only"). Such a file is **not opened**, because opening it would start a
    download. Left out, no file counts as online only. ``max_path`` is the longest path
    the engines can open (default: 259 characters on Windows, no limit elsewhere).
    """
    result = PeekResult(path=path, gzip_name=_is_gz(path))
    limit = _default_max_path() if max_path is None else max_path
    if limit and len(os.path.abspath(path)) > limit:
        return _fail(result, PeekStatus.PATH_TOO_LONG)
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return _fail(result, PeekStatus.MISSING, _missing_sentence(path))
    except (OSError, ValueError):
        return _fail(result, PeekStatus.UNREADABLE)
    result.size = info.st_size
    if is_online_only is not None:
        try:
            online = bool(is_online_only(path))
        except Exception:                    # noqa: BLE001 - a broken probe must not hide the file
            online = False
        if online:
            return _fail(result, PeekStatus.ONLINE_ONLY)
    if not stat.S_ISREG(info.st_mode):
        return _fail(result, PeekStatus.NOT_FASTQ)       # a folder, a pipe, a device: never opened
    if result.size == 0:
        return _fail(result, PeekStatus.EMPTY)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(PEEK_BYTES)
    except OSError:
        return _fail(result, PeekStatus.UNREADABLE)
    if not raw:
        return _fail(result, PeekStatus.EMPTY)
    result.gzip_content = raw[:2] == b"\x1f\x8b"
    return _look_inside(result, raw)


def assess_sample(sample: Sample, is_online_only: Optional[Callable[[str], bool]] = None, *,
                  max_path: Optional[int] = None) -> Tuple[SampleState, str]:
    """The state and note :func:`check_sample` would set, without changing the sample."""
    problem = _static_problem(sample)
    if problem:
        return SampleState.LEFT_OUT, problem
    several = len(sample.lanes) > 1
    looks: List[Tuple[int, int, PeekResult]] = []
    for index, pair in enumerate(sample.lanes):
        for role, path in enumerate(pair, start=1):
            looks.append((index, role, peek(path, is_online_only, max_path=max_path)))
    bad = [(index, role, look) for index, role, look in looks if not look.ok]
    if bad:
        index, role, look = bad[0]
        status = look.status
        if all(other.status is status for _, _, other in looks):
            if status is PeekStatus.MISSING:
                return SampleState.LEFT_OUT, _missing_sentence(look.path, plural=True)
            if status in _PEEK_PLURAL:
                return SampleState.LEFT_OUT, _PEEK_PLURAL[status]
        label = "Read %d" % role
        if several:
            label = "%s, %s" % (_lane_label(sample, index), label)
        return SampleState.LEFT_OUT, "%s: %s" % (label, look.problem)
    if any(_same_file(a, b) for a, b in sample.lanes):
        return SampleState.LEFT_OUT, SAME_FILE
    by_place = {(index, role): look for index, role, look in looks}
    for index in range(len(sample.lanes)):
        first, second = by_place[(index, 1)], by_place[(index, 2)]
        if first.first_read_name and second.first_read_name \
                and not _same_read(first.first_read_name, second.first_read_name):
            if several:
                return SampleState.LEFT_OUT, "%s: %s" % (_lane_label(sample, index), NAMES_DIFFER)
            return SampleState.LEFT_OUT, NAMES_DIFFER

    notes: List[str] = []
    static = _static_note(sample)
    if static:
        notes.append(static)
    total = sum(look.size for _, _, look in looks)
    factor = 1 if _is_gz(sample.read1) else _PLAIN_FACTOR
    if total < TINY_PAIR_BYTES * factor:
        notes.append(TINY)
    elif total > HUGE_PAIR_BYTES * factor:
        notes.append(HUGE)
    for index in range(len(sample.lanes)):
        sizes = sorted((by_place[(index, 1)].size, by_place[(index, 2)].size))
        if sizes[1] >= _LOPSIDED_MIN_BYTES and sizes[1] > sizes[0] * _LOPSIDED_RATIO:
            notes.append(LOPSIDED)
            break
    if notes:
        return SampleState.READY_NOTE, " ".join(notes)
    return SampleState.READY, ""


def check_sample(sample: Sample, is_online_only: Optional[Callable[[str], bool]] = None, *,
                 max_path: Optional[int] = None) -> Sample:
    """Look into the files of a sample (:func:`peek`) and set its ``state`` and ``note``.

    Left out, with the first serious reason: what the file names already said, then the
    first file whose peek found a problem ("Read 2: This file is empty."), then two files
    whose first read names do not belong together. Otherwise Ready; with a note when
    there is a harmless oddity: a name that was cleaned, reads this program trimmed
    itself, very little data, a very large pair, two files of very different size.

    It reads files, so call it from a worker thread. Only ``state``, ``note`` and
    ``size_bytes`` of the sample change. It knows nothing about the other rows: call
    :func:`mark_name_clashes` on the list afterwards. Returns the same sample.
    """
    state, note = assess_sample(sample, is_online_only, max_path=max_path)
    size = sum(_size(p) for p in _files(sample))
    if size:
        sample.size_bytes = size
    sample.__dict__.pop(_BEFORE_CLASH, None)
    sample.state, sample.note = state, note
    return sample


# --------------------------------------------------------------------------- the list

def _file_set(sample: Sample) -> frozenset:
    return frozenset(_norm(p) for p in _files(sample))


@dataclass
class MergeResult:
    """The list after adding. ``samples`` is the new list; the rest says what happened."""
    samples: List[Sample] = field(default_factory=list)
    added: List[Sample] = field(default_factory=list)
    duplicates: List[Sample] = field(default_factory=list)     # found again; not added
    replaced: List[Sample] = field(default_factory=list)       # rows that a fuller sample took the place of

    @property
    def duplicate_files(self) -> int:
        return sum(len(_files(s)) for s in self.duplicates)

    @property
    def note(self) -> str:
        """"Already in the list: 12 files." or "" when nothing was found twice."""
        count = self.duplicate_files
        return "Already in the list: %s." % _plural(count, "file") if count else ""


def merge(existing: Sequence[Sample], found: Union[ScanResult, Iterable[Sample]]) -> MergeResult:
    """Add newly found samples to the list. Neither argument is changed in length.

    A found sample whose files are all part of one sample already in the list is a
    duplicate: it is skipped and reported. A found sample that shares read files with rows
    of the list is the same sample seen more fully: the Read 2 that was missing has
    arrived, or a row held one lane and the sample is now found with all its lanes. It
    takes the place of those rows, as one row with the lanes of all of them in lane order,
    so that no read file is assembled twice under two names. If a row it replaces would
    have run, the name and the Trim tick the user gave that row are kept. (Rows that name
    two different files for the same lane cannot be made one; the found sample is then
    added as a row of its own.) Everything else is appended, in the order found. The
    order of the existing rows is kept. Name clashes are marked on the new list
    (:func:`mark_name_clashes`).

    A row made here from several rows is new: look inside it with :func:`check_sample`
    like every sample in ``added``.
    """
    incoming = list(found.samples if isinstance(found, ScanResult) else found)
    result = MergeResult(samples=list(existing))
    listed = [_file_set(other) for other in result.samples]      # kept in step with result.samples
    for sample in incoming:
        files = _file_set(sample)
        if not files:
            continue
        if any(files <= other for other in listed):
            result.duplicates.append(sample)
            continue
        parts = [i for i in range(len(listed)) if listed[i] & files]
        whole: Optional[Sample] = sample
        for i in parts:
            if whole is not None and not listed[i] <= _file_set(whole):
                whole = _union(result.samples[i], whole)
        if whole is None:
            # A row names another file for the same lane: that row stays as it is. Rows
            # that are wholly part of the found sample still give way to it.
            whole = sample
            parts = [i for i in parts if listed[i] < files]
        if parts:
            olds = [result.samples[i] for i in parts]
            result.replaced.extend(olds)
            chosen = next((old for old in olds if old.ready or _left_out_for_its_name(old)), None)
            if chosen is not None:
                _keep_choices(whole, chosen)
            result.samples[parts[0]] = whole
            listed[parts[0]] = _file_set(whole)
            for i in reversed(parts[1:]):
                del result.samples[i]
                del listed[i]
            result.added.append(whole)
        else:
            result.samples.append(sample)
            listed.append(files)
            result.added.append(sample)
    mark_name_clashes(result.samples)
    return result


def _union(old: Sample, new: Sample) -> Optional[Sample]:
    """One sample with the lanes of both, in lane order. None if they cannot be one sample:
    a file whose name says nothing, or two different files for the same lane and read."""
    places: Dict[Tuple[int, int], Dict[int, _File]] = {}
    raw_name = ""
    for sample in (new, old):
        for lane in sample.lanes:
            for role, path in enumerate(lane, start=1):
                if not path:
                    continue
                name = _parse_name(os.path.basename(path))
                if name is None:
                    return None
                raw_name = raw_name or name.sample
                sides = places.setdefault((name.lane or 0, name.chunk or 0), {})
                if role in sides and _norm(sides[role].path) != _norm(path):
                    return None
                sides[role] = _File(path, os.path.dirname(path), name, True)
    if not places:
        return None
    return _make_sample(raw_name, [(place, sides.get(1), sides.get(2)) for place, sides in places.items()])


def _keep_choices(sample: Sample, old: Sample) -> None:
    """``sample`` takes the place of the row ``old``: carry over what the user chose there."""
    if old.name != sample.name:
        sample.name = old.name
        _forget_cleaned_name(sample)
    if sample.trim and not old.trim:
        sample.trim = False


def _clash_note(names: List[str]) -> str:
    count = "Two" if len(names) == 2 else "%d" % len(names)
    todo = ("Rename one of them (double-click the name)." if len(names) == 2
            else "Rename them so that every name is different (double-click the name).")
    different = sorted(set(names), key=_natural)
    if len(different) == 1:
        return "%s samples are called %s. %s" % (count, different[0], todo)
    return ("%s samples have the same name apart from capital letters (%s), and Windows treats "
            "such names as one. %s" % (count, " and ".join(different), todo))


def _clash_groups(samples: Sequence[Sample]) -> List[List[Sample]]:
    by_name: Dict[str, List[Sample]] = {}
    for sample in samples:
        if sample.ready:
            by_name.setdefault(sample.name.casefold(), []).append(sample)
    return [group for group in by_name.values() if len(group) > 1]


def mark_name_clashes(samples: Sequence[Sample]) -> List[Sample]:
    """Set samples that share a name to Left out, with the reason. Returns those samples.

    Names that differ only in capital letters count as the same: on Windows they would be
    one folder. Only samples that would run are compared; a row that is Left out for a
    reason of its own keeps that reason. The call starts from scratch every time: a sample
    marked by an earlier call whose name is unique now gets back the state and note it had
    before. Call it after every change of the list or of a name.

    A sample whose name the program needs for itself in the results folder ("assemblies",
    "last_run.json", ...) is set to Left out in the same way, until it is renamed. It is
    not among the samples returned.
    """
    for sample in samples:
        if _left_out_for_its_name(sample):
            before = sample.__dict__.pop(_BEFORE_CLASH, None)
            if before is None:
                problem = _static_problem(sample)
                note = _static_note(sample)
                before = ((SampleState.LEFT_OUT, problem) if problem else
                          (SampleState.READY_NOTE, note) if note else (SampleState.READY, ""))
            sample.state, sample.note = before
        else:
            sample.__dict__.pop(_BEFORE_CLASH, None)
    marked: List[Sample] = []
    for group in _clash_groups(samples):
        note = _clash_note([s.name for s in group])
        for sample in group:
            sample.__dict__[_BEFORE_CLASH] = (sample.state, sample.note)
            sample.state, sample.note = SampleState.LEFT_OUT, note
            marked.append(sample)
    for sample in samples:
        if sample.ready and sample.name.casefold() in _RESERVED_NAMES:
            sample.__dict__[_BEFORE_CLASH] = (sample.state, sample.note)
            sample.state, sample.note = SampleState.LEFT_OUT, _RESERVED_NAME % sample.name
    return marked


def _forget_cleaned_name(sample: Sample) -> None:
    """After a rename the note "Name changed from ..." is no longer true."""
    def without(state: SampleState, note: str) -> Tuple[SampleState, str]:
        shorter = _NAME_CHANGED_RE.sub("", note).strip()
        if shorter == note:
            return state, note
        if state is SampleState.READY_NOTE and not shorter:
            return SampleState.READY, ""
        return state, shorter

    before = sample.__dict__.get(_BEFORE_CLASH)
    if before is not None:
        sample.__dict__[_BEFORE_CLASH] = without(*before)
    elif not _left_out_for_its_name(sample):
        sample.state, sample.note = without(sample.state, sample.note)


_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\s]')


def name_problem(samples: Sequence[Sample], sample: Optional[Sample], new_name: str) -> str:
    """Why ``new_name`` cannot be the name of ``sample``, in one sentence. "" when it can.

    For the check while the user types. A name must be usable as a Windows folder name as
    it stands (``fsutil.safe_name`` would not change it) and must not be the name of
    another sample in the list, capital letters aside.
    """
    name = new_name.strip() if isinstance(new_name, str) else ""
    if not name:
        return "Type a name for the sample."
    if len(name) > fsutil.MAX_NAME:
        return "Too long: %d characters at most." % fsutil.MAX_NAME
    if _FORBIDDEN_RE.search(name):
        return 'A name cannot contain spaces or any of these characters: < > : " / \\ | ? *'
    safe = fsutil.safe_name(name)
    if safe != name:
        if safe == name + "_":
            return "Windows does not allow the name %s. Choose another name." % name
        if name[0] in "._" or name[-1] in "._":
            return "A name cannot begin or end with a dot or an underscore."
        return "This name cannot be used as it is. Try %s." % safe
    folded = name.casefold()
    if folded in _RESERVED_NAMES:
        return "The program uses the name %s for its own files. Choose another name." % name
    if any(other is not sample and other.name.casefold() == folded for other in samples):
        return "There is already a sample with this name."
    return ""


def rename(samples: Sequence[Sample], sample: Sample, new_name: str) -> str:
    """Give ``sample`` (one of ``samples``) a new name.

    Returns "" when it is done, otherwise the sentence that says why not; it never raises
    for a bad name. Afterwards the name clashes of the whole list are marked again, so the
    row that shared the old name becomes Ready by itself.
    """
    problem = name_problem(samples, sample, new_name)
    if problem:
        return problem
    sample.name = new_name.strip()
    _forget_cleaned_name(sample)
    mark_name_clashes(samples)
    return ""


def _folder_parts(sample: Sample) -> List[str]:
    """The folders above the sample's first file, nearest first, as safe name pieces."""
    path = sample.read1 or sample.read2
    parts: List[str] = []
    folder = os.path.dirname(os.path.abspath(path)) if path else ""
    while folder:
        base = os.path.basename(folder)
        parent = os.path.dirname(folder)
        if not base or parent == folder:
            break
        parts.append(base)
        folder = parent
    return parts


def _with_tag(name: str, tag: str) -> str:
    tag = fsutil.safe_name(tag)[:30].strip("._ ")
    room = fsutil.MAX_NAME - len(tag) - 1
    return fsutil.safe_name("%s_%s" % (name[:room].rstrip("._ "), tag))


def make_names_different(samples: Sequence[Sample]) -> List[Sample]:
    """What the button "Make the names different (add the folder name)" does.

    Every sample that shares its name gets the name of the folder its reads are in added
    at the end: two samples called KP_001 in the folders run1 and run2 become KP_001_run1
    and KP_001_run2. If the folders have the same name too, the folder above is added as
    well; if nothing tells the samples apart (they are in one folder), they are numbered.
    A sample left out because the program needs its name ("assemblies") is renamed in the
    same way. Returns the samples that were renamed; the list is then free of name clashes.
    """
    mark_name_clashes(samples)
    groups = _groups_marked(samples)
    in_groups = {id(s) for group in groups for s in group}
    taken = {s.name.casefold() for s in samples if id(s) not in in_groups}
    renamed: List[Sample] = []
    for group in groups:
        parts = [_folder_parts(s) for s in group]
        chosen: Optional[List[str]] = None
        for depth in range(1, 5):
            tags = ["_".join(reversed(p[:depth])) for p in parts]
            if not all(tags):
                break
            names = [_with_tag(s.name, tag) for s, tag in zip(group, tags)]
            folded = [n.casefold() for n in names]
            if len(set(folded)) == len(folded) \
                    and not any(f in taken or f in _RESERVED_NAMES for f in folded):
                chosen = names
                break
        if chosen is None:
            chosen = []
            number = 1
            for sample in group:
                while True:
                    candidate = _with_tag(sample.name, "%d" % number)
                    number += 1
                    if candidate.casefold() not in taken \
                            and candidate.casefold() not in {c.casefold() for c in chosen}:
                        break
                chosen.append(candidate)
        for sample, name in zip(group, chosen):
            sample.name = name
            _forget_cleaned_name(sample)
            taken.add(name.casefold())
            renamed.append(sample)
    mark_name_clashes(samples)
    return renamed


def _groups_marked(samples: Sequence[Sample]) -> List[List[Sample]]:
    """The samples currently left out for their name, in list order: those that share a
    name as one group each, a sample with a name the program needs as a group of one."""
    by_name: Dict[str, List[Sample]] = {}
    for sample in samples:
        if _left_out_for_its_name(sample):
            by_name.setdefault(sample.name.casefold(), []).append(sample)
    return [group for name, group in by_name.items() if len(group) > 1 or name in _RESERVED_NAMES]


# --------------------------------------------------------------------------- disk and paths

#: Kept free on top of every estimate (ux-windows.md C-17; the same 2 GB as the bash rule).
DISK_MARGIN_BYTES = 2 * 1024 ** 3
#: What one finished sample leaves behind apart from its trimmed reads: the contigs, the
#: assembly graph and the copy in ``assemblies`` (each about the size of the genome) and
#: the reports (about 1 MB). Measured: 1.9 MB for a 0.3 Mb genome, so about 16 MB for 5 Mb.
RESULT_BYTES_PER_SAMPLE = 32 * 1024 ** 2
#: The longest path a run may create (ux-windows.md A-4, row 4.10).
MAX_RESULT_PATH = 240


def sample_disk_need(sample: Union[Sample, int]) -> int:
    """Free bytes the read check asks for before one sample is assembled.

    The rule of the bash ``tesseract`` script (line 243): three times the size of the two
    read files, plus 2 GB. Takes a sample (its ``size_bytes``) or a number of bytes.
    """
    size = sample.size_bytes if isinstance(sample, Sample) else int(sample)
    return (max(0, size) * 3 // 1024 + 2 * 1024 * 1024) * 1024


def estimate_disk_need(samples: Iterable[Sample], keep_trimmed: bool) -> int:
    """Free bytes the results folder needs for the whole batch. 0 for an empty batch.

    Only samples that will run are counted. With ``size`` the size of a sample's read files::

        need = kept + results + largest working space + 2 GB

        kept           = sum of size over the samples with Trim ticked, if keep_trimmed
                         (trimmed reads are about as large as their inputs and stay)
        results        = 32 MB for every sample (RESULT_BYTES_PER_SAMPLE: contigs, graph,
                         reports and the copy in ``assemblies``, which all stay)
        working space  = size      if the sample has several lanes (the joined copy)
          of one sample + size      if Trim is ticked and not keep_trimmed (trimmed reads
                                    that are deleted when the sample is done)
                        + 3 x size  (what the read check itself asks for: the bash rule)

    Working space is needed for one sample at a time, so only the largest counts. The
    2 GB margin is the one of :func:`sample_disk_need`, counted once.

    The read check of every sample asks for three times its reads plus 2 GB of free space
    at the moment it runs, when everything the earlier samples left is already on the
    disk. The last term and the ``results`` term are in the formula so that a batch this
    function lets through passes that "disk" line for every sample, the last one too, as
    long as trimmed reads are no larger than their inputs and a sample leaves no more
    than 32 MB of results. It is an estimate: the batch still checks before each sample.
    """
    kept = 0
    working = 0
    running = 0
    for sample in samples:
        if not sample.ready:
            continue
        running += 1
        size = max(0, int(sample.size_bytes))
        if sample.trim and keep_trimmed:
            kept += size
        space = 3 * size
        if len(sample.lanes) > 1:
            space += size
        if sample.trim and not keep_trimmed:
            space += size
        working = max(working, space)
    if not running:
        return 0
    return kept + running * RESULT_BYTES_PER_SAMPLE + working + DISK_MARGIN_BYTES


# The deepest names known inside a sample folder (decision D1).
# - The folder: where results.py parks the earlier result while a new one is swapped in.
#   It is longer than "_in_progress" alone, "last_failed_attempt" and "new_result".
# - The file: tesseract-asm 1.6.2 with the wrapper's profile writes it; the longest sits
#   in the kept first attempt of the thin-data retry.
# Kept as parts and joined when asked, so the separator is that of the running system.
_DEEPEST_WORK_FOLDER = ("_in_progress", "replaced")
_DEEPEST_ASSEMBLY_FILE = ("assembly", "attempt1_default_k", "p2_self_qa.missing_hi.tsv")
_TEMPORARY_SUFFIX = ".tmp1234567"        # fsutil.atomic_write_text: ".tmp" and the process number


def longest_result_path(results_dir: str, samples: Iterable[Sample]) -> str:
    """The longest path a run into ``results_dir`` will create, for the 240-character check.

    Built from the file names of decision D1 for the samples that will run: the trimmed
    reads and the deepest assembly file, in the deepest place a sample folder can hold
    them (``<sample>\\_in_progress\\replaced``, where the earlier result waits while a new
    one is put in place), the copy in ``assemblies\\``, the status file, and the summary
    files of the folder itself. Compare ``len()`` of the result with :data:`MAX_RESULT_PATH`.
    """
    root = os.path.abspath(results_dir)
    candidates = [os.path.join(root, "run_summary.html" + _TEMPORARY_SUFFIX)]
    for sample in samples:
        if not sample.ready:
            continue
        folder = os.path.join(root, sample.name)
        work = os.path.join(folder, *_DEEPEST_WORK_FOLDER)
        candidates += [
            os.path.join(work, "trimming", max(trimmed_names(sample.name), key=len)),
            os.path.join(work, "trimming", TRIMMING_REPORT_HTML),
            os.path.join(work, *_DEEPEST_ASSEMBLY_FILE),
            os.path.join(folder, _STATUS_FILE + _TEMPORARY_SUFFIX),
            os.path.join(root, "assemblies", sample.name + ".fasta" + _TEMPORARY_SUFFIX),
        ]
    return max(candidates, key=len)
