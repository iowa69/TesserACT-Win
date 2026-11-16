# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Shared data types: the contract between the core and the window.

Nothing here does any work. Every other module imports its types from this file,
so a name or a default changed here changes it everywhere.

Standard settings are the dataclass defaults. ``TrimSettings()`` is exactly
scepter's ``assembly`` preset; ``AssemblySettings()`` is what the Ubuntu
``tesseract`` command runs with no options, except that the final polishing step is off
(the owner's choice for this program; tick it under More options to have it).
"""

from __future__ import annotations

import enum
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SampleState", "RunStatus", "Stage", "StopMode", "Existing", "EndReason",
    "Sample", "TrimSettings", "AssemblySettings", "RunConfig", "SampleOutcome",
    "ExistingResult", "EnginePaths",
    "Event", "BatchStarted", "SampleStatusChanged", "StageProgress", "Working",
    "LogLine", "SampleFinished", "BatchProgress", "Notice", "BatchFinished",
    "ASSEMBLY_MODES", "default_threads",
]

ASSEMBLY_MODES = ("standard", "fast", "careful", "aggressive")


def default_threads() -> int:
    """Every logical processor of this PC (never less than 1)."""
    return max(1, os.cpu_count() or 1)


# --------------------------------------------------------------------------- enums

class SampleState(str, enum.Enum):
    """State of a row on the Samples page."""
    READY = "ready"
    READY_NOTE = "ready_note"      # will run; ``Sample.note`` says something worth knowing
    LEFT_OUT = "left_out"          # will not run; ``Sample.note`` says why, in plain words


class RunStatus(str, enum.Enum):
    """State of a row on the Run page."""
    WAITING = "waiting"
    JOINING_LANES = "joining_lanes"
    TRIMMING = "trimming"
    CHECKING_READS = "checking_reads"
    ASSEMBLING = "assembling"
    SUMMARISING = "summarising"
    DONE = "done"
    DONE_NOTES = "done_notes"      # finished, with warnings the user should read
    FAILED = "failed"
    SKIPPED = "skipped"            # already assembled, user chose Skip
    STOPPED = "stopped"            # was running when the user pressed Stop now
    NOT_STARTED = "not_started"    # still waiting when the batch ended early
    LEFT_OUT = "left_out"

    @property
    def is_running(self) -> bool:
        return self in (RunStatus.JOINING_LANES, RunStatus.TRIMMING, RunStatus.CHECKING_READS,
                        RunStatus.ASSEMBLING, RunStatus.SUMMARISING)

    @property
    def is_final(self) -> bool:
        return not self.is_running and self is not RunStatus.WAITING


class Stage(str, enum.Enum):
    """The chips shown for the sample that is running."""
    JOIN_LANES = "join_lanes"
    TRIM = "trim"
    CHECK_READS = "check_reads"
    ASSEMBLE = "assemble"
    SUMMARISE = "summarise"


class StopMode(str, enum.Enum):
    NOW = "now"                    # end the engines at once; the running sample is lost
    AFTER_CURRENT = "after_current"


class Existing(str, enum.Enum):
    """What to do with samples that already have a finished result in the results folder."""
    SKIP = "skip"
    OVERWRITE = "overwrite"


class EndReason(str, enum.Enum):
    COMPLETED = "completed"        # every sample was attempted
    STOPPED = "stopped"            # the user stopped it
    ENVIRONMENT = "environment"    # the PC, not a sample, was at fault (disk, drive, engine gone)


# --------------------------------------------------------------------------- inputs

@dataclass
class Sample:
    """One isolate: a name and its read files.

    ``lanes`` holds one ``(read1, read2)`` pair of absolute paths per lane, in lane
    order. A sample sequenced on one lane has exactly one pair.
    """
    name: str
    lanes: List[Tuple[str, str]]
    trim: bool = True
    size_bytes: int = 0
    state: SampleState = SampleState.READY
    note: str = ""
    assemble: bool = True          # unticked: the sample is trimmed only

    @property
    def ready(self) -> bool:
        return self.state is not SampleState.LEFT_OUT

    @property
    def read1(self) -> str:
        return self.lanes[0][0] if self.lanes else ""

    @property
    def read2(self) -> str:
        return self.lanes[0][1] if self.lanes else ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["lanes"] = [list(pair) for pair in self.lanes]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Sample":
        return cls(name=d["name"], lanes=[(a, b) for a, b in d.get("lanes", [])],
                   trim=bool(d.get("trim", True)), assemble=bool(d.get("assemble", True)),
                   size_bytes=int(d.get("size_bytes", 0)),
                   state=SampleState(d.get("state", "ready")), note=d.get("note", ""))


@dataclass
class TrimSettings:
    """The Trimming page. Defaults are scepter's ``assembly`` preset, flag for flag."""
    # --- on the page, all ticked by default
    remove_adapters: bool = True            # unticked: -A
    trim_low_quality_ends: bool = True      # --cut_tail --cut_tail_mean_quality <end_quality>
    remove_poor_reads: bool = True          # unticked: -Q
    remove_short_reads: bool = True         # unticked: -L
    trim_polyg: bool = True                 # ticked: automatic; unticked: -G
    keep_trimmed_reads: bool = True         # unticked: trimmed reads deleted after a good assembly
    # --- More options: the numbers behind the ticks above
    end_quality: int = 10                   # 1..36
    min_mean_quality: int = 15              # --average_qual, 0..60 (0 = rule off)
    base_quality: int = 15                  # --qualified_quality_phred, 0..60
    max_low_percent: int = 40               # --unqualified_percent_limit, 0..100
    min_length: int = 50                    # --length_required, 0..10000
    # --- More options: off by default
    trim_low_quality_starts: bool = False   # --cut_front --cut_front_mean_quality <end_quality>
    trim_polyx: bool = False                # -x (any repeated base at the end of a read)
    fixed_trim: bool = False                # -f <trim_front> -t <trim_tail>
    trim_front: int = 0                     # 0..100
    trim_tail: int = 0                      # 0..100
    own_adapters: bool = False              # -a <adapter_r1> [--adapter_sequence_r2 <adapter_r2>]
    adapter_r1: str = ""
    adapter_r2: str = ""

    def is_standard(self) -> bool:
        return self == TrimSettings()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrimSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class AssemblySettings:
    """The Assembly page. Defaults are a plain ``tesseract -1 R1 -2 R2 -o OUT`` run."""
    # --- on the page
    know_genome_size: bool = False
    genome_size_mb: float = 5.0             # 0.1..20; used by the depth and size checks only
    drop_short_contigs: bool = False
    min_contig: int = 500                   # --min-contig, 1..100000
    # --- More options
    mode: str = "standard"                  # one of ASSEMBLY_MODES; not "standard": --mode <mode>
    scaffold: bool = True                   # unticked: --no-scaffold
    polish: bool = False                    # unticked (the standard here): --no-polish
    force: bool = False                     # assemble even if the read check fails
    threads: int = field(default_factory=default_threads)

    def is_standard(self) -> bool:
        return self == AssemblySettings(threads=self.threads) and self.threads == default_threads()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AssemblySettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class RunConfig:
    """Everything a batch needs. ``samples`` holds only the samples to run, in order."""
    results_dir: str
    samples: List[Sample]
    trim: TrimSettings = field(default_factory=TrimSettings)
    assembly: AssemblySettings = field(default_factory=AssemblySettings)
    existing: Existing = Existing.SKIP

    def to_dict(self) -> Dict[str, Any]:
        return {"results_dir": self.results_dir,
                "samples": [s.to_dict() for s in self.samples],
                "trim": self.trim.to_dict(), "assembly": self.assembly.to_dict(),
                "existing": self.existing.value}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunConfig":
        return cls(results_dir=d["results_dir"],
                   samples=[Sample.from_dict(s) for s in d.get("samples", [])],
                   trim=TrimSettings.from_dict(d.get("trim", {})),
                   assembly=AssemblySettings.from_dict(d.get("assembly", {})),
                   existing=Existing(d.get("existing", "skip")))


@dataclass(frozen=True)
class EnginePaths:
    """Where the bundled programs and data are."""
    tesseract_asm: str
    scepter: str
    detect_sketch: str               # om2detect.sketch; "" if absent


# --------------------------------------------------------------------------- outputs

@dataclass
class SampleOutcome:
    """How one sample ended. Also what ``sample_status.json`` and the summaries are built from."""
    name: str
    status: RunStatus
    note: str = ""                   # first thing the user should read, in plain words
    notes: List[str] = field(default_factory=list)   # every warning, in plain words
    next_step: str = ""              # for FAILED: what to do about it
    trimmed: bool = False
    contigs: Optional[int] = None
    total_length: Optional[int] = None
    largest: Optional[int] = None
    n50: Optional[int] = None
    depth: Optional[int] = None      # estimated depth, x
    thin_retry: bool = False         # the second attempt for very thin data was used
    seconds: float = 0.0
    finished_at: str = ""            # ISO 8601 local time
    folder: str = ""                 # the sample's folder
    report_html: str = ""            # assembly/report.html if written
    log: str = ""                    # the log to open for a failed sample
    kept_earlier_result: bool = False    # a redo failed or was stopped; the old assembly is intact

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SampleOutcome":
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        kw["status"] = RunStatus(kw.get("status", "failed"))
        return cls(**kw)


@dataclass
class ExistingResult:
    """A finished result found in the results folder for a sample in the list."""
    name: str
    finished_at: str                 # ISO 8601
    same_reads: bool                 # made from the same read files (names and sizes)
    same_settings: bool              # made with the settings now on the pages
    settings_summary: str = ""       # e.g. "not trimmed; mode Careful"; "" when same_settings


# --------------------------------------------------------------------------- events
# Posted by the batch runner on a queue.Queue and read by the window (or by a test).

@dataclass
class Event:
    pass


@dataclass
class BatchStarted(Event):
    total: int = 0
    names: List[str] = field(default_factory=list)


@dataclass
class SampleStatusChanged(Event):
    name: str = ""
    status: RunStatus = RunStatus.WAITING
    note: str = ""


@dataclass
class StageProgress(Event):
    """What is happening inside the running sample.

    ``text`` is ready to show, e.g. "Step 3 of 7: building the assembly graph (k = 55)" or
    "Checking reads: 62 %". ``fraction`` is 0..1 where an honest figure exists, else None.
    """
    name: str = ""
    stage: Stage = Stage.ASSEMBLE
    text: str = ""
    fraction: Optional[float] = None


@dataclass
class Working(Event):
    """Proof of life from the running engine, about once a second."""
    name: str = ""
    busy_threads: float = 0.0        # processor threads in use, e.g. 7.6
    memory_bytes: int = 0


@dataclass
class LogLine(Event):
    name: str = ""
    text: str = ""


@dataclass
class SampleFinished(Event):
    outcome: Optional[SampleOutcome] = None


@dataclass
class BatchProgress(Event):
    """Where the batch stands; posted when a sample starts and when one ends.

    ``position`` is the 1-based number of the sample now running (0 before the first).
    ``eta_seconds`` is a rough time left for the whole batch, None until one sample has
    finished and there is something to estimate from.
    """
    position: int = 0
    total: int = 0
    done: int = 0
    failed: int = 0
    eta_seconds: Optional[float] = None


@dataclass
class Notice(Event):
    """A non-blocking bar on the Run page. ``kind``: battery, memory, slept, info."""
    kind: str = "info"
    text: str = ""
    level: str = "warn"              # "info" | "warn" | "bad"; "" text clears the bar of that kind


@dataclass
class BatchFinished(Event):
    reason: EndReason = EndReason.COMPLETED
    message: str = ""                # one line for the header, in plain words
    counts: Dict[str, int] = field(default_factory=dict)   # RunStatus value -> number of samples
    seconds: float = 0.0
