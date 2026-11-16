# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""After the engine: the thin-data retry rule and the postflight of bash ``tesseract`` 1.6.2.

``thin_retry_needed`` decides, as the script does, whether an assembly that came out
almost empty is tried once more with the short k ladder; ``keep_first_attempt`` puts the
first attempt aside the way the script does. ``summarise`` checks the output folder and
writes ``SUMMARY.txt`` and ``assembly_stats.tsv`` exactly as the script writes them on a
machine without a read mapper. ``detected_organism`` reads the organism call out of
``report.json`` the way the script does.

The specification is ``docs/superpowers/audits/wrapper-port.md`` (sections 5 to 8 and the
quirk register in section 9). Engine output is read as bytes; ``report.json`` is searched
as text and never parsed, because that is what decides the script's verdicts.

``PROFILE_ENV`` is the one place that records the nine settings the wrapper gives the
engine through its environment, and ``DETECT_REPORT_ENV`` / ``DETECT_SKETCH_ENV`` name the
two more it adds when the detection sketch is there.

``wording="upstream"`` reproduces the script byte for byte. ``wording="windows"`` (the
default) is the Windows edition. It keeps every check name, verdict and number and
differs in these places only:

* the ``map-back`` line says that map-back is not included in the Windows edition and
  that the k-mer self-check above covers completeness (the second half only when that
  self-check line was printed);
* the organism hint is information only: ``the reads look like Escherichia coli``, the
  species name in full, with no word about models or commands;
* F4  a failed engine is reported as ``N (see OUT/assembly.log)``. The script names
  ``OUT.log``, a file that does not exist;
* F5  a CR at the end of a line of ``contigs.fasta`` or ``p2_self_qa.tsv`` is not data.
  The engines are built to write LF, so this changes nothing unless a build gets that
  wrong; then the script's rule would call every contig line malformed.

Deliberate departures in both wordings (audit section 9, "fix deliberately"):

* F2  the genome size must be a positive decimal number, otherwise ``ValueError``;
* F3  numbers are always printed with ``.``, whatever the locale.

Not ported, because the Windows edition has neither: organism models (``--organism``)
and the read map-back with ``bwa`` or ``minimap2``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple, Union

from . import fsutil
from .readcheck import (DEFAULT_GENOME_MB, WORDINGS, CheckLine, encode_text, genome_size_text,
                        write_text_exact)
from .versions import TESSERACT_VERSION

__all__ = [
    "PROFILE_ENV", "DETECT_REPORT_ENV", "DETECT_SKETCH_ENV", "detect_env",
    "ORGANISM_GENOME_MB", "ORGANISM_NAMES", "RETRY_KMERS", "RETRY_ARGS", "FIRST_ATTEMPT_DIR",
    "RetryDecision", "ContigStats", "RunFacts", "Postflight",
    "thin_retry_needed", "keep_first_attempt", "detected_organism", "contig_statistics",
    "engine_version_line", "summarise",
]

PathLike = Union[str, "os.PathLike[str]"]

# --------------------------------------------------------------------------- constants

#: The profile of the wrapper: what ``tesseract`` 1.6.2 sets in the engine's environment on
#: every run (verified circles, library check, PhiX label, end audit, lower-case tips,
#: k-mer self-check, provenance, and depth normalisation that recovers high-copy plasmids).
PROFILE_ENV: Tuple[Tuple[str, str], ...] = (
    ("TESSERACT_P2_LIBGUARD", "1"),
    ("TESSERACT_P2_CIRC", "verify"),
    ("TESSERACT_P2_SPIKEIN", "1"),
    ("TESSERACT_P2_ENDS", "1"),
    ("TESSERACT_P2_TIPS_LOWERCASE", "1"),
    ("TESSERACT_P2_SELF_QA", "1"),
    ("TESSERACT_P2_PROVENANCE", "1"),
    ("TESSERACT_DEEP_NORM", "30"),
    ("TESSERACT_DEEP_NORM_MATE", "both"),
)
#: Added only when the detection sketch (``om2detect.sketch``) was found.
DETECT_REPORT_ENV = "TESSERACT_P2_DETECT_REPORT"
DETECT_SKETCH_ENV = "TESSERACT_P2_DETECT_SKETCH"


