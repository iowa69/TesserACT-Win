# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The read check: the preflight of the bash ``tesseract`` 1.6.2 script, in Python.

``check_reads`` gives the check lines, the counts and the text of ``preflight.txt`` that
the script gives for the same pair of read files; ``check_input_pair`` is the subset that
has to hold before a raw pair is trimmed, with messages meant for the screen.

The specification is ``docs/superpowers/audits/wrapper-port.md`` (sections 3 and 4, and the
quirk register in section 9). Everything the script does through gzip, paste, awk, od and
df is done here with the standard library:

* files are read as bytes, never as text; lines end at LF only and CR is data;
* a file is gzip when its first two bytes say so, whatever its name; "complete" means what
  ``gzip -t`` means: every member inflates, its CRC and length agree, and nothing follows
  the last member except zero bytes;
* the two files are paired row by row (four lines to a row) exactly as
  ``paste <(... | paste - - - -) <(... | paste - - - -)`` pairs them, including what
  happens when one file is shorter, ends in the middle of a record or holds a TAB;
* format, pairing, length and base counts look at every row; quality codes, adapters,
  poly-G and N look at the first 200,000 rows;
* fractions are rounded to text first and the limits are applied to the rounded value;
  the depth is ``"%.0f"`` of a double (ties go to the even number).

Speed: the files are decompressed in two threads (zlib releases the interpreter lock) and
the rows are examined a block at a time with list and bytes operations. A block falls
back to the row-by-row code, which is the script's awk program line for line, as soon as
it holds anything unusual. Both paths give the same numbers. Measured on the development
machine held to 4 cores: one million pairs of 2x150 in about 2 seconds and 85 MB of memory
(the bash script needs 6 seconds for the same files).

``wording="upstream"`` reproduces the script byte for byte. ``wording="windows"`` (the
default) changes only the three advice texts that name Linux tools (``fastp``,
``cutadapt``, ``seqtk``): they point to this program's Trimming page instead.

