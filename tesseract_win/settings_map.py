# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Settings of the Trimming and Assembly pages turned into engine commands.

One place decides which flag a tick becomes, so the window, the runner, the summaries and
the tests cannot drift apart. Nothing here starts a process or touches a file.

The trimming flags follow ``docs/superpowers/audits/scepter-port.md`` sections B1 to B3:
``scepter_command(exe, TrimSettings(), ...)`` is the audit's "fully explicit form" of
``--preset assembly``. The assembly arguments follow the bash ``tesseract`` 1.6.2 script,
which starts the engine as ``tesseract-asm -1 R1 -2 R2 -o OUT -t N [-k 21,33,55] <the rest>``.

The builders do not check values. Call :func:`validate_trim` and :func:`validate_assembly`
first: an out-of-range number would make the engine stop with its own error.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

from .model import ASSEMBLY_MODES, AssemblySettings, TrimSettings, default_threads

__all__ = [
    "TRIM_RANGES", "ASSEMBLY_RANGES", "FIELD_LABELS", "MIN_ADAPTER_LENGTH", "MAX_ADAPTER_LENGTH",
    "SCEPTER_MAX_THREADS", "N_BASE_LIMIT", "THIN_DATA_KMERS", "PROFILE_ENVIRONMENT",
    "TRIMMING_REPORT_JSON", "TRIMMING_REPORT_HTML", "TRIMMING_LOG", "TrimmingFiles",
    "number_range", "clamp_number", "typed_number", "parse_genome_size",
    "validate_trim", "validate_assembly",
    "scepter_threads", "scepter_command", "trimmed_names", "trimming_files",
    "tesseract_command", "expected_genome_size_mb", "engine_environment",
    "describe", "differences", "settings_fingerprint",
]

Number = Union[int, float]

# --------------------------------------------------------------------------- ranges

#: Whole-number fields of the Trimming page: lowest and highest value scepter accepts
#: (``model.TrimSettings`` comments; scepter-port.md B2).
TRIM_RANGES: Dict[str, Tuple[int, int]] = {
    "end_quality": (1, 36),
    "min_mean_quality": (0, 60),
    "base_quality": (0, 60),
    "max_low_percent": (0, 100),
    "min_length": (0, 10000),
    "trim_front": (0, 100),
    "trim_tail": (0, 100),
}

#: Number fields of the Assembly page. ``threads`` is not here: its top is this PC's
#: number of logical processors (see :func:`number_range`).
ASSEMBLY_RANGES: Dict[str, Tuple[Number, Number]] = {
    "genome_size_mb": (0.1, 20.0),
    "min_contig": (1, 100000),
}

#: The label each field carries on its page, used at the start of a validation message.
FIELD_LABELS: Dict[str, str] = {
    "end_quality": "Trim ends below quality",
    "min_mean_quality": "Remove reads with an average quality below",
    "base_quality": "Quality below which a base counts as poor",
    "max_low_percent": "Share of poor bases allowed in a read (%)",
    "min_length": "Remove reads shorter than",
    "trim_front": "Bases to cut from the start of every read",
    "trim_tail": "Bases to cut from the end of every read",
    "adapter_r1": "Adapter sequence for Read 1",
    "adapter_r2": "Adapter sequence for Read 2",
    "genome_size_mb": "Genome size (Mb)",
    "min_contig": "Leave out contigs shorter than",
    "mode": "Assembly mode",
    "threads": "Processor threads",
}

MIN_ADAPTER_LENGTH = 10
MAX_ADAPTER_LENGTH = 200
_ADAPTER_LETTERS = re.compile(r"^[ACGTacgt]+$")

#: scepter's own default is capped at 16 workers and nothing is gained above about 8
#: (scepter-port.md B6).
SCEPTER_MAX_THREADS = 16
#: In force whenever "Remove poor-quality reads" is ticked; not shown on the page (B2).
N_BASE_LIMIT = 5
#: The short k-mer ladder of the thin-data retry (``tesseract`` script, lines 278-298).
THIN_DATA_KMERS = "21,33,55"

_FLOAT_FIELDS = ("genome_size_mb",)