def detect_env(sketch_path: str) -> Tuple[Tuple[str, str], ...]:
    """The two detection variables for a sketch that exists; nothing for ``""``."""
    if not sketch_path:
        return ()
    return ((DETECT_REPORT_ENV, "1"), (DETECT_SKETCH_ENV, sketch_path))


#: Organisms the engine can call, with the genome size (Mb, as text) the size check uses.
ORGANISM_GENOME_MB = {"kpneumoniae": "5.5", "ecoli": "5.1", "ecloacae": "5.0", "abaumannii": "4.0",
                      "paeruginosa": "6.6", "saureus": "2.8", "efaecium": "3.0", "senterica": "4.9"}
#: Their names in full, for the Windows wording of the hint and for the screen.
ORGANISM_NAMES = {"kpneumoniae": "Klebsiella pneumoniae", "ecoli": "Escherichia coli",
                  "ecloacae": "Enterobacter cloacae complex", "abaumannii": "Acinetobacter baumannii",
                  "paeruginosa": "Pseudomonas aeruginosa", "saureus": "Staphylococcus aureus",
                  "efaecium": "Enterococcus faecium", "senterica": "Salmonella enterica"}

RETRY_KMERS = "21,33,55"                 #: the short ladder of the second attempt
#: Arguments of the second attempt. They go right after ``-t N``, before any other argument.
RETRY_ARGS: Tuple[str, str] = ("-k", RETRY_KMERS)
FIRST_ATTEMPT_DIR = "attempt1_default_k"  #: where the first attempt is kept, inside the output folder
RETRY_FRACTION = 0.2                     #: retry when the contigs hold less than this share of the genome
RETRY_KMER_DEPTH = 2                     #: ... and the log reports a k-mer depth below this
SIZE_LOW, SIZE_HIGH = 0.8, 1.3           #: WARN size outside this ratio of the expected genome size

_SETTINGS_LINE = ("  settings standard (read-verified circles, library check, PhiX label, "
                  "small-plasmid recovery, self-check)")
_MAP_BACK = {
    "upstream": "skipped: install bwa or minimap2 to measure how many reads map back",
    "windows": "not included in the Windows edition",
}
_MAP_BACK_COVERED = "; the k-mer self-check above covers completeness"
_THIN_DATA = ("the default k ladder assembled almost nothing; this result uses k = 21,33,55 (the first "
              "attempt is in attempt1_default_k/). Expect a fragmented assembly: sequence more deeply if you can")