Deliberate departures from the script, in both wordings (audit section 9, "fix
deliberately"):

* F2  the genome size must be a positive decimal number. The script accepts any text and
  then prints ``about x`` or ``about +infx`` depending on the awk installed. Here a bad
  value raises ``ValueError``.
* F3  numbers are always printed with ``.``, whatever the locale.
* F7  a folder given as a read file is reported as ``cannot read``. The script calls it
  readable and then counts the other file as having too many reads.
* F8  free disk space comes from the operating system, not from the columns of ``df``.

Not a departure, but worth knowing: if a file that could be opened fails while it is being
read (a drive that drops out), the ``OSError`` reaches the caller. The script would report
a damaged file, which would be the wrong thing to tell the user.
"""

from __future__ import annotations

import math
import os
import queue
import re
import stat
import threading
import zlib
from dataclasses import dataclass
from itertools import repeat
from operator import itemgetter
from typing import Callable, List, Optional, Sequence, Tuple, Union

from . import fsutil
from .versions import TESSERACT_VERSION

__all__ = [
    "Cancelled", "CheckLine", "ReadStats", "ReadCheck", "InputProblem", "InputCheck",
    "check_reads", "check_input_pair", "genome_size_text", "gzip_complete",
    "encode_text", "write_text_exact",
    "WORDINGS", "DEFAULT_GENOME_MB", "SAMPLE_ROWS", "ADAPTER", "POLYG_TAIL",
]

PathLike = Union[str, "os.PathLike[str]"]
Progress = Optional[Callable[[float], None]]

# --------------------------------------------------------------------------- constants
# Appendix B of the audit. One place, so an upstream change is an edit to this block.

WORDINGS = ("upstream", "windows")
DEFAULT_GENOME_MB = "5"                 #: genome size assumed when none is given (megabases)
SAMPLE_ROWS = 200000                    #: quality, adapters, poly-G and N use the first rows only
ADAPTER = b"AGATCGGAAGAGC"              #: Illumina adapter start, searched in either read, forward only
POLYG_TAIL = b"G" * 10                  #: a read "ends in a poly-G run" when it ends in this
QUALITY_STEP = 7                        #: quality characters 1, 8, 15, ... are examined
QUALITY_LOW = 33                        #: a sampled code below this is not Phred+33
PHRED64_MIN, PHRED64_MAX = 64, 74       #: Phred+64 when the lowest code is >= 64 and the highest > 74
LONG_READ_LENGTH = 240                  #: longest read at or above this: "long-read MiSeq-type library"
DEPTH_THIN, DEPTH_LOW, DEPTH_DEEP = 15, 30, 400
ADAPTER_LIMIT, POLYG_LIMIT, N_LIMIT = 0.05, 0.02, 0.05
DISK_BASE_KB = 2097152                  #: 2 GB on top of three times the size of the read files
MEMORY_OK_KB = 4194304                  #: 4 GB

# The gzip magic, and a table that gives a quality character its code if it is '!'..'~' and
# 0 otherwise (the awk program looks the character up in a table that has only those).
_GZIP_MAGIC = b"\x1f\x8b"
_QUALITY_CODE = bytes(c if 33 <= c < 127 else 0 for c in range(256))

# Advice that names Linux tools in the script, and what this program says instead.
_ADVICE = {
    "upstream": {
        "phred64": "convert to Phred+33 first (e.g. seqtk seq -VQ64)",
        "adapters": "trim them first (fastp, cutadapt)",
        "polyg": "consider fastp --trim_poly_g",
    },
    "windows": {
        "phred64": "trim this sample first (Trimming page): trimming rewrites the qualities as Phred+33",
        "adapters": "trim them first (Remove adapters on the Trimming page)",
        "polyg": "consider Trim poly-G tails on the Trimming page",
    },
}

_IN_BLOCK = 1 << 20          # compressed bytes read at a time
_PLAIN_BLOCK = 4 << 20       # bytes read at a time from a file that is not compressed
_OUT_BLOCK = 8 << 20         # most decompressed bytes handed over at a time
_QUEUE_BLOCKS = 4            # blocks a reader may run ahead of the checks
_POLL = 0.05                 # seconds between looks at the cancel request while waiting


class Cancelled(Exception):
    """The check was stopped through its ``cancel`` event."""


# --------------------------------------------------------------------------- text

def _text(data: bytes) -> str:
    """Bytes of a read name as text that turns back into the same bytes."""
    return data.decode("utf-8", "surrogateescape")


def encode_text(text: str) -> bytes:
    """The bytes to write for ``text``: UTF-8, with the bytes of read names unchanged."""
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:          # a lone surrogate in a Windows file name
        return text.encode("utf-8", "replace")


def write_text_exact(path: str, text: str) -> None:
    """Write ``text`` to ``path`` byte for byte (LF line ends on every platform), atomically."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "wb") as handle:
        handle.write(encode_text(text))
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        fsutil.replace_file(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


_GENOME_SIZE = re.compile(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def genome_size_text(value: Union[str, float, int, None]) -> Optional[str]:
    """The genome size in megabases as the text the checks print, or None when not given.

    A string is kept as typed (``"5.0"`` stays ``5.0``, as in the script). A number is written
    plainly: ``5.0`` gives ``5.0``, ``0.3`` gives ``0.3``. Anything that is not a positive
    decimal number raises ``ValueError`` (departure F2).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("the genome size must be a positive number of megabases")
    if isinstance(value, (int, float)):
        number = float(value)
        if not (math.isfinite(number) and number > 0):
            raise ValueError("the genome size must be a positive number of megabases")
        if isinstance(value, int):
            return str(value)
        text = ("%.6f" % number).rstrip("0")
        if text.endswith("."):
            text += "0"
        return text if float(text) > 0 else repr(number)
    text = str(value)
    if text == "":
        return None                      # the script treats an empty --genome-size as not given
    if not _GENOME_SIZE.fullmatch(text):
        raise ValueError("the genome size must be a positive number of megabases, not %r" % text)
    number = float(text)
    if not (math.isfinite(number) and number > 0):
        raise ValueError("the genome size must be a positive number of megabases, not %r" % text)
    return text


# --------------------------------------------------------------------------- results

@dataclass(frozen=True)
class CheckLine:
    """One check: ``verdict`` is ``OK``, ``WARN`` or ``FAIL``; ``name`` is the check name."""
    verdict: str
    name: str
    message: str

    @property
    def text(self) -> str:
        """The line as the script prints it (without the line end)."""
        return "  %-6s %-22s %s" % (self.verdict, self.name, self.message)


@dataclass(frozen=True)
class ReadStats:
    """What the statistics pass measured. All zero when ``ran`` is False."""
    ran: bool = False                    #: the pass ran at all (no file, gzip or same-file failure)
    pairs: int = 0                       #: rows, counting the rows of the longer file
    bases: int = 0                       #: bases in well-formed pairs, every row
    max_length: int = 0                  #: longest read, every row
    mean_length: float = 0.0             #: mean read length of the sampled rows, one decimal
    depth: Optional[int] = None          #: estimated depth (x); None when no row was read
    genome_size_mb: str = DEFAULT_GENOME_MB   #: the genome size the depth was computed for
    genome_size_assumed: bool = True     #: True when that size is the default, not the user's
    adapter_fraction: float = 0.0        #: sampled pairs with adapter, rounded to 4 decimals
    polyg_fraction: float = 0.0          #: sampled pairs ending in poly-G, rounded to 4 decimals
    n_fraction: float = 0.0              #: N among the sampled bases, rounded to 5 decimals
    sampled_pairs: int = 0               #: well-formed pairs among the first 200,000 rows
    malformed: int = 0                   #: rows that are not a FASTQ record in both files
    first_malformed: int = 0             #: row number of the first of those (0: none)
    length_mismatches: int = 0           #: rows whose sequence and quality lengths differ
    first_length_mismatch: int = 0
    first_bad_pair: int = 0              #: the script's "first at pair": first of either kind
    short_read1: int = 0                 #: rows the first file lacks, as the script counts them
    short_read2: int = 0                 #: rows the second file lacks, as the script counts them
    blank_rows_read1: int = 0            #: among those rows: rows of the first file that are only empty lines
    blank_rows_read2: int = 0            #: the same for the second file
    rows_read1: int = 0                  #: rows of the first file (a last incomplete one counts)
    rows_read2: int = 0
    name_mismatches: int = 0
    first_mismatch: str = ""             #: name in the first file of the first pair that differs
    quality_codes: Tuple[int, int, int, int] = (999, 0, 999, 0)   #: lowest, highest; per file


@dataclass(frozen=True)
class ReadCheck:
    """The result of :func:`check_reads`."""
    header: str                          #: ``TesserACT 1.6.2 preflight: R1 + R2``
    lines: List[CheckLine]
    fails: int
    warns: int
    stats: ReadStats
    preflight_path: str = ""             #: where the text was written ("" if it was not)

    @property
    def closing(self) -> str:
        return "preflight: %d failed, %d warnings" % (self.fails, self.warns)

    @property
    def text(self) -> str:
        """The whole of ``preflight.txt`` (also what ``tesseract check`` prints)."""
        return "".join(line + "\n" for line in [self.header] + [c.text for c in self.lines] + [self.closing])

    @property
    def data(self) -> bytes:
        """``text`` as the bytes of the file."""
        return encode_text(self.text)

    @property
    def ok(self) -> bool:
        """No check failed (warnings do not count)."""
        return self.fails == 0

    @property
    def has_statistics(self) -> bool:
        """The statistics lines were printed: the pass ran and found at least one row."""
        return self.stats.ran and self.stats.pairs > 0

    @property
    def retry_genome_size(self) -> Optional[str]:
        """The genome size the thin-data retry rule may use, or None.

        The script takes that size from its depth check, so it exists only when the
        statistics lines were printed. Without it the retry rule assumes 5 Mb, whatever
        genome size the user gave (audit, P26).
        """
        return self.stats.genome_size_mb if self.has_statistics else None


@dataclass(frozen=True)
class InputProblem:
    """One reason a raw pair cannot be trimmed, in plain words."""
    code: str            #: for the program, never shown: unreadable, empty, damaged, same_file,
    #:                     no_reads, not_fastq, length_mismatch, fewer_reads, blank_lines, line_ends,
    #:                     names_differ
    message: str         #: what happened and what it means for the data
    next_step: str       #: what to do about it
    file: str = ""       #: the read file it is about ("" when it is about the pair)


@dataclass(frozen=True)
class InputCheck:
    """The result of :func:`check_input_pair`."""
    problems: List[InputProblem]
    stats: ReadStats

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def message(self) -> str:
        """The first problem, ready to show ("" when there is none)."""
        return self.problems[0].message if self.problems else ""

    @property
    def next_step(self) -> str:
        return self.problems[0].next_step if self.problems else ""


# --------------------------------------------------------------------------- reading

class _Halted(Exception):
    pass


class _Source(threading.Thread):
    """Reads one file in its own thread and hands over what it holds, decompressed.

    ``blocks`` receives ``(data, bytes of the file read when it was made)`` and then None.
    ``complete`` is the verdict of ``gzip -t`` (True for a file that is not gzip).
    ``consumed`` counts the bytes of the file read so far and ``taken`` those behind the
    blocks the checks have received. With ``discard`` set the data is checked but not
    handed over.
    """

    def __init__(self, path: str, gz: bool, halt: threading.Event, discard: bool = False):
        super().__init__(name="readcheck-reader", daemon=True)
        self.path = path
        self.gz = gz
        self.halt = halt
        self.discard = discard
        self.blocks: "queue.Queue[Optional[Tuple[bytes, int]]]" = queue.Queue(maxsize=_QUEUE_BLOCKS)
        self.complete = True
        self.consumed = 0
        self.taken = 0
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            with open(self.path, "rb") as handle:
                if self.gz:
                    self.complete = self._inflate(handle)
                else:
                    self._copy(handle)
        except _Halted:
            return
        except BaseException as exc:     # handed to the thread that started the check
            self.error = exc
        try:
            self._put(None)
        except _Halted:
            pass

    def _put(self, block: Optional[Tuple[bytes, int]]) -> None:
        while not self.halt.is_set():
            try:
                self.blocks.put(block, timeout=_POLL)
                return
            except queue.Full:
                continue
        raise _Halted()

    def _emit(self, block: bytes) -> None:
        if self.halt.is_set():
            raise _Halted()
        if block and not self.discard:
            self._put((block, self.consumed))

    def _copy(self, handle) -> None:
        while True:
            block = handle.read(_PLAIN_BLOCK)
            if not block:
                return
            self.consumed += len(block)
            self._emit(block)

    def _inflate(self, handle) -> bool:
        """Decompress every member. True when ``gzip -t`` would be satisfied.

        After a member gzip looks at what follows: nothing, or only zero bytes, is fine;
        another gzip member is read in turn; anything else is "trailing garbage", which
        makes ``gzip -t`` exit with 2 and so fails the script's check. (gzip would also read
        on through a member in one of the formats of the 1980s, compress, pack or lzh. No
        read file is made like that; here such a tail counts as garbage.)
        """
        inflater = zlib.decompressobj(31)        # gzip container: header, CRC and length are checked
        pending = b""
        between = False                          # the last member ended; what follows decides
        zeros = False                            # only zero bytes may follow now
        while True:
            if not pending:
                pending = handle.read(_IN_BLOCK)
                if not pending:
                    return between               # in the middle of a member: truncated
                self.consumed += len(pending)
                if self.halt.is_set():
                    raise _Halted()
            if zeros:
                if pending.count(0) != len(pending):
                    return False
                pending = b""
                continue
            if between:
                if pending[0] == 0:
                    zeros = True
                    continue
                if len(pending) < 2:
                    extra = handle.read(_IN_BLOCK)
                    if not extra:
                        return False             # one stray byte: gzip reports an unexpected end
                    self.consumed += len(extra)
                    pending += extra
                if pending[:2] != _GZIP_MAGIC:
                    return False
                inflater = zlib.decompressobj(31)
                between = False
            try:
                block = inflater.decompress(pending, _OUT_BLOCK)
            except zlib.error:
                return False
            if inflater.eof:
                pending = inflater.unused_data
                between = True
            else:
                # Not empty when the output limit was reached. Output that zlib still holds
                # with no input left comes out with the next input; a member cannot end
                # that way, because its trailer is read only after its last output.
                pending = inflater.unconsumed_tail
            self._emit(block)


def gzip_complete(path: PathLike) -> bool:
    """Whether ``gzip -t`` would accept the file (which must start with the gzip magic)."""
    halt = threading.Event()
    source = _Source(os.fspath(path), True, halt, discard=True)
    source.run()
    if source.error is not None:
        raise source.error
    return source.complete


class _Side:
    """The lines of one file that are waiting to be paired."""
    __slots__ = ("source", "lines", "tail", "tabs", "tail_tab", "ended", "rows")

    def __init__(self, source: _Source):
        self.source = source
        self.lines: List[bytes] = []     # complete lines not yet examined
        self.tail: List[bytes] = []      # pieces of the line that has not ended yet
        self.tabs = False                # a waiting line may hold a TAB
        self.tail_tab = False
        self.ended = False
        self.rows = 0                    # rows handed to the checks so far

    def add(self, block: bytes) -> None:
        has_tab = b"\t" in block
        if b"\n" not in block:           # a very long line: keep the pieces, join once
            self.tail.append(block)
            if has_tab:
                self.tail_tab = True
            return
        parts = block.split(b"\n")
        if self.tail:
            self.tail.append(parts[0])
            parts[0] = b"".join(self.tail)
        last = parts.pop()
        if has_tab or self.tail_tab:
            self.tabs = True
        self.tail = [last] if last else []
        self.tail_tab = has_tab and b"\t" in last
        if self.lines:
            self.lines += parts
        else:
            self.lines = parts

    def finish(self) -> None:
        """End of the file: a last line without a line end is a line."""
        if self.tail:
            self.lines.append(b"".join(self.tail))
            self.tail = []
            if self.tail_tab:
                self.tabs = True
        self.ended = True

    def ready(self) -> int:
        """Rows that can be given now (the incomplete last row counts once the file has ended)."""
        count = len(self.lines)
        return (count + 3) >> 2 if self.ended else count >> 2

    def drop(self, count: int) -> None:
        if count >= len(self.lines):
            self.lines = []
            self.tabs = False
        else:
            del self.lines[:count]
            if self.tabs:
                self.tabs = any(b"\t" in line for line in self.lines)

    def take(self) -> bytes:
        """The next row as ``paste - - - -`` writes it: four lines joined by TAB, padded."""
        row = self.lines[:4]
        self.drop(4)
        self.rows += 1
        if len(row) < 4:
            row += [b""] * (4 - len(row))
        return b"\t".join(row)

    def take_all(self) -> List[bytes]:
        """Every row that is ready, as :meth:`take` gives them."""
        if self.ended:
            lines, self.lines, self.tabs = self.lines, [], False
        else:                            # lines of a row that is not complete yet stay behind
            whole = len(self.lines) & ~3
            lines = self.lines[:whole]
            self.drop(whole)
        rows = []
        for start in range(0, len(lines), 4):
            row = lines[start:start + 4]
            if len(row) < 4:
                row += [b""] * (4 - len(row))
            rows.append(b"\t".join(row))
        self.rows += len(rows)
        return rows


class _Counts:
    """The variables of the script's awk program, plus a few the screen messages need."""
    __slots__ = ("n", "bad", "badlen", "badrec", "mism", "firstm", "bases", "maxlen", "s", "sumlen",
                 "adapt", "polyg", "nn", "short1", "short2", "qmin", "qmax", "qmin2", "qmax2",
                 "first_bad", "first_badlen", "blank1", "blank2")

    def __init__(self) -> None:
        self.n = self.bad = self.badlen = self.badrec = self.mism = 0
        self.bases = self.maxlen = self.s = self.sumlen = 0
        self.adapt = self.polyg = self.nn = self.short1 = self.short2 = 0
        self.qmin, self.qmax, self.qmin2, self.qmax2 = 999, 0, 999, 0
        self.firstm: Optional[bytes] = None
        self.first_bad = self.first_badlen = self.blank1 = self.blank2 = 0


_BLANK_ROW = b"\t\t\t"
_SUFFIXES = (b"/1", b"/2")
_item0 = itemgetter(0)


def _name(header: bytes) -> bytes:
    """A read name as the script compares it: no ``@``, cut at the first blank, no ``/1`` ``/2``.

    The header is one TAB-separated field here, so the first blank can only be a space.
    """
    name = header[1:] if header[:1] == b"@" else header
    cut = name.find(b" ")
    if cut >= 0:
        name = name[:cut]
    if name[-2:] in _SUFFIXES:
        name = name[:-2]
    return name


def _row(c: _Counts, a: bytes, b: bytes) -> None:
    """One row, exactly as the awk program of the script treats it.

    ``a`` and ``b`` are the rows of the two files as ``paste - - - -`` writes them, or
    ``b""`` for a file that has run out. They are joined by a TAB and split at every TAB,
    which is what awk sees: a TAB inside a line moves all later fields.
    """
    f = (a + b"\t" + b).split(b"\t")
    nf = len(f)
    if nf < 8:
        f += [b""] * (8 - nf)
    c.n += 1
    if nf == 4 or (nf > 4 and f[4] == b"" and f[5] == b""):
        c.short2 += 1
        if a == _BLANK_ROW:
            c.blank1 += 1
        if b == _BLANK_ROW:
            c.blank2 += 1
        return
    if f[0] == b"" and nf <= 5:
        c.short1 += 1
        return
    if (nf < 8 or f[0][:1] != b"@" or f[4][:1] != b"@" or f[2][:1] != b"+" or f[6][:1] != b"+"):
        c.bad += 1
        if not c.badrec:
            c.badrec = c.n
        if not c.first_bad:
            c.first_bad = c.n
        return
    s1, q1, s2, q2 = f[1], f[3], f[5], f[7]
    l1, l2 = len(s1), len(s2)
    if l1 != len(q1) or l2 != len(q2):
        c.badlen += 1
        if not c.badrec:
            c.badrec = c.n
        if not c.first_badlen:
            c.first_badlen = c.n
    name1 = _name(f[0])
    if name1 != _name(f[4]):
        c.mism += 1
        if c.firstm is None:
            c.firstm = name1             # the script builds "A vs B" and then prints only A
    c.bases += l1 + l2
    if l1 > c.maxlen:
        c.maxlen = l1
    if l2 > c.maxlen:
        c.maxlen = l2
    if c.n <= SAMPLE_ROWS:
        c.s += 1
        c.sumlen += l1 + l2
        if ADAPTER in s1 or ADAPTER in s2:
            c.adapt += 1
        if s1.endswith(POLYG_TAIL) or s2.endswith(POLYG_TAIL):
            c.polyg += 1
        sample = q1[::QUALITY_STEP]
        if sample:
            codes = sample.translate(_QUALITY_CODE)
            c.qmin = min(c.qmin, min(codes))
            c.qmax = max(c.qmax, max(codes))
        sample = q2[::QUALITY_STEP]
        if sample:
            codes = sample.translate(_QUALITY_CODE)
            c.qmin2 = min(c.qmin2, min(codes))
            c.qmax2 = max(c.qmax2, max(codes))
        c.nn += s1.count(b"N") + s2.count(b"N")


def _rows(c: _Counts, a: List[bytes], b: List[bytes], k: int) -> None:
    """``k`` complete rows of each file, one at a time."""
    for start in range(0, 4 * k, 4):
        _row(c, b"\t".join(a[start:start + 4]), b"\t".join(b[start:start + 4]))


def _all_start(lines: List[bytes], first: bytes) -> bool:
    """Whether every line starts with the byte ``first``.

    Bytes compare like words in a dictionary, so if the smallest and the largest line both
    start with that byte, every line between them does too. Two passes in C instead of a
    Python loop over every line.
    """
    return min(lines)[:1] == first and max(lines)[:1] == first


def _block(c: _Counts, a: List[bytes], b: List[bytes], k: int) -> None:
    """``k`` complete rows of each file that hold no TAB: the same result as :func:`_rows`.

    Without a TAB the eight fields of a row are its eight lines. If every header starts
    with ``@`` and every third line with ``+``, no row is short or malformed and each
    remaining rule can be applied to the whole block at once.
    """
    h1, p1, h2, p2 = a[0::4], a[2::4], b[0::4], b[2::4]
    if not ((p1.count(b"+") == k or _all_start(p1, b"+")) and (p2.count(b"+") == k or _all_start(p2, b"+"))
            and _all_start(h1, b"@") and _all_start(h2, b"@")):
        _rows(c, a, b, k)
        return
    s1, q1, s2, q2 = a[1::4], a[3::4], b[1::4], b[3::4]
    len1, len2 = list(map(len, s1)), list(map(len, s2))
    first = c.n                                   # row number of the row before this block
    qlen1, qlen2 = list(map(len, q1)), list(map(len, q2))
    if len1 != qlen1 or len2 != qlen2:
        for i in range(k):
            if len1[i] != qlen1[i] or len2[i] != qlen2[i]:
                c.badlen += 1
                if not c.badrec:
                    c.badrec = first + i + 1
                if not c.first_badlen:
                    c.first_badlen = first + i + 1
    if h1 != h2:
        # Names up to the first space. Both keep their "@", which changes nothing.
        cut1 = list(map(_item0, map(bytes.partition, h1, repeat(b" "))))
        cut2 = list(map(_item0, map(bytes.partition, h2, repeat(b" "))))
        if cut1 != cut2:
            for i in range(k):
                x, y = cut1[i], cut2[i]
                if x != y:
                    if x[-2:] in _SUFFIXES:
                        x = x[:-2]
                    if y[-2:] in _SUFFIXES:
                        y = y[:-2]
                    if x != y:
                        c.mism += 1
                        if c.firstm is None:
                            c.firstm = x[1:]
    c.bases += sum(len1) + sum(len2)
    longest = max(max(len1), max(len2))
    if longest > c.maxlen:
        c.maxlen = longest
    room = SAMPLE_ROWS - first
    if room > 0:
        m = k if k <= room else room
        if m < k:
            s1, q1, s2, q2, len1, len2 = s1[:m], q1[:m], s2[:m], q2[:m], len1[:m], len2[:m]
        c.s += m
        c.sumlen += sum(len1) + sum(len2)
        adapt = polyg = 0
        for x, y in zip(s1, s2):
            if ADAPTER in x or ADAPTER in y:
                adapt += 1
            if x[-10:] == POLYG_TAIL or y[-10:] == POLYG_TAIL:
                polyg += 1
        c.adapt += adapt
        c.polyg += polyg
        codes = b"".join([q[::QUALITY_STEP] for q in q1]).translate(_QUALITY_CODE)
        if codes:
            c.qmin = min(c.qmin, min(codes))
            c.qmax = max(c.qmax, max(codes))
        codes = b"".join([q[::QUALITY_STEP] for q in q2]).translate(_QUALITY_CODE)
        if codes:
            c.qmin2 = min(c.qmin2, min(codes))
            c.qmax2 = max(c.qmax2, max(codes))
        c.nn += b"".join(s1).count(b"N") + b"".join(s2).count(b"N")
    c.n = first + k


def _pair(c: _Counts, one: _Side, two: _Side, fast: bool) -> None:
    """Examine every row that can be paired with what has been read so far."""
    while True:
        count1, count2 = len(one.lines), len(two.lines)
        k = min(count1, count2) >> 2
        if k:
            used = 4 * k
            a = one.lines if count1 == used else one.lines[:used]
            b = two.lines if count2 == used else two.lines[:used]
            if fast and not (one.tabs or two.tabs):
                _block(c, a, b, k)
            else:
                _rows(c, a, b, k)
            one.drop(used)
            two.drop(used)
            one.rows += k
            two.rows += k
            continue
        ready1, ready2 = one.ready(), two.ready()
        if ready1 and ready2:            # one of the two is the incomplete last row of its file
            _row(c, one.take(), two.take())
            continue
        if ready1 and two.ended:         # the second file has run out
            for row in one.take_all():
                _row(c, row, b"")
        elif ready2 and one.ended:       # the first file has run out
            for row in two.take_all():
                _row(c, b"", row)
        return


class _Watch:
    """Cancel requests and progress, looked at between blocks."""

    def __init__(self, sources: Sequence[_Source], total: int, progress: Progress,
                 cancel: Optional[threading.Event]):
        self.sources = sources
        self.total = total
        self.progress = progress
        self.cancel = cancel
        self.shown = -1.0

    def tick(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        if self.progress is None or self.total <= 0:
            return
        # What the checks have received counts, so that the figure moves with the work done;
        # a file that is only tested (nothing is handed over) counts as far as it was read.
        done = sum(s.consumed if s.discard else s.taken for s in self.sources)
        fraction = min(1.0, done / self.total)
        if fraction - self.shown >= 0.002:
            self.shown = fraction
            self.progress(min(fraction, 0.999))      # 1.0 is said once, at the very end

    def next_block(self, source: _Source) -> Optional[bytes]:
        while True:
            try:
                item = source.blocks.get(timeout=_POLL)
            except queue.Empty:
                self.tick()
                continue
            if item is None:
                source.taken = source.consumed
                return None
            source.taken = item[1]
            return item[0]


def _scan(paths: Sequence[str], gzipped: Sequence[bool], total: int, *, statistics: bool,
          progress: Progress, cancel: Optional[threading.Event], fast: bool = True,
          ) -> Tuple[List[bool], Optional[_Counts], Tuple[int, int]]:
    """Read the files to their end.

    ``paths`` holds one or two files. Returns, per file, whether it is complete (``gzip
    -t``); the counts, or None when ``statistics`` is False or a gzip file turned out to be
    damaged; and the number of rows of each file.

    Each file is read by its own thread, a few blocks ahead. This thread takes the next
    block from whichever file has fewer lines waiting, so the two stay level and memory
    stays small however large the files are.
    """
    halt = threading.Event()
    sources = [_Source(path, gz, halt, discard=not statistics) for path, gz in zip(paths, gzipped)]
    watch = _Watch(sources, total, progress, cancel)
    counts: Optional[_Counts] = _Counts() if statistics else None
    sides = [_Side(source) for source in sources]
    try:
        watch.tick()
        for source in sources:
            source.start()
        while not all(side.ended for side in sides):
            watch.tick()
            live = [side for side in sides if not side.ended]
            side = min(live, key=lambda s: len(s.lines))     # keep the two files level
            block = watch.next_block(side.source)
            if block is None:
                if side.source.error is not None:
                    raise side.source.error
                side.finish()
                if not side.source.complete and counts is not None:
                    counts = None                            # a damaged file: no statistics
                    for other in sides:
                        other.source.discard = True
                        other.lines, other.tail = [], []
            elif counts is not None:
                side.add(block)
            else:
                continue
            if counts is not None:
                _pair(counts, sides[0], sides[1], fast)
        watch.tick()
    finally:
        halt.set()
        for source in sources:
            if source.is_alive():
                source.join()
    rows = (sides[0].rows, sides[1].rows) if len(sides) == 2 else (0, 0)
    return [source.complete for source in sources], counts, rows


# --------------------------------------------------------------------------- the file checks

@dataclass
class _File:
    path: str
    shown: str
    state: str           # "unreadable", "empty", "gzip" or "plain"
    size: int = 0
    complete: bool = True


def _probe(path: str, shown: str) -> _File:
    """Rules 1, 2 and the magic bytes of section 4.1."""
    try:
        info = os.stat(path)
        if stat.S_ISDIR(info.st_mode):
            return _File(path, shown, "unreadable")          # departure F7
        with open(path, "rb") as handle:
            magic = handle.read(2)
    except (OSError, ValueError):
        return _File(path, shown, "unreadable")
    if info.st_size == 0:
        return _File(path, shown, "empty")
    return _File(path, shown, "gzip" if magic == _GZIP_MAGIC else "plain", info.st_size)


def _same_file(path1: str, path2: str) -> bool:
    try:
        return os.path.samefile(path1, path2)
    except (OSError, ValueError):
        return False


def _examine(read1: str, read2: str, shown1: str, shown2: str, progress: Progress,
             cancel: Optional[threading.Event], fast: bool = True,
             ) -> Tuple[_File, _File, bool, Optional[_Counts], Tuple[int, int]]:
    """Everything that needs the files: the per-file checks, same file, and the statistics."""
    one, two = _probe(read1, shown1), _probe(read2, shown2)
    same = _same_file(read1, read2)
    usable = [f for f in (one, two) if f.state in ("gzip", "plain")]
    counts: Optional[_Counts] = None
    rows = (0, 0)
    if len(usable) == 2 and not same:
        complete, counts, rows = _scan([one.path, two.path], [one.state == "gzip", two.state == "gzip"],
                                       one.size + two.size, statistics=True, progress=progress,
                                       cancel=cancel, fast=fast)
        one.complete, two.complete = complete
    else:
        # A check has failed already, so the script reads nothing more. What is left is
        # gzip -t on each gzip file, for its own line.
        todo = [f for f in usable if f.state == "gzip"]
        if same and len(todo) == 2:
            todo = todo[:1]
        if todo:
            complete, _, _ = _scan([f.path for f in todo], [True] * len(todo), sum(f.size for f in todo),
                                   statistics=False, progress=progress, cancel=cancel)
            for f, whole in zip(todo, complete):
                f.complete = whole
            if same and len(usable) == 2:
                two.complete = one.complete
        elif cancel is not None and cancel.is_set():
            raise Cancelled()
    return one, two, same, counts, rows


def _stats(counts: Optional[_Counts], rows: Tuple[int, int], genome: Optional[str]) -> ReadStats:
    gs = genome or DEFAULT_GENOME_MB
    if counts is None:
        return ReadStats(genome_size_mb=gs, genome_size_assumed=genome is None)
    c = counts
    depth: Optional[int] = None
    if c.n:
        value = c.bases / (float(gs) * 1e6)
        if not math.isfinite(value):
            raise ValueError("the genome size %s is too small to compute a depth" % gs)
        depth = int("%.0f" % value)
    return ReadStats(
        ran=True, pairs=c.n, bases=c.bases, max_length=c.maxlen,
        mean_length=float("%.1f" % (c.sumlen / (2 * c.s) if c.s else 0)),
        depth=depth, genome_size_mb=gs, genome_size_assumed=genome is None,
        adapter_fraction=float("%.4f" % (c.adapt / c.s if c.s else 0)),      # rounded BEFORE the limit
        polyg_fraction=float("%.4f" % (c.polyg / c.s if c.s else 0)),
        n_fraction=float("%.5f" % (c.nn / c.sumlen if c.sumlen else 0)),
        sampled_pairs=c.s, malformed=c.bad, first_malformed=c.first_bad,
        length_mismatches=c.badlen, first_length_mismatch=c.first_badlen, first_bad_pair=c.badrec,
        short_read1=c.short1, short_read2=c.short2, blank_rows_read1=c.blank1, blank_rows_read2=c.blank2,
        rows_read1=rows[0], rows_read2=rows[1], name_mismatches=c.mism,
        first_mismatch=_text(c.firstm) if c.firstm is not None else "",
        quality_codes=(c.qmin, c.qmax, c.qmin2, c.qmax2))


# --------------------------------------------------------------------------- check_reads

def check_reads(read1: PathLike, read2: PathLike, *, out_dir: Optional[PathLike] = None,
                genome_size_mb: Union[str, float, int, None] = None, wording: str = "windows",
                progress: Progress = None, cancel: Optional[threading.Event] = None,
                free_bytes: Optional[int] = None, available_memory_bytes: Optional[int] = None,
                shown_read1: Optional[str] = None, shown_read2: Optional[str] = None,
                shown_out: Optional[str] = None, _fast: bool = True) -> ReadCheck:
    """The preflight of ``tesseract`` on one pair of read files.

    ``out_dir``
        None: the read checks only, as ``tesseract check`` (no disk or memory line, nothing
        written). A folder: also the disk and memory lines of a run, and the text is
        written to ``out_dir/preflight.txt`` (the folder is created if needed).
    ``genome_size_mb``
        What the user gave, or None. Text is printed as typed. See :func:`genome_size_text`.
    ``wording``
        ``"upstream"``: the script, byte for byte. ``"windows"``: the same lines, except
        that advice naming Linux tools points to the Trimming page.
    ``progress``
        Called in the calling thread with the fraction (0..1) of the input worked through
        so far, counted in bytes of the files as they are on disk, and with 1.0 at the end.
    ``cancel``
        A ``threading.Event``; once set, :class:`Cancelled` is raised within a moment.
    ``free_bytes``
        Free space for ``out_dir``. None: asked from the system (``fsutil.free_bytes``); if
        even that is unknown the line is ``FAIL disk only 0 GB free``, as the script prints
        when ``df`` gives nothing.
    ``available_memory_bytes``
        Free memory. None or 0: no memory line, as the script on a system that cannot tell.
    ``shown_read1``, ``shown_read2``, ``shown_out``
        The paths to print, when they differ from the paths to open (the script prints them
        "as typed"; a caller that opens absolute paths can still print the short ones).

    Raises ``ValueError`` for a bad genome size or wording, :class:`Cancelled`, and
    ``OSError`` when a file fails while it is being read.
    """
    if wording not in WORDINGS:
        raise ValueError("wording must be one of %s" % ", ".join(WORDINGS))
    advice = _ADVICE[wording]
    genome = genome_size_text(genome_size_mb)
    path1, path2 = os.fspath(read1), os.fspath(read2)
    shown1 = path1 if shown_read1 is None else shown_read1
    shown2 = path2 if shown_read2 is None else shown_read2
    lines: List[CheckLine] = []

    def pf(verdict: str, name: str, message: str) -> None:
        lines.append(CheckLine(verdict, name, message))

    one, two, same, counts, rows = _examine(path1, path2, shown1, shown2, progress, cancel, _fast)
    for f in (one, two):
        if f.state == "unreadable":
            pf("FAIL", "file", "cannot read '%s'" % f.shown)
        elif f.state == "empty":
            pf("FAIL", "file", "'%s' is empty" % f.shown)
        elif f.state == "gzip":
            if f.complete:
                pf("OK", "gzip", "'%s' is complete" % os.path.basename(f.shown))
            else:
                pf("FAIL", "gzip", "'%s' is truncated or corrupt (gzip -t failed)" % f.shown)
    if same:
        pf("FAIL", "pairing", "-1 and -2 are the same file")

    # After any failure above there are no counts: the statistics pass is skipped entirely (P21).
    st = _stats(counts, rows, genome)
    if counts is not None:
        _statistics_lines(pf, counts, st, advice)

    preflight_path = ""
    if out_dir is not None:
        folder = os.fspath(out_dir)
        if one.state != "unreadable" and two.state != "unreadable":      # P23
            _machine_lines(pf, folder, folder if shown_out is None else shown_out, one, two,
                           free_bytes, available_memory_bytes)
        preflight_path = os.path.join(folder, "preflight.txt")

    result = ReadCheck(
        header="TesserACT %s preflight: %s + %s" % (TESSERACT_VERSION, shown1, shown2),
        lines=lines, fails=sum(1 for line in lines if line.verdict == "FAIL"),
        warns=sum(1 for line in lines if line.verdict == "WARN"), stats=st, preflight_path=preflight_path)
    if cancel is not None and cancel.is_set():
        raise Cancelled()                # nothing is written for a check that was stopped
    if preflight_path:
        write_text_exact(preflight_path, result.text)
    if progress is not None:
        progress(1.0)
    return result


def _statistics_lines(pf: Callable[[str, str, str], None], c: _Counts, st: ReadStats, advice: dict) -> None:
    """Section 4.4: the lines printed from the statistics, in the script's order."""
    if c.n == 0:
        pf("FAIL", "format", "no FASTQ records read")
        return
    if c.bad == 0:
        pf("OK", "format", "%d read pairs, every record well-formed" % c.n)
    else:
        pf("FAIL", "format", "%d malformed records (first at pair %d); is this FASTQ?" % (c.bad, c.badrec))
    if c.badlen:
        pf("FAIL", "format", "%d records whose sequence and quality lengths differ" % c.badlen)
    if c.short1:
        pf("FAIL", "pairing", "the -1 file has %d fewer reads than the -2 file (truncated, or not the mate file)" % c.short1)
    if c.short2:
        pf("FAIL", "pairing", "the -2 file has %d fewer reads than the -1 file (truncated, or not the mate file)" % c.short2)
    if c.mism == 0:
        pf("OK", "pairing", "mate names agree on every pair")
    else:
        pf("FAIL", "pairing", "%d pairs whose mate names differ (first: %s); the files are not in the same order, "
                              "or one is truncated" % (c.mism, st.first_mismatch))
    quality_ok = True
    for side, low, high in (("-1", c.qmin, c.qmax), ("-2", c.qmin2, c.qmax2)):
        if low < QUALITY_LOW:
            pf("FAIL", "quality", "%s file: quality characters below '!' (code %d): not FASTQ Phred+33" % (side, low))
            quality_ok = False
        elif low >= PHRED64_MIN and high > PHRED64_MAX:
            pf("FAIL", "quality", "%s file: qualities look Phred+64 (codes %d-%d); %s" % (side, low, high, advice["phred64"]))
            quality_ok = False
    if quality_ok:
        pf("OK", "quality", "Phred+33 (codes %d-%d / %d-%d)" % (c.qmin, c.qmax, c.qmin2, c.qmax2))
    pf("OK", "read length", "max %d bp, mean %s bp (%s)" % (
        c.maxlen, "%.1f" % (c.sumlen / (2 * c.s) if c.s else 0),
        "long-read MiSeq-type library: 2x250/2x300" if c.maxlen >= LONG_READ_LENGTH
        else "short-read library: 2x150 or shorter"))
    depth = st.depth if st.depth is not None else 0
    note = "for a %s Mb genome%s" % (
        st.genome_size_mb, " (assumed; set --genome-size for a better estimate)" if st.genome_size_assumed else "")
    if depth < DEPTH_THIN:
        pf("WARN", "depth", "about %dx %s: too thin for a reliable assembly; expect many breaks" % (depth, note))
    elif depth < DEPTH_LOW:
        pf("WARN", "depth", "about %dx %s: low; dropouts are likely" % (depth, note))
    elif depth > DEPTH_DEEP:
        pf("WARN", "depth", "about %dx %s: very deep; runtime and memory grow with depth, results do not improve "
                            "beyond ~150x" % (depth, note))
    else:
        pf("OK", "depth", "about %dx %s" % (depth, note))
    # The limits are applied to the rounded fractions (P15).
    if st.adapter_fraction > ADAPTER_LIMIT:
        pf("WARN", "adapters", "%.1f%% of read pairs carry Illumina adapter sequence: %s"
           % (st.adapter_fraction * 100, advice["adapters"]))
    else:
        pf("OK", "adapters", "%.2f%% of read pairs with adapter read-through" % (st.adapter_fraction * 100))
    if st.polyg_fraction > POLYG_LIMIT:
        pf("WARN", "poly-G", "%.1f%% of read pairs end in a poly-G run (two-colour chemistry): %s"
           % (st.polyg_fraction * 100, advice["polyg"]))
    else:
        pf("OK", "poly-G", "%.2f%% of read pairs end in a poly-G run" % (st.polyg_fraction * 100))
    if st.n_fraction > N_LIMIT:
        pf("WARN", "N bases", "%.1f%% of bases are N" % (st.n_fraction * 100))


def _machine_lines(pf: Callable[[str, str, str], None], folder: str, shown: str, one: _File, two: _File,
                   free_bytes: Optional[int], memory_bytes: Optional[int]) -> None:
    """Section 4.5: free disk space and free memory, in units of 1024 bytes as the script."""
    if free_bytes is None:
        free_bytes = fsutil.free_bytes(folder)
    avail_kb = int(free_bytes) // 1024 if free_bytes is not None else 0
    need_kb = (one.size + two.size) * 3 // 1024 + DISK_BASE_KB
    if avail_kb >= need_kb:
        pf("OK", "disk", "%d GB free in %s" % (avail_kb // 1048576, shown))
    else:
        pf("FAIL", "disk", "only %d GB free in %s; need about %d GB" % (avail_kb // 1048576, shown, need_kb // 1048576))
    mem_kb = int(memory_bytes) // 1024 if memory_bytes else 0
    if mem_kb > 0:
        if mem_kb >= MEMORY_OK_KB:
            pf("OK", "memory", "%d GB available" % (mem_kb // 1048576))
        else:
            pf("WARN", "memory", "only %d GB available; a bacterial assembly needs 2-6 GB" % (mem_kb // 1048576))


# --------------------------------------------------------------------------- check_input_pair

def _plural(count: int, one: str, many: str) -> str:
    return "%d %s" % (count, one if count == 1 else many)


def _shown_name(name: str) -> str:
    """A read name for the screen: no control characters, not too long."""
    clean = "".join(ch if ch.isprintable() else "?" for ch in name.encode("utf-8", "replace").decode("utf-8", "replace"))
    return clean if len(clean) <= 60 else clean[:57] + "..."


def check_input_pair(read1: PathLike, read2: PathLike, *, progress: Progress = None,
                     cancel: Optional[threading.Event] = None) -> InputCheck:
    """Whether a raw pair can be given to the trimmer.

    The trimmer ends without an error on files that are cut short, do not belong together
    or are not FASTQ, so these are checked first: both files can be opened and are not
    empty, compressed files are complete, every record is FASTQ, both files hold the same
    number of reads, and the names of the two reads of every pair agree. It is the same
    pass as :func:`check_reads` and decides exactly as its ``file``, ``gzip``, ``format``
    and ``pairing`` checks do; only the words are different. Quality, depth, adapters and
    the PC are not looked at here: they are checked on the reads that will be assembled.

    ``progress`` and ``cancel`` work as in :func:`check_reads`.
    """
    path1, path2 = os.fspath(read1), os.fspath(read2)
    name1, name2 = os.path.basename(path1) or path1, os.path.basename(path2) or path2
    one, two, same, counts, rows = _examine(path1, path2, name1, name2, progress, cancel)
    problems: List[InputProblem] = []
    again = "Copy or download the file again, then run this sample again."
    for f in (one, two):
        if f.state == "unreadable":
            problems.append(InputProblem(
                "unreadable", 'The file "%s" could not be opened, so this sample cannot be processed.' % f.shown,
                "Check that the file still exists and that its drive or network folder is connected, "
                "then run this sample again.", f.path))
        elif f.state == "empty":
            problems.append(InputProblem(
                "empty", 'The file "%s" is empty (0 bytes): it holds no reads.' % f.shown, again, f.path))
        elif f.state == "gzip" and not f.complete:
            problems.append(InputProblem(
                "damaged", 'The file "%s" is incomplete or damaged: it could not be unpacked to its end, '
                           "so part of the reads is missing. This usually happens when a copy or a download "
                           "is interrupted." % f.shown, again, f.path))
    if same:
        problems.append(InputProblem(
            "same_file", "The same file was given for read 1 and for read 2. A sample needs its two "
                         "different read files.", "Choose the two files that belong to this sample."))
    st = _stats(counts, rows, None)
    if counts is not None:               # never after one of the problems above
        c = counts
        both = "Check that these are the two FASTQ files of this sample. If they are, copy or download them again."
        if c.n == 0:
            problems.append(InputProblem(
                "no_reads", 'No reads were found in "%s" and "%s".' % (name1, name2), both))
        if c.bad:
            problems.append(InputProblem(
                "not_fastq", "%s not in FASTQ format (the first is pair number %d). The files are damaged, "
                             "or they are not read files." % (
                                 _plural(c.bad, "read pair is", "read pairs are"), c.first_bad), both))
        if c.badlen:
            problems.append(InputProblem(
                "length_mismatch", "%s a different number of bases and of quality values (the first is pair "
                                   "number %d). The files are damaged." % (
                                       _plural(c.badlen, "read pair has", "read pairs have"), c.first_badlen),
                "Copy or download the files again, then run this sample again."))
        if c.short1 or c.short2:
            # The script's two counts decide that there is a problem. What is said about it
            # comes from the rows each file really holds, empty rows left out.
            difference = (rows[0] - c.blank1) - (rows[1] - c.blank2)
            if difference:
                fewer, more = (name2, name1) if difference > 0 else (name1, name2)
                problems.append(InputProblem(
                    "fewer_reads", '"%s" holds %s than "%s". The two files of a sample must hold the same reads: '
                                   "one of them is incomplete, or they do not belong together."
                    % (fewer, _plural(abs(difference), "read fewer", "reads fewer"), more), both))
            elif not (c.blank1 or c.blank2):
                problems.append(InputProblem(
                    "fewer_reads", "%s incomplete in one of the two files. The files are damaged, or they do not "
                                   "belong together." % _plural(c.short1 + c.short2, "read is", "reads are"), both))
            for name, path, count in ((name1, path1, c.blank1), (name2, path2, c.blank2)):
                if count:
                    problems.append(InputProblem(
                        "blank_lines", 'The file "%s" has empty lines where a read should be (often at the very '
                                       "end of the file), so the two files no longer line up." % name,
                        "Remove the empty lines from the file, or copy or download it again.", path))
        if c.mism and st.first_mismatch.endswith("\r"):
            problems.append(InputProblem(
                "line_ends", "The read files have Windows line ends, which read files must not have (they may have "
                             "been opened and saved in a text editor). Because of them %s cannot be matched."
                % _plural(c.mism, "read pair", "read pairs"),
                "Use the files as they came from the sequencer, or copy or download them again."))
        elif c.mism:
            problems.append(InputProblem(
                "names_differ", '%s different names in the two files (the first is "%s"). The files are not in '
                                "the same order, or they do not belong to the same sample." % (
                                    _plural(c.mism, "read pair has", "read pairs have"),
                                    _shown_name(st.first_mismatch)), both))
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if progress is not None:
        progress(1.0)
    return InputCheck(problems=problems, stats=st)