def number_range(field: str) -> Tuple[Number, Number]:
    """Lowest and highest value allowed in a number field. Raises ``KeyError`` for another name."""
    if field == "threads":
        return (1, default_threads())
    if field in TRIM_RANGES:
        return TRIM_RANGES[field]
    return ASSEMBLY_RANGES[field]


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _plain(value: Number) -> str:
    """5.0 -> '5', 0.1 -> '0.1', 20 -> '20'."""
    if isinstance(value, float) and not value.is_integer():
        return ("%.3f" % value).rstrip("0").rstrip(".")
    return "%d" % int(value)


def _range_words(field: str) -> str:
    low, high = number_range(field)
    kind = "a number" if field in _FLOAT_FIELDS else "a whole number"
    return "%s from %s to %s" % (kind, _plain(low), _plain(high))


def _standard_value(field: str) -> Number:
    """What ``field`` holds when the page is at standard."""
    if field == "threads":
        return default_threads()
    settings: Any = TrimSettings() if field in TRIM_RANGES else AssemblySettings()
    return getattr(settings, field)


def _as_number(value: Any) -> Optional[float]:
    """``value`` as a float, or None when it is not a number (text that is one counts)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = parse_genome_size(value)       # digits, with a dot or a comma
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return float("inf") if value > 0 else float("-inf")
    return None if number != number else number


def clamp_number(field: str, value: Any) -> Number:
    """The nearest value inside the range of ``field``. Whole-number fields stay whole.

    It never raises for a value: something that is no number at all (None, "abc", NaN)
    gives the standard value of the field. ``KeyError`` for an unknown field name.
    """
    low, high = number_range(field)
    number = _as_number(value)
    if number is None:
        return _standard_value(field)
    number = min(max(number, float(low)), float(high))
    if field in _FLOAT_FIELDS:
        return number
    return int(round(number))


def parse_genome_size(text: str) -> Optional[float]:
    """A typed genome size in Mb. Accepts "5.5" and "5,5". None when it is not a number."""
    cleaned = str(text).strip().replace(",", ".")
    if not re.match(r"^\d+(\.\d*)?$|^\.\d+$", cleaned):
        return None
    return float(cleaned)


def typed_number(field: str, text: str, last_good: Number) -> Tuple[Number, str]:
    """What a number field holds after the user has left it.

    Returns ``(value, message)``. A good entry comes back as a number with an empty message.
    Anything else gives back ``last_good`` and a line for the help line, for example
    "Use a whole number from 0 to 100. Set back to 40." Never a pop-up, never an error.
    If ``last_good`` is itself no number, the standard value of the field is given back;
    if it lies outside the range, the nearest value inside it.
    """
    low, high = number_range(field)
    value: Optional[Number]
    if field in _FLOAT_FIELDS:
        value = parse_genome_size(text)
    else:
        cleaned = str(text).strip()
        value = int(cleaned) if re.match(r"^\d{1,9}$", cleaned) else None
    if value is not None and low <= value <= high:
        return value, ""
    last_good = clamp_number(field, last_good)       # in range whatever was handed in
    shown = ("%.1f" % last_good) if field in _FLOAT_FIELDS else "%d" % last_good
    return last_good, "Use %s. Set back to %s." % (_range_words(field), shown)


# --------------------------------------------------------------------------- validation

def _check_whole(problems: List[Tuple[str, str]], settings: Any, field: str) -> None:
    value = getattr(settings, field)
    low, high = number_range(field)
    if not _is_whole(value) or not low <= value <= high:
        problems.append((field, "%s: use %s." % (FIELD_LABELS[field], _range_words(field))))


def _adapter(text: Any) -> str:
    return str(text or "").strip().upper()


def _check_adapter(problems: List[Tuple[str, str]], field: str, text: Any) -> None:
    label = FIELD_LABELS[field]
    sequence = str(text or "").strip()
    if not _ADAPTER_LETTERS.match(sequence):
        problems.append((field, "%s: use only the letters A, C, G and T." % label))
    elif len(sequence) < MIN_ADAPTER_LENGTH:
        problems.append((field, "%s: too short. An adapter sequence has at least %d letters."
                         % (label, MIN_ADAPTER_LENGTH)))
    elif len(sequence) > MAX_ADAPTER_LENGTH:
        problems.append((field, "%s: too long. Use %d letters at most."
                         % (label, MAX_ADAPTER_LENGTH)))


def validate_trim(trim: TrimSettings) -> List[Tuple[str, str]]:
    """Everything that is wrong on the Trimming page, as ``(field name, plain message)``.

    An empty list means the settings can be run. Only values that are in use are checked:
    a number behind an unticked box cannot stop a run.
    """
    problems: List[Tuple[str, str]] = []
    if trim.trim_low_quality_ends or trim.trim_low_quality_starts:
        _check_whole(problems, trim, "end_quality")
    if trim.remove_poor_reads:
        for field in ("min_mean_quality", "base_quality", "max_low_percent"):
            _check_whole(problems, trim, field)
    if trim.remove_short_reads:
        _check_whole(problems, trim, "min_length")
    if trim.fixed_trim:
        _check_whole(problems, trim, "trim_front")
        _check_whole(problems, trim, "trim_tail")
    # "Remove adapters" unticked switches every kind of adapter removal off, custom
    # sequences included, so they are not looked at then.
    if trim.own_adapters and trim.remove_adapters:
        if not str(trim.adapter_r1 or "").strip():
            problems.append(("adapter_r1", "%s: type the sequence, or untick "
                             "\"Use my own adapter sequences\"." % FIELD_LABELS["adapter_r1"]))
        else:
            _check_adapter(problems, "adapter_r1", trim.adapter_r1)
        if str(trim.adapter_r2 or "").strip():
            _check_adapter(problems, "adapter_r2", trim.adapter_r2)
    return problems


def validate_assembly(assembly: AssemblySettings,
                      logical_processors: Optional[int] = None) -> List[Tuple[str, str]]:
    """Everything that is wrong on the Assembly page, as ``(field name, plain message)``.

    ``logical_processors`` is the top of the threads range; left out, it is this PC's count.
    The genome size and the contig length are checked only when their boxes are ticked.
    """
    problems: List[Tuple[str, str]] = []
    if assembly.know_genome_size:
        low, high = ASSEMBLY_RANGES["genome_size_mb"]
        value = assembly.genome_size_mb
        if not _is_number(value) or not low <= value <= high:
            problems.append(("genome_size_mb", "%s: use %s." % (FIELD_LABELS["genome_size_mb"],
                                                                _range_words("genome_size_mb"))))
    if assembly.drop_short_contigs:
        _check_whole(problems, assembly, "min_contig")
    if assembly.mode not in ASSEMBLY_MODES:
        problems.append(("mode", "%s: choose Standard, Fast, Careful or Aggressive."
                         % FIELD_LABELS["mode"]))
    top = max(1, int(logical_processors)) if logical_processors else default_threads()
    if not _is_whole(assembly.threads) or not 1 <= assembly.threads <= top:
        if top == 1:
            words = "this PC has 1 processor thread, so use 1"
        else:
            words = "use a whole number from 1 to %d (this PC has %d)" % (top, top)
        problems.append(("threads", "%s: %s." % (FIELD_LABELS["threads"], words)))
    return problems


# --------------------------------------------------------------------------- trimming

TRIMMING_REPORT_JSON = "trimming_report.json"
TRIMMING_REPORT_HTML = "trimming_report.html"
TRIMMING_LOG = "trimming.log"


class TrimmingFiles(NamedTuple):
    """The five file names of a sample's ``trimming`` folder (decision D1)."""
    read1: str
    read2: str
    json_report: str
    html_report: str
    log: str