_VALID_BASES = re.compile(rb"[^ACGTNacgtn]")
_AWK_NUMBER = re.compile(rb"[ \t\n\r\f\v]*([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
_PLAIN_NUMBER = re.compile(rb"[0-9]+\.?[0-9]*|\.[0-9]+")


# --------------------------------------------------------------------------- small helpers

def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _non_empty(path: str) -> bool:
    """``[ -s path ]``."""
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _awk_number(text: bytes) -> float:
    """A text as awk reads a number: the longest numeric start, 0 when there is none."""
    match = _AWK_NUMBER.match(text)
    return float(match.group(1)) if match else 0.0


def engine_version_line(output: Union[str, bytes]) -> str:
    """Of what ``tesseract-asm --version`` printed, the first line that holds ``TesserACT``.

    That is the text :func:`summarise` wants as ``engine_version``. ``""`` when there is none.
    """
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    for line in text.split("\n"):
        if "TesserACT" in line:
            return line.rstrip("\r")
    return ""


# --------------------------------------------------------------------------- thin-data retry

@dataclass(frozen=True)
class RetryDecision:
    """What :func:`thin_retry_needed` found. Truthy when the second attempt is due."""
    needed: bool
    assembled_bp: int            #: bases in ``contigs.fasta`` (0 when it is missing)
    wanted_bp: int               #: a fifth of the expected genome
    kmer_depth: Optional[str]    #: the last ``k-mer depth`` of ``assembly.log``, as text; None: not there
    message: str                 #: the line the script prints before it retries ("" when not needed)

    def __bool__(self) -> bool:
        return self.needed


def _assembled_bases(path: str) -> int:
    """``grep -v '^>' contigs.fasta | tr -d '\\n' | wc -c``."""
    data = _read(path)
    if not data:
        return 0
    return sum(len(line) for line in data.split(b"\n") if not line.startswith(b">"))


def _kmer_depth(path: str) -> Optional[str]:
    """The last ``k-mer depth  N.Nx`` of the log; None when there is none or it has no digits."""
    data = _read(path)
    if not data:
        return None
    found = re.findall(rb"k-mer depth *([0-9.]*)x", data)
    if not found or not found[-1]:
        return None
    return found[-1].decode("ascii")


def _below(text: str, limit: int) -> bool:
    """``awk -v d=TEXT 'BEGIN { exit !(d < LIMIT) }'`` for a text of digits and dots.

    awk compares as numbers when the text is a number and as text when it is not (``1.2.3``).
    """
    if _PLAIN_NUMBER.fullmatch(text.encode("ascii", "replace")):
        return float(text) < limit
    return text < str(limit)


def thin_retry_needed(out_dir: PathLike, *, exit_code: int = 0, engine_args: Sequence[str] = (),
                      genome_size_mb: Union[str, float, int, None] = None,
                      stats_ran: bool = True) -> RetryDecision:
    """Whether the assembly in ``out_dir`` must be tried again with ``-k 21,33,55``.

    The rule of the script, to be asked once, right after the first engine run:

    * the engine ended with status 0, and
    * the user did not choose the k-mer sizes (no argument that is exactly ``-k`` or
      ``--kmers``; ``-k21,33`` and ``--kmers=21,33`` do not count), and
    * ``contigs.fasta`` holds fewer bases than a fifth of the expected genome, and
    * the last ``k-mer depth`` line of ``assembly.log`` reports less than 2 (no such line
      counts as 99).

    ``genome_size_mb`` is what the user gave (None: 5 Mb). ``stats_ran`` says whether the
    read check printed its statistics lines (``ReadCheck.has_statistics``): when it did
    not, the script has no genome size at this point and uses 5 Mb whatever the user gave.
    ``engine_args`` are the extra arguments the engine was given.
    """
    folder = os.fspath(out_dir)
    genome = genome_size_text(genome_size_mb)
    size = (genome or DEFAULT_GENOME_MB) if stats_ran else DEFAULT_GENOME_MB
    wanted = int(float(size) * 1e6 * RETRY_FRACTION)
    got = _assembled_bases(os.path.join(folder, "contigs.fasta"))
    depth = _kmer_depth(os.path.join(folder, "assembly.log"))
    needed = (exit_code == 0 and not any(arg in ("-k", "--kmers") for arg in engine_args)
              and got < wanted and _below(depth if depth is not None else "99", RETRY_KMER_DEPTH))
    message = ("the assembly holds only %d bp: retrying with a short k ladder (%s) for thin data ..."
               % (got, RETRY_KMERS)) if needed else ""
    return RetryDecision(needed, got, wanted, depth, message)


def keep_first_attempt(out_dir: PathLike) -> str:
    """Move the first attempt into ``attempt1_default_k`` inside ``out_dir``; return that folder.

    As the script: everything in the output folder goes there except ``preflight.txt``,
    the folder itself and names that start with a dot.
    """
    folder = os.fspath(out_dir)
    keep = os.path.join(folder, FIRST_ATTEMPT_DIR)
    os.makedirs(keep, exist_ok=True)
    for name in sorted(os.listdir(folder)):
        if name in ("preflight.txt", FIRST_ATTEMPT_DIR) or name.startswith("."):
            continue
        source, target = os.path.join(folder, name), os.path.join(keep, name)
        if os.path.lexists(target):      # left by an earlier first attempt; mv would replace it too
            fsutil.remove_tree(target)
        fsutil.replace_file(source, target)
    return keep


# --------------------------------------------------------------------------- organism

def detected_organism(out_dir: PathLike) -> str:
    """The organism the engine called in ``out_dir/report.json``, or ``""``.

    One of the keys of :data:`ORGANISM_GENOME_MB`. As the script: newlines and spaces are
    taken out of the file, and the first ``"call":"<lower-case letters>"`` after the first
    ``"organism_detect":{`` is the call. The script looks only after an engine run that
    ended with status 0; :func:`summarise` applies that condition itself.
    """
    data = _read(os.path.join(os.fspath(out_dir), "report.json"))
    if not data:
        return ""
    flat = data.replace(b"\n", b"").replace(b" ", b"")
    start = flat.find(b'"organism_detect":{')
    if start < 0:
        return ""
    match = re.search(rb'"call":"([a-z]*)"', flat[start:])
    call = match.group(1).decode("ascii") if match else ""
    return call if call in ORGANISM_GENOME_MB else ""


# --------------------------------------------------------------------------- contigs

@dataclass(frozen=True)
class ContigStats:
    """The numbers of ``assembly_stats.tsv``, over every contig written (no length filter)."""
    contigs: int
    total_length: int
    largest: int
    n50: int
    l50: int
    n90: int
    l90: int
    duplicate_names: int = 0
    empty_records: int = 0
    bad_lines: int = 0           #: sequence lines with a character other than ACGTN (either case)

    @property
    def well_formed(self) -> bool:
        return not (self.duplicate_names or self.empty_records or self.bad_lines)

    def rows(self) -> List[Tuple[str, int]]:
        return [("contigs", self.contigs), ("total_bp", self.total_length), ("largest", self.largest),
                ("N50", self.n50), ("L50", self.l50), ("N90", self.n90), ("L90", self.l90)]


def contig_statistics(path: PathLike, *, strip_cr: bool = False) -> ContigStats:
    """Count a FASTA file the way the script's awk program does.

    A record is named by the first word of its header. A record whose name is empty (a
    header that is ``>`` alone or ``> text``) and sequence before the first header are not
    counted. N50 and N90 are the first lengths, largest first, at which the running sum
    reaches half and nine tenths of the total (``>=``, in double arithmetic).
    """
    count = total = duplicates = empty = bad = 0
    lengths: List[int] = []
    seen = set()
    name = b""
    length = 0
    with open(os.fspath(path), "rb") as handle:
        for line in handle:
            if line.endswith(b"\n"):
                line = line[:-1]
            if strip_cr and line.endswith(b"\r"):
                line = line[:-1]
            if line.startswith(b">"):
                if name != b"":
                    if length == 0:
                        empty += 1
                    lengths.append(length)
                    total += length
                    count += 1
                name = re.match(rb"[^ \t]*", line).group(0)[1:]      # awk's $1 without the ">"
                if name in seen:
                    duplicates += 1
                seen.add(name)
                length = 0
                continue
            if _VALID_BASES.search(line):
                bad += 1
            length += len(line)
    if name != b"":
        if length == 0:
            empty += 1
        lengths.append(length)
        total += length
        count += 1
    lengths.sort(reverse=True)
    largest = lengths[0] if lengths else 0
    n50 = n90 = l50 = l90 = running = 0
    for index, value in enumerate(lengths, 1):
        running += value
        if not n50 and running >= total * 0.5:
            n50, l50 = value, index
        if not n90 and running >= total * 0.9:
            n90, l90 = value, index
    return ContigStats(count, total, largest, n50, l50, n90, l90, duplicates, empty, bad)


# --------------------------------------------------------------------------- summarise

@dataclass(frozen=True)
class RunFacts:
    """The numbers behind the check lines, for whoever turns them into sentences for the screen.

    Each is what the line of that check was made from; nothing here is computed differently.
    """
    exit_code: int = 0
    retried: bool = False                        #: the result is the second attempt (k = 21,33,55)
    missing_files: Tuple[str, ...] = ()          #: of contigs.fasta, report.json, assembly_graph.gfa
    scaffold_records: Optional[int] = None       #: None: scaffolds.fasta was not written (no gaps left)
    expected_genome_mb: str = ""                 #: what the size check compared with; "" when it had nothing
    size_warned: bool = False
    report_readable: bool = True
    completeness: str = ""                       #: k-mer completeness of the self-check, as the engine wrote it
    qv: str = ""                                 #: its quality value, as the engine wrote it
    lost_kb: Optional[float] = None              #: high-copy sequence not in the assembly; None: no alarm
    circular: Optional[int] = None               #: replicons written circular; None: the report does not say
    circles_failed: int = 0                      #: circle candidates that failed verification
    spikein_records: int = 0                     #: PhiX records labelled ``_spikein``
    library_verdict: str = ""                    #: pair orientation check: ``pass``, ``outward``, ...; "" when absent


@dataclass(frozen=True)
class Postflight:
    """The result of :func:`summarise`."""
    lines: List[CheckLine]               #: the checks, in order
    fails: int
    warns: int
    text: str                            #: the whole of ``SUMMARY.txt``
    contigs: Optional[ContigStats]       #: None when the folder holds no contigs to count
    facts: RunFacts                      #: the numbers the lines were made from
    organism: str                        #: e.g. ``"ecoli"``; ``""`` when none was called
    organism_name: str                   #: e.g. ``"Escherichia coli"``
    summary_path: str
    stats_path: str                      #: ``""`` when ``assembly_stats.tsv`` was not written

    @property
    def ok(self) -> bool:
        """No check failed (warnings do not count)."""
        return self.fails == 0

    @property
    def data(self) -> bytes:
        return encode_text(self.text)


def _last_line(path: str) -> str:
    """``tail -1`` of a text file, without its line end; ``""`` for a missing or empty file."""
    data = _read(path)
    if not data:
        return ""
    if data.endswith(b"\n"):
        data = data[:-1]
    return _text(data.rsplit(b"\n", 1)[-1])


def _table_value(lines: List[bytes], key: bytes) -> bytes:
    """``awk -F'\\t' '$1==KEY {print $2}'``: every match, one per line."""
    values = []
    for line in lines:
        fields = line.split(b"\t")
        if fields[0] == key:
            values.append(fields[1] if len(fields) > 1 else b"")
    return b"\n".join(values)


def _first(pattern: bytes, text: bytes) -> bytes:
    match = re.search(pattern, text)
    return match.group(1) if match else b""


def summarise(out_dir: PathLike, *, read1: str, read2: str, exit_code: int, engine_version: str = "",
              genome_size_mb: Union[str, float, int, None] = None, engine_args: Sequence[str] = (),
              retried: bool = False, wording: str = "windows", shown_out: Optional[str] = None,
              preflight_line: Optional[str] = None) -> Postflight:
    """The postflight of ``tesseract`` on the output folder of one engine run.

    Writes ``out_dir/SUMMARY.txt`` and, when there are contigs to count,
    ``out_dir/assembly_stats.tsv``; returns the checks, the counts, the contig statistics
    and the organism the engine called.

    ``read1``, ``read2``
        The read files as they are to be printed (they are not opened).
    ``exit_code``
        How the last engine run ended.
    ``engine_version``
        The engine's own version line (:func:`engine_version_line`).
    ``genome_size_mb``
        What the user gave, or None. Without it the size check uses the genome size of the
        organism the engine called, and is left out when there is none.
    ``engine_args``
        The extra arguments the engine was given (``--no-gfa`` excuses a missing graph).
    ``retried``
        The second attempt of the thin-data retry was run.
    ``wording``
        ``"upstream"`` or ``"windows"``; see the module text.
    ``shown_out``
        The output folder as it is to be printed, when that is not ``out_dir`` itself.
    ``preflight_line``
        The closing line of the read check. None: the last line of ``out_dir/preflight.txt``.
    """
    if wording not in WORDINGS:
        raise ValueError("wording must be one of %s" % ", ".join(WORDINGS))
    windows = wording == "windows"
    folder = os.fspath(out_dir)
    shown = folder if shown_out is None else shown_out
    genome = genome_size_text(genome_size_mb)
    detected = detected_organism(folder) if exit_code == 0 else ""
    if preflight_line is None:
        preflight_line = _last_line(os.path.join(folder, "preflight.txt"))

    text: List[str] = [
        "TesserACT %s run summary" % TESSERACT_VERSION,
        "  reads    %s + %s" % (read1, read2),
        "  version  tesseract %s, engine %s" % (TESSERACT_VERSION, engine_version),
        _SETTINGS_LINE,
        "  preflight %s" % preflight_line,
        "[vanilla assembly] %s" % shown,
    ]
    lines: List[CheckLine] = []

    def pf(verdict: str, name: str, message: str) -> None:
        line = CheckLine(verdict, name, message)
        lines.append(line)
        text.append(line.text)

    stats, wrote_stats, facts = _check_folder(pf, folder, shown, exit_code, genome, detected, engine_args, windows)
    facts = replace(facts, retried=retried)
    if retried:
        pf("WARN", "thin data", _THIN_DATA)
    if detected:
        if windows:
            text.append("  hint     the reads look like %s" % ORGANISM_NAMES[detected])
        else:
            text.append("  hint     the reads look like %s, which has an organism model. To use it (your choice): "
                        "tesseract models download %s, then rerun with --organism %s" % (detected, detected, detected))
    fails = sum(1 for line in lines if line.verdict == "FAIL")
    warns = sum(1 for line in lines if line.verdict == "WARN")
    text.append("")
    text.append("postflight: %d failed, %d warnings (details above; this text is in %s/SUMMARY.txt)"
                % (fails, warns, shown))

    summary_path = os.path.join(folder, "SUMMARY.txt")
    stats_path = ""
    whole = "".join(line + "\n" for line in text)
    if wrote_stats and stats is not None:
        stats_path = os.path.join(folder, "assembly_stats.tsv")
        write_text_exact(stats_path, "metric\tvalue\n" + "".join("%s\t%d\n" % row for row in stats.rows()))
    write_text_exact(summary_path, whole)
    return Postflight(lines=lines, fails=fails, warns=warns, text=whole, contigs=stats, facts=facts, organism=detected,
                      organism_name=ORGANISM_NAMES.get(detected, ""), summary_path=summary_path,
                      stats_path=stats_path)


def _check_folder(pf, folder: str, shown: str, exit_code: int, genome: Optional[str], detected: str,
                  engine_args: Sequence[str], windows: bool,
                  ) -> Tuple[Optional[ContigStats], bool, RunFacts]:
    """Section 8.2: the checks of the output folder, in the script's order.

    Returns the contig statistics (None when they were not computed), whether
    ``assembly_stats.tsv`` is to be written, and the numbers the lines were made from.
    """
    if exit_code != 0:
        pf("FAIL", "exit status", "%d (see %s)" % (exit_code, shown + "/assembly.log" if windows else shown + ".log"))
        return None, False, RunFacts(exit_code=exit_code)
    pf("OK", "exit status", "0")
    missing = []
    for name in ("contigs.fasta", "report.json", "assembly_graph.gfa"):
        if not _non_empty(os.path.join(folder, name)):
            if name == "assembly_graph.gfa" and "--no-gfa" in engine_args:
                continue
            pf("FAIL", "file", "%s missing or empty" % name)
            missing.append(name)
    facts = RunFacts(missing_files=tuple(missing))
    scaffolds = os.path.join(folder, "scaffolds.fasta")
    if _non_empty(scaffolds):
        data = _read(scaffolds) or b""
        records = sum(1 for line in data.split(b"\n") if b">" in line)
        pf("OK", "scaffolds.fasta", "%d records (scaffolds with N gaps; scaffolds.agp lists them)" % records)
        facts = replace(facts, scaffold_records=records)
    else:
        pf("OK", "scaffolds.fasta",
           "not written: every scaffold join was closed with sequence, contigs.fasta is complete")
    contigs = os.path.join(folder, "contigs.fasta")
    if not _non_empty(contigs):
        return None, False, facts
    st = contig_statistics(contigs, strip_cr=windows)
    if st.well_formed:
        pf("OK", "contigs.fasta", "%d contigs, %d bp; largest %d; N50 %d (L50 %d); N90 %d (L90 %d)"
           % (st.contigs, st.total_length, st.largest, st.n50, st.l50, st.n90, st.l90))
    else:
        pf("FAIL", "contigs.fasta", "malformed: %d duplicate names, %d empty records, %d lines with non-ACGTN "
                                    "characters" % (st.duplicate_names, st.empty_records, st.bad_lines))
    expected = genome or ORGANISM_GENOME_MB.get(detected, "")
    if expected and st.total_length > 0:
        ratio = st.total_length / (float(expected) * 1e6)
        facts = replace(facts, expected_genome_mb=expected)
        if ratio < SIZE_LOW or ratio > SIZE_HIGH:
            pf("WARN", "size", "total %.2f Mb vs ~%s Mb expected: contamination, a mixed sample, or the wrong "
                               "genome size" % (st.total_length / 1e6, expected))
            facts = replace(facts, size_warned=True)
    report = (_read(os.path.join(folder, "report.json")) or b"").replace(b"\n", b"")
    if b'"version"' not in report:
        pf("FAIL", "report.json", "unreadable")
        facts = replace(facts, report_readable=False)
    self_check = False
    table = os.path.join(folder, "p2_self_qa.tsv")
    if _non_empty(table):
        rows = (_read(table) or b"").split(b"\n")
        if windows:
            rows = [row[:-1] if row.endswith(b"\r") else row for row in rows]
        completeness, qv = _table_value(rows, b"completeness"), _table_value(rows, b"qv_read0")
        alarm, lost = _table_value(rows, b"lost_replicon_alarm"), _table_value(rows, b"missing_bp_est_gt10x")
        self_check = True
        facts = replace(facts, completeness=_text(completeness), qv=_text(qv))
        if alarm == b"1":
            pf("WARN", "self-check",
               "k-mer completeness %s, QV %s; about %.1f kb of sequence seen in the reads at >10x genome depth is "
               "not in the assembly (a small high-copy plasmid, rRNA or IS copies); see p2_self_qa.missing_hi.tsv"
               % (_text(completeness), _text(qv), _awk_number(lost) / 1000))
            facts = replace(facts, lost_kb=_awk_number(lost) / 1000)
        else:
            pf("OK", "self-check", "k-mer completeness %s, QV %s; no high-copy sequence lost"
               % (_text(completeness), _text(qv)))
    circular = _first(rb'"written_circular": *([0-9]*)', report)
    linear = _first(rb'"written_linear": *([0-9]*)', report)
    if circular:
        pf("OK", "circles", "%s replicons written circular (read-verified join), %s circle candidates failed "
                            "verification and were written linear" % (_text(circular), _text(linear or b"0")))
        facts = replace(facts, circular=int(circular), circles_failed=int(linear or b"0"))
    spike = _first(rb'"spikein": *([0-9]*)', report)
    if spike and int(spike) > 0:
        pf("WARN", "spike-in", "%s PhiX record(s) labelled _spikein: remove them before submission" % _text(spike))
        facts = replace(facts, spikein_records=int(spike))
    guard = b"\n".join(re.findall(rb'"r2": *\{[^}]*"verdict": *"([a-z]*)"', report))
    if guard and guard != b"pass":
        pf("WARN", "library", "pair orientation check '%s': not a standard FR library; pair-based joins were "
                              "disabled" % _text(guard))
    facts = replace(facts, library_verdict=_text(guard))
    if windows:
        pf("OK", "map-back", _MAP_BACK["windows"] + (_MAP_BACK_COVERED if self_check else ""))
    else:
        pf("OK", "map-back", _MAP_BACK["upstream"])
    return st, True, facts