def trimmed_names(sample_name: str) -> Tuple[str, str]:
    """``("<sample>_trimmed_R1.fastq.gz", "<sample>_trimmed_R2.fastq.gz")``.

    ``pairing.scan`` knows these endings and never picks such files up from a folder.
    """
    return ("%s_trimmed_R1.fastq.gz" % sample_name, "%s_trimmed_R2.fastq.gz" % sample_name)


def trimming_files(sample_name: str) -> TrimmingFiles:
    read1, read2 = trimmed_names(sample_name)
    return TrimmingFiles(read1, read2, TRIMMING_REPORT_JSON, TRIMMING_REPORT_HTML, TRIMMING_LOG)


def scepter_threads(threads: int) -> int:
    """Worker threads for scepter: the threads asked for, but 16 at most and 1 at least."""
    return max(1, min(int(threads), SCEPTER_MAX_THREADS))


def scepter_command(scepter_exe: str, trim: TrimSettings, *, read1: str, read2: str,
                    out1: str, out2: str, json_report: str, html_report: str,
                    title: str, threads: int) -> List[str]:
    """The scepter command line for one sample, as a list for ``subprocess``.

    Mapping: scepter-port.md B2 (one row per control) and B3 (outputs, title, what is
    printed). Every value on the page is passed explicitly, so nothing depends on
    scepter's own defaults. Never ``--preset`` (a preset cannot be switched off piece by
    piece), never ``--out-dir`` (it would give the trimmed files the names of the raw
    ones), never ``-D`` and never ``-c`` (both are off in the ``assembly`` preset).

    ``-h`` always gets a value: a bare ``-h`` is scepter's help.
    """
    cmd: List[str] = [
        scepter_exe,
        "-i", read1, "-I", read2,
        "-o", out1, "-O", out2,
        "-j", json_report, "-h", html_report,
        "-R", title,
        "-w", str(scepter_threads(threads)),
        "--no-color", "--no-summary", "--verbose",
    ]
    # Remove adapters. Unticked: -A, which also switches custom sequences off.
    if not trim.remove_adapters:
        cmd.append("-A")
    elif trim.own_adapters:
        adapter1, adapter2 = _adapter(trim.adapter_r1), _adapter(trim.adapter_r2)
        if adapter1:
            cmd += ["-a", adapter1]
        if adapter2:
            cmd += ["--adapter_sequence_r2", adapter2]
    # Cut a fixed number of bases from every read (read 2 follows read 1).
    if trim.fixed_trim:
        cmd += ["-f", str(int(trim.trim_front)), "-t", str(int(trim.trim_tail))]
    # Trim low-quality starts too: the same quality as the ends. Left to itself
    # --cut_front would use Q20, which is far harsher than the ends at Q10.
    if trim.trim_low_quality_starts:
        cmd += ["--cut_front", "--cut_front_mean_quality", str(int(trim.end_quality))]
    # Trim low-quality ends.
    if trim.trim_low_quality_ends:
        cmd += ["--cut_tail", "--cut_tail_mean_quality", str(int(trim.end_quality))]
    # Trim poly-G tails. Ticked means automatic (no flag); unticked means never.
    if not trim.trim_polyg:
        cmd.append("-G")
    # Trim repeated-base tails.
    if trim.trim_polyx:
        cmd.append("-x")
    # Remove poor-quality reads. -Q switches off the percentage rule, the mean-quality
    # rule and the N-base rule together.
    if trim.remove_poor_reads:
        cmd += ["--qualified_quality_phred", str(int(trim.base_quality)),
                "--unqualified_percent_limit", str(int(trim.max_low_percent)),
                "--n_base_limit", str(N_BASE_LIMIT),
                "--average_qual", str(int(trim.min_mean_quality))]
    else:
        cmd.append("-Q")
    # Remove very short reads.
    if trim.remove_short_reads:
        cmd += ["--length_required", str(int(trim.min_length))]
    else:
        cmd.append("-L")
    return cmd


# --------------------------------------------------------------------------- assembly

def tesseract_command(asm_exe: str, assembly: AssemblySettings, *, read1: str, read2: str,
                      out_dir_name: str, kmers: Optional[str] = None) -> List[str]:
    """The ``tesseract-asm`` command line for one sample.

    For ``AssemblySettings()`` it is ``-1 R1 -2 R2 -o OUT -t N`` and nothing else, which is
    what the bash ``tesseract`` command runs. ``kmers`` (:data:`THIN_DATA_KMERS`) is given
    only for the thin-data retry and goes where the script puts it, right after ``-t``.

    Not engine options, so not here: the genome size (:func:`expected_genome_size_mb`) and
    "Assemble even if the read check fails" (``assembly.force``). Both belong to the read
    check and the summary.
    """
    cmd: List[str] = [asm_exe, "-1", read1, "-2", read2, "-o", out_dir_name,
                      "-t", str(max(1, int(assembly.threads)))]
    if kmers:
        cmd += ["-k", kmers]
    if assembly.mode != "standard":
        cmd += ["--mode", assembly.mode]
    if assembly.drop_short_contigs:
        cmd += ["--min-contig", str(int(assembly.min_contig))]
    if not assembly.scaffold:
        cmd.append("--no-scaffold")
    if not assembly.polish:
        cmd.append("--no-polish")
    return cmd


def expected_genome_size_mb(assembly: AssemblySettings) -> Optional[float]:
    """The genome size the user gave, in Mb, or None when the box is not ticked.

    None means what it means in the bash script: 5 Mb is assumed and the read check says so.
    """
    if not assembly.know_genome_size:
        return None
    return float(assembly.genome_size_mb)


# The profile the bash ``tesseract`` command applies to every run: verified circles,
# library guard, spike-in labels, end audit, k-mer self-check, provenance and high-copy
# plasmid recovery. Source: docs/superpowers/audits/engine-port.md section 8.7, which
# quotes ``tesseract`` lines 261-264. Without these nine, contigs.fasta differs from what
# a Linux user of ``tesseract`` gets.
PROFILE_ENVIRONMENT: Tuple[Tuple[str, str], ...] = (
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


def engine_environment(base_env: Mapping[str, str], detect_sketch: str) -> Dict[str, str]:
    """The environment both engines are started with.

    A copy of ``base_env`` (normally ``os.environ``) without any ``TESSERACT_*`` variable:
    the engine checks every one of them and stops on a value it does not like, and "the
    user's environment wins" suits a shell, not a window. Then the nine profile variables,
    the two detection variables when ``detect_sketch`` (``EnginePaths.detect_sketch``) is
    not empty, and ``NO_COLOR=1``. ``base_env`` itself is not changed.

    Names are compared without regard to case, as Windows does.
    """
    env: Dict[str, str] = {}
    for name, value in (base_env or {}).items():
        upper = str(name).upper()
        if upper.startswith("TESSERACT_") or upper == "NO_COLOR":
            continue
        env[str(name)] = str(value)
    for name, value in PROFILE_ENVIRONMENT:
        env[name] = value
    if detect_sketch:
        env["TESSERACT_P2_DETECT_REPORT"] = "1"
        env["TESSERACT_P2_DETECT_SKETCH"] = str(detect_sketch)
    env["NO_COLOR"] = "1"
    return env


# --------------------------------------------------------------------------- words
# These describe settings; they are also called on settings read back from disk, before
# anything was checked, so they never raise: a value that is not a number counts as 0.

def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def _effective_trim(trim: TrimSettings) -> Dict[str, Any]:
    """The trimming settings that reach scepter, with unused numbers taken out."""
    own = bool(trim.remove_adapters and trim.own_adapters)
    fixed = bool(trim.fixed_trim and (_int(trim.trim_front) or _int(trim.trim_tail)))
    return {
        "adapters": bool(trim.remove_adapters),
        "adapter_r1": _adapter(trim.adapter_r1) if own else "",
        "adapter_r2": _adapter(trim.adapter_r2) if own else "",
        "ends": _int(trim.end_quality) if trim.trim_low_quality_ends else None,
        "starts": _int(trim.end_quality) if trim.trim_low_quality_starts else None,
        "poor": ([_int(trim.min_mean_quality), _int(trim.base_quality), _int(trim.max_low_percent)]
                 if trim.remove_poor_reads else None),
        "short": _int(trim.min_length) if trim.remove_short_reads else None,
        "polyg": bool(trim.trim_polyg),
        "polyx": bool(trim.trim_polyx),
        "fixed": [_int(trim.trim_front), _int(trim.trim_tail)] if fixed else None,
    }


def _effective_assembly(assembly: AssemblySettings) -> Dict[str, Any]:
    """The assembly settings that can change a result. Threads cannot, and are left out."""
    return {
        "genome_size_mb": ("%.3f" % _float(assembly.genome_size_mb)
                           if assembly.know_genome_size else None),
        "min_contig": _int(assembly.min_contig) if assembly.drop_short_contigs else None,
        "mode": str(assembly.mode),
        "scaffold": bool(assembly.scaffold),
        "polish": bool(assembly.polish),
    }


_FORCE_PHRASE = "assembled even if the read check fails"


def _count(number: int, noun: str) -> str:
    return "%d %s%s" % (number, noun, "" if number == 1 else "s")


def _trim_differences(trim: TrimSettings) -> List[str]:
    now, standard = _effective_trim(trim), _effective_trim(TrimSettings())
    out: List[str] = []
    if not now["adapters"]:
        out.append("adapters not removed")
    elif now["adapter_r1"] or now["adapter_r2"]:
        # The tick alone, with both boxes empty, sends nothing to the trimmer: no difference.
        out.append("own adapter sequences used")
    if now["ends"] is None:
        out.append("low-quality ends not trimmed")
    elif now["ends"] != standard["ends"]:
        out.append("ends trimmed below quality %d" % now["ends"])
    if now["starts"] is not None:
        if now["ends"] is not None:
            out.append("low-quality starts trimmed too")
        else:
            out.append("low-quality starts trimmed below quality %d" % now["starts"])
    if now["poor"] is None:
        out.append("poor-quality reads kept")
    else:
        mean, base, percent = now["poor"]
        if mean != standard["poor"][0]:
            out.append("reads with an average quality below %d removed" % mean)
        if [base, percent] != standard["poor"][1:]:
            out.append("reads with more than %d %% of bases below quality %d removed" % (percent, base))
    if now["short"] is None:
        out.append("very short reads kept")
    elif now["short"] != standard["short"]:
        out.append("reads shorter than %s removed" % _count(now["short"], "base"))
    if not now["polyg"]:
        out.append("poly-G tails not trimmed")
    if now["polyx"]:
        out.append("repeated-base tails trimmed")
    if now["fixed"] is not None:
        out.append("%s cut from the start and %d from the end of every read"
                   % (_count(now["fixed"][0], "base"), now["fixed"][1]))
    if not trim.keep_trimmed_reads:
        out.append("trimmed reads not kept")
    return out


def _assembly_differences(assembly: AssemblySettings) -> List[str]:
    """Differences that can change a result (no threads, no "assemble even if...")."""
    out: List[str] = []
    if assembly.know_genome_size:
        out.append("genome size about %.1f Mb" % _float(assembly.genome_size_mb))
    if assembly.drop_short_contigs:
        out.append("contigs shorter than %s left out" % _count(_int(assembly.min_contig), "base"))
    if assembly.mode != "standard":
        out.append("mode %s" % str(assembly.mode).capitalize())
    if not assembly.scaffold:
        out.append("no scaffolding")
    if assembly.polish:
        out.append("with polishing")
    return out


def differences(trim: TrimSettings, assembly: AssemblySettings, *, trimmed: bool = True,
                include_threads: bool = False) -> List[str]:
    """One plain phrase per setting that is not standard, in page order.

    For the overwrite question ("made with different settings (not trimmed; mode Careful)")
    and the summaries. An empty list means everything is standard. A number behind an
    unticked box does not count.

    ``trimmed=False`` describes a sample that is not trimmed: the trimming settings are
    replaced by the one phrase "not trimmed". The number of threads does not change a
    result, so it is listed only with ``include_threads=True``.
    """
    out = _trim_differences(trim) if trimmed else ["not trimmed"]
    out += _assembly_differences(assembly)
    if assembly.force:
        out.append(_FORCE_PHRASE)
    if include_threads and assembly.threads != default_threads():
        out.append(_count(max(1, _int(assembly.threads)), "thread"))
    return out


def describe(trim: TrimSettings, assembly: AssemblySettings, any_trimmed: bool) -> str:
    """One short line for the Run page and the summaries.

    "standard trimming, standard assembly, 8 threads", "trimming changed, mode Careful,
    8 threads", "no trimming, standard assembly, 4 threads". Up to two assembly changes
    are named; more become "assembly changed".
    """
    parts: List[str] = []
    if not any_trimmed:
        parts.append("no trimming")
    elif _effective_trim(trim) == _effective_trim(TrimSettings()):
        parts.append("standard trimming")
    else:
        parts.append("trimming changed")
    changed = _assembly_differences(assembly)
    if assembly.force:
        changed.append(_FORCE_PHRASE)
    if not changed:
        parts.append("standard assembly")
    elif len(changed) <= 2:
        parts.extend(changed)
    else:
        parts.append("assembly changed")
    parts.append(_count(max(1, _int(assembly.threads)), "thread"))
    return ", ".join(parts)


def settings_fingerprint(trim: TrimSettings, assembly: AssemblySettings, trimmed: bool) -> str:
    """A short, stable name for the settings that can change a sample's result.

    Two runs with the same fingerprint, the same reads and the same engines give the same
    assembly. Left out on purpose: the number of threads (the result does not depend on
    it), "Keep the trimmed reads" (disk space only), "Assemble even if the read check
    fails" (it decides whether there is a result, not what it is), and every number behind
    an unticked box. With ``trimmed=False`` the trimming settings do not count at all.

    The string starts with a format mark ("s1-") so that a later change of the rule cannot
    be mistaken for a change of settings.
    """
    payload = {
        "trim": _effective_trim(trim) if trimmed else None,
        "assembly": _effective_assembly(assembly),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "s1-" + hashlib.sha256(text.encode("ascii")).hexdigest()[:16]
