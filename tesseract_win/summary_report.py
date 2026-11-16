# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The two summaries of a results folder: ``run_summary.tsv`` and ``run_summary.html``.

Both are rebuilt from scratch after every sample: from the status files in the folder (so a
summary never forgets yesterday's samples) plus what the run in hand knows and the folder
cannot yet show (failures, samples left out, samples still waiting).

``run_summary.html`` is the one to read: one self-contained page, no script, nothing fetched,
relative links only, so it still works after the folder is moved to another PC or a USB stick.
``run_summary.tsv`` is the one for other programs: whole numbers, no thousands separators.

Writing a summary must never make a run fail. :func:`write_run_summary` catches everything,
logs it and reports which file it could not write.
"""

from __future__ import annotations

import dataclasses
import datetime
import html
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from . import branding, fsutil, results, versions
from .model import EndReason, RunStatus, SampleOutcome
from .results import DONE_STATUSES, SampleRecord

__all__ = ["write_run_summary", "STATUS_WORDS", "TSV_COLUMNS"]

log = logging.getLogger(__name__)

#: The status words of the Run page (audit, "Status words").
STATUS_WORDS: Dict[RunStatus, str] = {
    RunStatus.WAITING: "Waiting",
    RunStatus.JOINING_LANES: "Joining lanes",
    RunStatus.TRIMMING: "Trimming",
    RunStatus.CHECKING_READS: "Checking reads",
    RunStatus.ASSEMBLING: "Assembling",
    RunStatus.SUMMARISING: "Summarising",
    RunStatus.DONE: "Done",
    RunStatus.DONE_NOTES: "Done (check notes)",
    RunStatus.FAILED: "Failed",
    RunStatus.SKIPPED: "Skipped (already assembled)",
    RunStatus.STOPPED: "Stopped",
    RunStatus.NOT_STARTED: "Not started",
    RunStatus.LEFT_OUT: "Left out",
}

TSV_COLUMNS = ("sample", "status", "contigs", "total_length", "largest", "N50", "depth", "trimmed",
               "settings", "program_version", "TesserACT_version", "scepter_version", "finished",
               "seconds", "note")

# The order in which statuses are counted and shown, and the CSS class of each.
_TALLY_ORDER: Tuple[Tuple[str, str], ...] = (
    ("done", "Done"), ("notes", "Done (check notes)"), ("failed", "Failed"),
    ("skipped", "Skipped (already assembled)"), ("stopped", "Stopped"),
    ("notstarted", "Not started"), ("leftout", "Left out"), ("running", "Running now"),
    ("waiting", "Waiting"),
)
_KIND_OF = {
    RunStatus.DONE: "done", RunStatus.DONE_NOTES: "notes", RunStatus.FAILED: "failed",
    RunStatus.SKIPPED: "skipped", RunStatus.STOPPED: "stopped",
    RunStatus.NOT_STARTED: "notstarted", RunStatus.LEFT_OUT: "leftout",
    RunStatus.WAITING: "waiting",
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# One writer at a time: the batch thread writes after every sample and the window may ask for the
# summary to be written again. Two at once would share fsutil's temporary file.
_WRITE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- small formatters

def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _parse_time(text: Any) -> Optional[datetime.datetime]:
    if isinstance(text, datetime.datetime):
        return text
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _when(text: Any) -> str:
    """``17 Nov 2026, 03:42`` whatever the language of the PC; the raw text if it is no date."""
    moment = _parse_time(text)
    if moment is None:
        return "" if text is None else str(text)
    return "%d %s %d, %02d:%02d" % (moment.day, _MONTHS[moment.month - 1], moment.year,
                                    moment.hour, moment.minute)


def _day(text: Any) -> str:
    """``3 Nov 2026``."""
    moment = _parse_time(text)
    if moment is None:
        return "" if text is None else str(text)
    return "%d %s %d" % (moment.day, _MONTHS[moment.month - 1], moment.year)


def _took(seconds: Any) -> str:
    """A length of time in words: ``9 h 14 min``, ``6 min 41 s``, ``42 s``."""
    try:
        whole = max(0, int(round(float(seconds))))
    except (TypeError, ValueError, OverflowError):
        return ""
    if whole >= 3600:
        return "%d h %02d min" % (whole // 3600, (whole % 3600) // 60)
    if whole >= 60:
        return "%d min %02d s" % (whole // 60, whole % 60)
    return "%d s" % whole


def _number(value: Optional[int]) -> str:
    return "" if value is None else "{:,}".format(value)


def _plural(count: int, one: str, many: str) -> str:
    return "%s %s" % ("{:,}".format(count), one if count == 1 else many)


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def _sentence(text: str) -> str:
    text = _one_line(text)
    return text if not text or text[-1] in ".!?" else text + "."


def _url(*parts: str) -> str:
    """A relative link from the results folder; every part escaped for a URL.

    Nothing a name contains can turn the link into something else: ``:``, ``/``, ``#``, ``?`` and
    ``%`` are escaped, and a part made of dots (``..``) is escaped too, so the link stays inside
    the results folder.
    """
    return "/".join(quote(part, safe="").replace(".", "%2E") if not part.strip(".") else quote(part, safe="")
                    for part in parts if part)


def _relative_url(results_dir: str, path: str) -> str:
    """A relative link to ``path`` when it is a file or folder inside the results folder, else ""."""
    if not path:
        return ""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(results_dir))
    except ValueError:
        return ""
    if os.altsep:
        rel = rel.replace(os.altsep, os.sep)
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    if not parts or parts[0] == ".." or not os.path.exists(path):
        return ""
    return _url(*parts)


# --------------------------------------------------------------------------- what the pages show

@dataclass
class _Row:
    """One line of the table on the page."""
    name: str
    kind: str                          # CSS class and tally key: done, notes, failed, ...
    words: str                         # the status in words
    group: str                         # "run" or "earlier"
    contigs: Optional[int] = None
    total_length: Optional[int] = None
    n50: Optional[int] = None
    depth: Optional[int] = None
    trimmed: Optional[bool] = None
    note: str = ""
    notes: List[str] = field(default_factory=list)
    next_step: str = ""
    small: str = ""                    # a quieter second line under the note
    report: str = ""
    trimming: str = ""
    folder: str = ""
    log: str = ""
    anchor: str = ""


@dataclass
class _Summary:
    results_dir: str
    rows: List[_Row]
    tsv: List[List[str]]
    info: Dict[str, Any]
    tally: Dict[str, int]
    assemblies: int
    written: str
    earlier: int
    has_tsv_link: bool = True
    has_log: bool = False


def _moment(value: Any) -> Any:
    """A time the page can show (ISO text or a datetime); None for anything else."""
    return value if isinstance(value, datetime.datetime) or (isinstance(value, str) and value) else None


def _listed(value: Any) -> List[Any]:
    """The items of whatever was handed in as a list; [] for what cannot be gone through."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return list(value.items())
    if value is None or isinstance(value, (bytes, bytearray)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _info(run_info: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    raw = dict(run_info) if isinstance(run_info, Mapping) else {}

    def pick(*keys: str) -> Any:
        for key in keys:
            if raw.get(key) is not None:
                return raw[key]
        return None

    reason = pick("end_reason", "reason")
    if isinstance(reason, EndReason):
        reason = reason.value
    events = [e for e in _listed(pick("events", "happened", "on_the_way")) if isinstance(e, str)]
    current = pick("current")
    if isinstance(current, (tuple, list)):
        current = current[0] if current else None
    finished = pick("finished", "finished_at", "ended_at")
    return {
        "started": _moment(pick("started", "started_at")),
        # None means "still running". An end that was given but cannot be shown is still an end.
        "finished": None if finished is None else (_moment(finished) or ""),
        "seconds": pick("seconds"),
        "reason": reason if isinstance(reason, str) else None,
        "message": _one_line(pick("message")) if isinstance(pick("message"), str) else "",
        "settings": _one_line(pick("settings", "settings_line"))
        if isinstance(pick("settings", "settings_line"), str) else "",
        "events": [_one_line(e) for e in events if _one_line(e)],
        "samples": list(dict.fromkeys(n for n in _listed(pick("samples")) if isinstance(n, str) and n)),
        "current": current if isinstance(current, str) else None,
    }


def _kind(status: RunStatus) -> str:
    return "running" if status.is_running else _KIND_OF.get(status, "waiting")


def _joined_notes(note: str, notes: Sequence[str]) -> str:
    pieces = [_sentence(note)] if _one_line(note) else []
    for extra in notes:
        if _one_line(extra) and _one_line(extra) != _one_line(note):
            pieces.append(_sentence(extra))
    return " ".join(pieces)


def _made_with(record: SampleRecord) -> str:
    """How an earlier result was made, for the quiet second line of its row."""
    pieces = []
    if record.settings_text:
        pieces.append(_one_line(record.settings_text))
    if record.tesseract_version and record.tesseract_version != versions.TESSERACT_VERSION:
        pieces.append("TesserACT %s" % record.tesseract_version)
    if record.trimmed and record.scepter_version and record.scepter_version != versions.SCEPTER_VERSION:
        pieces.append("scepter %s" % record.scepter_version)
    made = "Made on %s" % _day(record.finished_at) if record.finished_at else "Made earlier"
    return "%s (%s)." % (made, "; ".join(pieces)) if pieces else made + "."


def _fill_from_record(row: _Row, record: SampleRecord) -> None:
    out = record.outcome
    row.contigs, row.total_length, row.n50, row.depth = out.contigs, out.total_length, out.n50, out.depth
    row.trimmed = record.trimmed


def _links(row: _Row, results_dir: str, outcome: Optional[SampleOutcome],
           record: Optional[SampleRecord]) -> None:
    name = record.name if record is not None else row.name       # the folder as it is spelled on disk
    if outcome is not None and outcome.report_html:
        row.report = _relative_url(results_dir, outcome.report_html)
    if not row.report and record is not None and record.outcome.report_html:
        row.report = _url(name, results.ASSEMBLY, results.REPORT_HTML)
    if (record is not None and record.trimmed) or (outcome is not None and outcome.trimmed):
        if os.path.isfile(results.trimming_report_html_path(results_dir, name)):
            row.trimming = _url(name, results.TRIMMING, results.TRIMMING_REPORT_HTML)
    if record is not None or os.path.isdir(results.sample_dir(results_dir, name)):
        row.folder = _url(name) + "/"
    if row.kind == "failed":
        if outcome is not None and outcome.log:
            # The outcome may still name the log inside _in_progress, which is gone by now.
            row.log = (_relative_url(results_dir, outcome.log)
                       or _relative_url(results_dir, results.kept_log_path(results_dir, name, outcome.log)))
        if not row.log and record is not None and not record.done and record.outcome.log:
            row.log = _relative_url(results_dir, record.outcome.log)


def _run_row(results_dir: str, outcome: SampleOutcome, record: Optional[SampleRecord]) -> _Row:
    status = outcome.status
    row = _Row(name=outcome.name, kind=_kind(status), words=STATUS_WORDS.get(status, status.value),
               group="run", note=_one_line(outcome.note),
               notes=[_one_line(n) for n in outcome.notes if _one_line(n)],
               next_step=_one_line(outcome.next_step))
    if status in DONE_STATUSES:
        row.contigs, row.total_length = outcome.contigs, outcome.total_length
        row.n50, row.depth, row.trimmed = outcome.n50, outcome.depth, outcome.trimmed
        if row.contigs is None and record is not None and record.assembled:
            _fill_from_record(row, record)
    elif status is RunStatus.SKIPPED and record is not None and record.assembled:
        _fill_from_record(row, record)
        if not row.note:
            row.note = "Already assembled on %s." % _day(record.finished_at) \
                if record.finished_at else "Already assembled."
    elif status in (RunStatus.FAILED, RunStatus.STOPPED):
        kept = outcome.kept_earlier_result or (record is not None and record.assembled)
        if kept and "kept" not in row.note.lower():
            row.small = "The earlier assembly%s was kept." % (
                " (%s)" % _day(record.finished_at) if record is not None and record.finished_at else "")
    _links(row, results_dir, outcome, record)
    return row


def _earlier_row(results_dir: str, record: SampleRecord) -> _Row:
    status = record.status
    row = _Row(name=record.name, kind=_kind(status), words=STATUS_WORDS.get(status, status.value),
               group="earlier", note=_one_line(record.outcome.note),
               notes=[_one_line(n) for n in record.outcome.notes if _one_line(n)],
               next_step=_one_line(record.outcome.next_step))
    if record.done:
        _fill_from_record(row, record)
        row.small = _made_with(record)
        later = record.last_attempt
        if later and later.get("status") == RunStatus.FAILED.value:
            row.small += " A later attempt%s failed; this result was kept." % (
                " (%s)" % _day(later.get("finished_at")) if later.get("finished_at") else "")
        if not record.assembly_present:
            row.small = "The file contigs.fasta is no longer in this sample's assembly folder. " + row.small
    else:
        pieces = ["Tried on %s." % _day(record.finished_at)] if record.finished_at else []
        if status is RunStatus.FAILED and row.next_step:
            pieces.append("What to do: " + _sentence(row.next_step))
        row.small = " ".join(pieces)
    _links(row, results_dir, None, record)
    return row


# ----- the TSV

def _tsv_cell(value: Any) -> str:
    return _one_line(value)


def _tsv_version(text: str) -> str:
    # "1.6.2" alone is read as a date by Excel on many PCs; "v1.6.2" never is.
    return "v" + text if text else ""


def _tsv_from_record(record: SampleRecord, note: Optional[str] = None) -> List[str]:
    out = record.outcome

    def whole(value: Optional[int]) -> str:
        return "" if value is None or not record.done else str(int(value))

    text = _joined_notes(out.note, out.notes) if note is None else note
    if not record.done and out.next_step and note is None:
        text = (text + " " + _sentence(out.next_step)).strip()
    if record.done and not record.assembly_present:
        text = ("contigs.fasta is no longer in this sample's assembly folder. " + text).strip()
    seconds = str(int(round(out.seconds))) if out.seconds and out.seconds > 0 else ""
    return [record.name, record.status.value, whole(out.contigs), whole(out.total_length),
            whole(out.largest), whole(out.n50), whole(out.depth), "yes" if record.trimmed else "no",
            _tsv_cell(record.settings_text), _tsv_version(record.program_version),
            _tsv_version(record.tesseract_version), _tsv_version(record.scepter_version),
            _tsv_cell(record.finished_at), seconds, _tsv_cell(text)]


def _tsv_from_outcome(outcome: SampleOutcome, status_value: str) -> List[str]:
    """A row for a sample of this run that has no status file in the folder."""
    text = _joined_notes(outcome.note, outcome.notes)
    if outcome.next_step:
        text = (text + " " + _sentence(outcome.next_step)).strip()
    seconds = str(int(round(outcome.seconds))) if outcome.seconds and outcome.seconds > 0 else ""
    done = outcome.status in DONE_STATUSES      # its result could not be put in place (new_result)
    attempted = done or outcome.status in (RunStatus.FAILED, RunStatus.STOPPED) or outcome.status.is_running

    def whole(value: Optional[int]) -> str:
        return str(int(value)) if done and isinstance(value, int) and not isinstance(value, bool) else ""

    made_with = [_tsv_version(versions.APP_VERSION), _tsv_version(versions.TESSERACT_VERSION),
                 _tsv_version(versions.SCEPTER_VERSION)] if done else ["", "", ""]
    return [outcome.name, status_value, whole(outcome.contigs), whole(outcome.total_length),
            whole(outcome.largest), whole(outcome.n50), whole(outcome.depth),
            ("yes" if outcome.trimmed else "no") if attempted else "", ""] + made_with + [
            _tsv_cell(outcome.finished_at), seconds, _tsv_cell(text)]


def _tsv_rows(records: Mapping[str, SampleRecord], outcomes: Mapping[str, SampleOutcome],
              waiting: Mapping[str, str], left_out: Sequence[Tuple[str, str]]) -> List[List[str]]:
    keyed: List[Tuple[Any, int, List[str]]] = []
    for name, record in records.items():
        outcome = outcomes.get(name)
        note: Optional[str] = None
        if outcome is not None:
            latest = _joined_notes(outcome.note, outcome.notes)
            if outcome.status in DONE_STATUSES and latest:
                note = latest
            elif outcome.status in (RunStatus.FAILED, RunStatus.STOPPED) and record.assembled:
                what = "failed" if outcome.status is RunStatus.FAILED else "was stopped"
                note = "The latest attempt %s%s The earlier assembly was kept." % (
                    what, (": " + latest) if latest else ".")
        keyed.append((fsutil.natural_key(name), 0, _tsv_from_record(record, note)))
    for name, outcome in outcomes.items():
        if name not in records and outcome.status is not RunStatus.LEFT_OUT:
            value = "running" if outcome.status.is_running else outcome.status.value
            keyed.append((fsutil.natural_key(name), 1, _tsv_from_outcome(outcome, value)))
    for name, value in waiting.items():
        if name not in records and name not in outcomes:
            keyed.append((fsutil.natural_key(name), 1,
                          [name, value] + [""] * (len(TSV_COLUMNS) - 2)))
    for name, reason in left_out:
        keyed.append((fsutil.natural_key(name), 2,
                      [name, RunStatus.LEFT_OUT.value] + [""] * (len(TSV_COLUMNS) - 3)
                      + [_tsv_cell(reason)]))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in keyed]


def _render_tsv(summary: _Summary) -> str:
    lines = ["\t".join(TSV_COLUMNS)]
    lines.extend("\t".join(row) for row in summary.tsv)
    return "\n".join(lines) + "\n"


# ----- gathering

_TEXT_FIELDS = ("note", "next_step", "finished_at", "folder", "report_html", "log")
_NUMBER_FIELDS = ("contigs", "total_length", "largest", "n50", "depth")


def _usable(outcome: Any) -> Optional[SampleOutcome]:
    """A copy of an outcome in which every field is of the kind the pages expect.

    One value of the wrong kind (a number given as text, notes that are None) must not cost the
    whole summary. None for something that is no outcome at all.
    """
    if not isinstance(outcome, SampleOutcome) or not isinstance(outcome.name, str) or not outcome.name:
        return None
    try:
        status = RunStatus(outcome.status)
    except ValueError:
        return None
    clean = dataclasses.replace(outcome, status=status)
    for name in _TEXT_FIELDS:
        if not isinstance(getattr(clean, name), str):
            setattr(clean, name, "")
    for name in _NUMBER_FIELDS:
        setattr(clean, name, results.whole_number(getattr(clean, name)))
    notes = clean.notes if isinstance(clean.notes, (list, tuple)) else []
    clean.notes = [n for n in notes if isinstance(n, str)]
    clean.seconds = results.seconds_of(clean.seconds)
    return clean


def _left_out(left_out: Any) -> List[Tuple[str, str]]:
    """``(name, reason)`` pairs from whatever was handed in; what is no such pair is logged."""
    pairs: List[Tuple[str, str]] = []
    for item in _listed(left_out):
        parts = [item] if isinstance(item, str) else _listed(item)
        if not parts or not isinstance(parts[0], str) or not parts[0]:
            log.error("the run summary was given a left-out sample it cannot show: %r", item)
            continue
        reason = parts[1] if len(parts) > 1 and isinstance(parts[1], str) else ""
        pairs.append((parts[0], _one_line(reason)))
    return pairs


def _gather(results_dir: str, this_run: Iterable[SampleOutcome],
            left_out: Iterable[Tuple[str, str]], run_info: Optional[Mapping[str, Any]]) -> _Summary:
    info = _info(run_info)
    records = {record.name: record for record in results.all_records(results_dir)}
    outcomes: Dict[str, SampleOutcome] = {}
    running = info["finished"] is None
    for given in _listed(this_run):
        outcome = _usable(given)
        if outcome is None:
            log.error("the run summary was given something that is no sample outcome: %r", given)
            continue
        if outcome.status is RunStatus.WAITING and not running:
            outcome = SampleOutcome(name=outcome.name, status=RunStatus.NOT_STARTED, note=outcome.note)
        outcomes.pop(outcome.name, None)             # a sample tried twice: the last word counts
        outcomes[outcome.name] = outcome
    left = _left_out(left_out)
    left_names = {name for name, _reason in left}
    left += [(o.name, _one_line(o.note)) for o in outcomes.values()
             if o.status is RunStatus.LEFT_OUT and o.name not in left_names]
    left_names = {name for name, _reason in left}
    # A sample of this run is matched to the folder that is really there, however that is spelled:
    # on Windows KP_1 and kp_1 are one folder, and the sample must not be listed a second time
    # as a result of an earlier run. (normcase changes nothing where capitals count.)
    spelled = {os.path.normcase(name): name for name in records}
    for name in list(outcomes) + info["samples"]:
        on_disk = spelled.get(os.path.normcase(name))
        if name not in records and on_disk in records:
            records[name] = records.pop(on_disk)
    for outcome in outcomes.values():
        if outcome.thin_retry:
            event = ("%s had very little data: its assembly was made on a second attempt, "
                     "with settings for thin data." % outcome.name)
            if event not in info["events"]:
                info["events"].append(event)

    order = [n for n in info["samples"] if n in outcomes and outcomes[n].status is not RunStatus.LEFT_OUT]
    order += [n for n, o in outcomes.items() if n not in order and o.status is not RunStatus.LEFT_OUT]
    # A caller may list every row of the Samples page: a name that was left out is not waiting too.
    listed = [n for n in info["samples"] if n not in outcomes and n not in left_names]
    waiting_status = RunStatus.WAITING if running else RunStatus.NOT_STARTED
    waiting: Dict[str, str] = {}

    rows: List[_Row] = []
    by_position = {n: i for i, n in enumerate(info["samples"])}
    pending_rows: List[Tuple[int, _Row]] = []
    for position, name in enumerate(order):
        pending_rows.append((by_position.get(name, len(by_position) + position),
                             _run_row(results_dir, outcomes[name], records.get(name))))
    for name in listed:
        if name == info["current"] and running:
            row = _Row(name=name, kind="running", words="Running now", group="run")
            waiting[name] = "running"
        else:
            row = _Row(name=name, kind=_kind(waiting_status), words=STATUS_WORDS[waiting_status], group="run")
            waiting[name] = waiting_status.value
        record = records.get(name)
        if record is not None and record.assembled:
            # Chosen to be assembled again and not reached (yet): what is in the folder still counts.
            row.small = "The earlier assembly%s is still in place." % (
                " (%s)" % _day(record.finished_at) if record.finished_at else "")
        _links(row, results_dir, None, record)
        pending_rows.append((by_position[name], row))
    pending_rows.sort(key=lambda item: item[0])
    rows.extend(row for _, row in pending_rows)
    for name, reason in left:
        rows.append(_Row(name=name, kind="leftout", words=STATUS_WORDS[RunStatus.LEFT_OUT],
                         group="run", note=reason))
    in_run = set(outcomes) | set(listed)
    earlier = [record for name, record in records.items() if name not in in_run]
    rows.extend(_earlier_row(results_dir, record) for record in earlier)
    for index, row in enumerate(rows, 1):
        row.anchor = "r%d" % index

    tally: Dict[str, int] = {}
    for row in rows:
        if row.group == "run":
            tally[row.kind] = tally.get(row.kind, 0) + 1
    return _Summary(
        results_dir=results_dir, rows=rows,
        tsv=_tsv_rows(records, outcomes, waiting, left), info=info, tally=tally,
        assemblies=results.count_assemblies(results_dir), written=fsutil.now_iso(),
        earlier=len(earlier), has_log=os.path.isfile(results.run_log_path(results_dir)))


# --------------------------------------------------------------------------- the page

# The colours are the window's own two palettes (gui/theme.py), repeated here because this
# module must work without Tk. Light and dark follow the reader's system; print is always light.
_CSS = """
:root{--page:#ecf0f6;--card:#ffffff;--text:#121820;--muted:#5b6573;--line:#c9d2e0;
--accent:#2563eb;--ok:#0f7032;--warn:#8a5300;--bad:#b42318;--ok-wash:#e7f5ec;--accent-wash:#e9f0fe}
@media (prefers-color-scheme:dark){:root{--page:#111419;--card:#191d24;--text:#e9edf4;
--muted:#98a3b3;--line:#38424f;--accent:#6ea0ff;--ok:#4ade80;--warn:#fbbf24;--bad:#fb7185;
--ok-wash:#14261c;--accent-wash:#1d2836}}
*{box-sizing:border-box}
html{color-scheme:light dark;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--text);
font:1rem/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif}
a{color:var(--accent);text-underline-offset:.15em}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}
.tone{height:8px;background:var(--tone)}
.tone-ok{--tone:var(--ok)}.tone-warn{--tone:var(--warn)}.tone-bad{--tone:var(--bad)}.tone-run{--tone:var(--accent)}
.wrap{max-width:80rem;margin:0 auto;padding:1.75rem 1.5rem 3rem}
.what{margin:0;color:var(--muted);font-size:.9375rem}
h1{margin:.35rem 0 .5rem;max-width:26em;font-size:clamp(1.5rem,1.05rem + 1.9vw,2.125rem);
line-height:1.2;font-weight:600;letter-spacing:-.012em}
.times{margin:0;max-width:44em;color:var(--muted)}
h2{margin:2.5rem 0 .75rem;font-size:1.1875rem;line-height:1.3;font-weight:600}
p{margin:.5rem 0;max-width:44em}
.plate{display:flex;flex-wrap:wrap;gap:5px;margin:1.5rem 0 .75rem;max-width:62rem}
.well,.dot{display:inline-block;flex:none;border-radius:50%;border:2px solid var(--c);
background:var(--c);color:var(--card);text-align:center;font-weight:700;text-decoration:none;
-webkit-print-color-adjust:exact;print-color-adjust:exact}
.well{width:20px;height:20px;font-size:13px;line-height:16px}
.plate.few{gap:7px}
.plate.few .well{width:28px;height:28px;font-size:18px;line-height:24px}
.plate.many{gap:3px}
.plate.many .well{width:13px;height:13px;font-size:0;line-height:0}
.dot{width:.8em;height:.8em;margin-right:.45em;font-size:1em;vertical-align:-.04em}
.well:hover,.well:focus-visible{outline:2px solid var(--text);outline-offset:1px}
.done{--c:var(--ok)}.notes{--c:var(--warn)}.failed{--c:var(--bad)}.running{--c:var(--accent)}
.skipped{--c:var(--ok)}.stopped{--c:var(--warn)}.notstarted,.waiting,.leftout{--c:var(--muted)}
.well.skipped,.well.stopped,.well.notstarted,.well.waiting,.well.leftout,
.dot.skipped,.dot.stopped,.dot.notstarted,.dot.waiting,.dot.leftout{background:transparent;color:var(--c)}
.well.waiting,.dot.waiting{border-style:dotted}
.well.leftout,.dot.leftout{border-style:dashed}
.well.failed::after{content:"\\00d7"}.well.notes::after{content:"!"}
.tally{display:flex;flex-wrap:wrap;gap:.25rem 1.5rem;margin:0;padding:0;list-style:none}
.tally b{font-weight:600;font-variant-numeric:tabular-nums}
.tally .sum{color:var(--muted)}
.issue{margin:.75rem 0;padding:.85rem 1.1rem;max-width:56rem;background:var(--card);
border:1px solid var(--line);border-left:5px solid var(--c);border-radius:4px}
.issue h3{margin:0;font-size:1rem;font-weight:600}
.issue h3 span{font-weight:400;color:var(--c)}
.issue p{margin:.3rem 0 0}
.issue ul{margin:.3rem 0 0;padding-left:1.2rem;max-width:44em}
.issue .do{font-weight:600}
.issue .open{font-size:.9375rem}
.issue .open a{margin-right:1rem}
.calm{padding:.7rem 1rem;max-width:56rem;background:var(--ok-wash);border-radius:4px}
.plain{margin:.4rem 0;padding-left:1.2rem;max-width:44em}
.plain li{margin:.25rem 0}
.sheet{background:var(--card);border:1px solid var(--line);border-radius:6px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.9375rem}
th,td{padding:.5rem .65rem;text-align:left;vertical-align:top;border-top:1px solid var(--line)}
thead th{border-top:0;border-bottom:2px solid var(--line);font-weight:600;white-space:nowrap;vertical-align:bottom}
tbody th{font-weight:600;overflow-wrap:anywhere;min-width:7rem}
.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.st{min-width:7.5rem}
.st>span{display:flex;align-items:flex-start}
.st .dot{margin-top:.4em}
.st.failed{color:var(--bad);font-weight:600}.st.notes{color:var(--warn);font-weight:600}
.st.done{color:var(--ok)}.st.leftout,.st.notstarted,.st.waiting{color:var(--muted)}
.note{min-width:11rem;overflow-wrap:anywhere}
.note small{display:block;color:var(--muted);font-size:.93em}
.quiet{color:var(--muted);font-size:.875rem}
.links{font-size:.93em}
.links a{display:inline-block;margin-right:.6rem;white-space:nowrap}
.links a:last-child{margin-right:0}
@media (min-width:66rem){.links{white-space:nowrap}}
@media (min-width:82rem){.st{white-space:nowrap}}
.group th{padding-top:1.1rem;background:var(--page);border-top:0;font-weight:600;color:var(--muted)}
.sheet .group:first-child th{padding-top:.6rem}
tr:target>*{background:var(--accent-wash)}
.words{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:.4rem 2rem;
margin:.9rem 0 0;font-size:.875rem;color:var(--muted)}
.words div{margin:0}
.words dt{display:inline;font-weight:600;color:var(--text)}
.words dd{display:inline;margin:0}
.methods{margin:.5rem 0;padding:.85rem 1.1rem;max-width:56rem;background:var(--card);
border:1px solid var(--line);border-radius:4px;-webkit-user-select:all;user-select:all}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);font-size:.875rem}
footer p{margin:.15rem 0;max-width:none}
@media (max-width:48rem){.wrap{padding:1.25rem 1rem 2rem}th,td{padding:.45rem .5rem}
.st,.links{white-space:nowrap}}
@media print{:root{--page:#fff;--card:#fff;--text:#000;--muted:#3c4350;--line:#b9c0cc;
--accent:#1d4ed8;--ok:#0f7032;--warn:#8a5300;--bad:#b42318;--ok-wash:#fff;--accent-wash:#fff}
@page{margin:12mm}
html{font-size:10pt}
.wrap{max-width:none;padding:0}
.tone{-webkit-print-color-adjust:exact;print-color-adjust:exact;margin-bottom:6mm}
a{color:inherit;text-decoration:none}
.links,.open,.refresh{display:none}
.sheet{border:0;overflow:visible}
table{font-size:.9rem}
thead{display:table-header-group}
tr,.issue{break-inside:avoid}
h2{break-after:avoid;margin-top:1.8rem}
footer{margin-top:1.5rem}
.methods{-webkit-user-select:auto;user-select:auto}}
"""


def _counts_in_words(tally: Mapping[str, int]) -> str:
    return ", ".join("%s %s" % ("{:,}".format(tally[kind]), words.lower())
                     for kind, words in _TALLY_ORDER if tally.get(kind))


def _finished_so_far(tally: Mapping[str, int]) -> int:
    return sum(tally.get(kind, 0) for kind in ("done", "notes", "failed", "skipped", "stopped"))


def _tone(summary: _Summary) -> str:
    tally, info = summary.tally, summary.info
    if info["finished"] is None and (info["started"] or summary.tally):
        return "run"
    if info["reason"] == EndReason.ENVIRONMENT.value:
        return "bad"
    good = tally.get("done", 0) + tally.get("skipped", 0)
    total = sum(tally.values())
    if total and tally.get("failed", 0) and not good and not tally.get("notes", 0):
        return "bad"
    return "ok" if total == good else "warn"


def _headline(summary: _Summary) -> str:
    info, tally = summary.info, summary.tally
    if info["message"]:
        return info["message"]
    total = sum(tally.values())
    counts = _counts_in_words(tally)
    if not total:
        return "The results in this folder"
    if info["finished"] is None:
        return "The run is in progress: %d of %s finished so far." % (
            _finished_so_far(tally), _plural(total - tally.get("leftout", 0), "sample", "samples"))
    if info["reason"] == EndReason.ENVIRONMENT.value:
        return "The run was stopped by a problem with this PC or the results folder: %s." % counts
    if info["reason"] == EndReason.STOPPED.value:
        return "The run was stopped: %s." % counts
    return "The run has finished: %s." % counts


def _times_line(summary: _Summary) -> str:
    info = summary.info
    pieces = []
    took = _took(info["seconds"]) if info["seconds"] is not None else ""
    if info["finished"] is not None:
        if info["finished"]:
            pieces.append("Finished %s." % _when(info["finished"]))
        if took:
            pieces.append("It took %s." % took)
        if info["started"]:
            pieces.append("Started %s." % _when(info["started"]))
    elif info["started"] or summary.tally:
        if info["started"]:
            pieces.append("Started %s." % _when(info["started"]))
        if took:
            pieces.append("Running for %s so far." % took)
        pieces.append("This page is rewritten after every sample and reloads by itself; "
                      "it was last written %s." % _when(summary.written))
    return " ".join(pieces)


def _status_cell(row: _Row) -> str:
    return ('<td class="st %s"><span><span class="dot %s"></span><span>%s</span></span></td>'
            % (row.kind, row.kind, _esc(row.words)))


def _note_cell(row: _Row) -> str:
    text = row.note or (row.notes[0] if row.notes else "")
    others = [n for n in row.notes if n != text]
    if row.group == "earlier":
        # Only this run's samples are listed under "What needs your attention": for the others
        # this cell is the one place on the page for their notes, so it carries them all.
        parts = [_esc(_joined_notes(text, others))]
    else:
        parts = [_esc(text)]
        if others and row.kind == "notes":
            parts.append(' <a href="#attention">Plus %s.</a>'
                         % _plural(len(others), "more note", "more notes"))
    if row.small:
        parts.append("<small>%s</small>" % _esc(row.small))
    return '<td class="note">%s</td>' % "".join(parts)


def _link_cell(row: _Row) -> str:
    links = []
    if row.report:
        links.append('<a href="%s">Report</a>' % _esc(row.report))
    if row.log:
        links.append('<a href="%s">Log</a>' % _esc(row.log))
    if row.trimming:
        links.append('<a href="%s">Trimming</a>' % _esc(row.trimming))
    if row.folder:
        links.append('<a href="%s">Folder</a>' % _esc(row.folder))
    return '<td class="links">%s</td>' % "".join(links)


def _table(summary: _Summary) -> List[str]:
    out = ['<div class="sheet"><table>',
           '<thead><tr><th scope="col">Sample</th><th scope="col">Status</th>'
           '<th scope="col" class="n">Contigs</th><th scope="col" class="n">Total length</th>'
           '<th scope="col" class="n">N50</th><th scope="col" class="n">Depth</th>'
           '<th scope="col">Note</th><th scope="col" class="links">Open</th></tr></thead>', "<tbody>"]
    in_run = sum(1 for row in summary.rows if row.group == "run")
    group = None
    for row in summary.rows:
        if row.group != group:
            group = row.group
            if group == "run" and summary.earlier:
                out.append('<tr class="group"><th colspan="8">This run: %s</th></tr>'
                           % _esc(_plural(in_run, "sample", "samples")))
            elif group == "earlier":
                out.append('<tr class="group"><th colspan="8">Already in this folder from earlier runs: '
                           "%s</th></tr>" % _esc(_plural(summary.earlier, "sample", "samples")))
        depth = "" if row.depth is None else _number(row.depth) + "&times;"
        out.append('<tr id="%s"><th scope="row">%s</th>%s<td class="n">%s</td><td class="n">%s</td>'
                   '<td class="n">%s</td><td class="n">%s</td>%s%s</tr>'
                   % (row.anchor, _esc(row.name), _status_cell(row), _number(row.contigs),
                      _number(row.total_length), _number(row.n50), depth, _note_cell(row),
                      _link_cell(row)))
    out.append("</tbody></table></div>")
    out.append('<dl class="words">'
               "<div><dt>Contigs</dt> <dd>The number of pieces in the assembly. Fewer is usually better."
               "</dd></div>"
               "<div><dt>Total length</dt> <dd>All contigs added together, in bases. It should be close to "
               "the genome size of your organism.</dd></div>"
               "<div><dt>N50</dt> <dd>Half of the assembly is in contigs at least this long. "
               "Larger is better.</dd></div>"
               "<div><dt>Depth</dt> <dd>How many times each base of the genome was read, on average."
               "</dd></div>"
               "</dl>")
    return out


def _open_links(row: _Row) -> str:
    links = []
    if row.log:
        links.append('<a href="%s">Open the log</a>' % _esc(row.log))
    if row.report:
        links.append('<a href="%s">Open the report</a>' % _esc(row.report))
    if row.folder:
        links.append('<a href="%s">Open the folder</a>' % _esc(row.folder))
    links.append('<a href="#%s">Show in the table</a>' % row.anchor)
    return '<p class="open">%s</p>' % "".join(links)


def _attention(summary: _Summary) -> List[str]:
    run = [row for row in summary.rows if row.group == "run"]
    if not run:
        return []
    out = ['<h2 id="attention">What needs your attention</h2>']
    failed = [row for row in run if row.kind == "failed"]
    noted = [row for row in run if row.kind == "notes"]
    stopped = sum(1 for row in run if row.kind == "stopped")
    not_started = sum(1 for row in run if row.kind == "notstarted")
    left = [row for row in run if row.kind == "leftout"]
    if not (failed or noted or stopped or not_started or left):
        if summary.info["finished"] is None:
            out.append('<p class="calm">Nothing so far. Samples still waiting are assembled one after '
                       "the other.</p>")
        else:
            out.append('<p class="calm">Nothing. Every sample in this run has an assembly.</p>')
        return out
    for row in failed:
        out.append('<div class="issue failed"><h3>%s <span>failed</span></h3>' % _esc(row.name))
        out.append("<p>%s</p>" % _esc(row.note or "No reason was recorded. The log has the details."))
        if row.next_step:
            out.append('<p><span class="do">What to do:</span> %s</p>' % _esc(row.next_step))
        if row.small:
            out.append("<p>%s</p>" % _esc(row.small))
        out.append(_open_links(row) + "</div>")
    for row in noted:
        notes = row.notes or ([row.note] if row.note else [])
        if row.note and row.note not in notes:
            notes = [row.note] + notes
        out.append('<div class="issue notes"><h3>%s <span>is done, with notes to read</span></h3>'
                   % _esc(row.name))
        if notes:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _esc(n) for n in notes))
        else:
            out.append("<p>The sample's report has the details.</p>")
        out.append(_open_links(row) + "</div>")
    if stopped or not_started:
        pieces = []
        if stopped:
            pieces.append("%s stopped" % _plural(stopped, "sample was", "samples were"))
        if not_started:
            pieces.append("%s not started" % _plural(not_started, "sample was", "samples were"))
        out.append('<div class="issue stopped"><h3>%s</h3><p><span class="do">What to do:</span> '
                   "open %s, choose this results folder and press Continue. "
                   "Samples that are finished are not run again.</p></div>"
                   % (_esc(" and ".join(pieces).capitalize() + "."), _esc(branding.APP_NAME)))
    if left:
        out.append('<div class="issue leftout"><h3>%s left out of this run</h3><ul>'
                   % _esc(_plural(len(left), "sample was", "samples were")))
        for row in left:
            out.append("<li><b>%s</b>: %s</li>"
                       % (_esc(row.name), _esc(row.note or "no reason was recorded")))
        out.append("</ul></div>")
    return out


def _where(summary: _Summary) -> List[str]:
    out = ["<h2>Where things are</h2>", '<ul class="plain">']
    files = _plural(summary.assemblies, "file", "files")
    if summary.assemblies:
        out.append('<li>Your assemblies are in the folder <a href="%s/"><b>%s</b></a>: %s, one per sample, '
                   "named after the sample.</li>" % (results.ASSEMBLIES, results.ASSEMBLIES, _esc(files)))
    else:
        out.append("<li>There are no assemblies yet. They will be in the folder <b>%s</b>, "
                   "one file per sample.</li>" % results.ASSEMBLIES)
    out.append("<li>Each sample has its own folder beside this page. Its <b>assembly</b> folder holds the "
               "contigs and a report; its <b>trimming</b> folder holds the cleaned reads and the trimming "
               "report. The Open column of the table leads there.</li>")
    out.append('<li>The same table for Excel or other programs is <a href="%s">%s</a>.</li>'
               % (results.SUMMARY_TSV, results.SUMMARY_TSV))
    if summary.has_log:
        out.append('<li>The technical log of the run is <a href="%s">%s</a>.</li>'
                   % (results.RUN_LOG, results.RUN_LOG))
    out.append("</ul>")
    return out


def _on_the_way(summary: _Summary) -> List[str]:
    events = list(summary.info["events"])
    return ["<h2>What happened on the way</h2>"] + (
        ['<ul class="plain">%s</ul>' % "".join("<li>%s</li>" % _esc(e) for e in events)]
        if events else ["<p>Nothing out of the ordinary was recorded.</p>"])


def _methods(summary: _Summary) -> List[str]:
    info = summary.info
    done = [row for row in summary.rows if row.group == "run" and row.kind in ("done", "notes")]
    tesseract = "TesserACT %s" % versions.TESSERACT_VERSION
    scepter = "scepter %s" % versions.SCEPTER_VERSION
    program = "%s %s (%s)" % (branding.APP_NAME, versions.APP_VERSION, branding.HOMEPAGE)
    trimmed = sum(1 for row in done if row.trimmed)
    if not done:
        sentence = "%s runs the trimmer %s and the assembler %s." % (program, scepter, tesseract)
    elif trimmed == len(done):
        sentence = "Reads were trimmed with %s and assembled with %s, run through %s." % (
            scepter, tesseract, program)
    elif trimmed == 0:
        sentence = "Reads were assembled with %s, without trimming, run through %s." % (tesseract, program)
    else:
        sentence = ("Reads were assembled with %s, run through %s; for %d of the %d samples they were "
                    "first trimmed with %s." % (tesseract, program, trimmed, len(done), scepter))
    settings = info["settings"]
    if settings:
        if not settings.lower().startswith("settings"):
            settings = "Settings: " + settings
        sentence += " " + _sentence(settings)
    date = info["finished"] or info["started"]
    if date:
        sentence += " Date of the run: %s." % _day(date)
    out = ["<h2>What it was made with</h2>",
           "<p>For the methods section of a report or paper. Click the text to select it, then copy it.</p>",
           '<p class="methods">%s</p>' % _esc(sentence)]
    if summary.earlier or any(row.kind == "skipped" for row in summary.rows):
        out.append('<p class="quiet">Samples that were skipped, or that are in this folder from earlier '
                   "runs, may have been made with other settings or versions. %s lists them for every sample.</p>"
                   % results.SUMMARY_TSV)
    return out


def _render_html(summary: _Summary) -> str:
    info, tally = summary.info, summary.tally
    run = [row for row in summary.rows if row.group == "run"]
    total = len(run)
    folder_name = os.path.basename(os.path.normpath(summary.results_dir)) or summary.results_dir
    title = "Run summary: %s" % folder_name
    in_progress = info["finished"] is None and bool(info["started"] or tally)
    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<meta name="generator" content="%s %s">' % (_esc(branding.APP_NAME), _esc(versions.APP_VERSION)),
           '<meta name="color-scheme" content="light dark">']
    if in_progress:
        out.append('<meta http-equiv="refresh" content="120">')
    out += ["<title>%s</title>" % _esc(title), "<style>%s</style>" % _CSS, "</head>", "<body>",
            '<div class="tone tone-%s"></div>' % _tone(summary), '<div class="wrap">', "<header>",
            '<p class="what">%s, written by %s</p>' % (_esc(title), _esc(branding.APP_NAME)),
            "<h1>%s</h1>" % _esc(_headline(summary))]
    times = _times_line(summary)
    if times:
        out.append('<p class="times">%s</p>' % _esc(times))
    if total:
        size = " few" if total <= 60 else " many" if total > 150 else ""
        out.append('<div class="plate%s" aria-hidden="true">' % size)
        out.append("".join('<a class="well %s" href="#%s" title="%s" tabindex="-1"></a>'
                           % (row.kind, row.anchor, _esc("%s: %s" % (row.name, row.words))) for row in run))
        out.append("</div>")
        out.append('<ul class="tally">')
        for kind, words in _TALLY_ORDER:
            if tally.get(kind):
                out.append('<li><span class="dot %s"></span><b>%s</b> %s</li>'
                           % (kind, "{:,}".format(tally[kind]), _esc(words)))
        out.append('<li class="sum">%s in this run, one circle each</li>'
                   % _esc(_plural(total, "sample", "samples")))
        out.append("</ul>")
    out.append("</header>")
    out.append("<main>")
    out += _attention(summary)
    out += _where(summary)
    out.append("<h2>All samples</h2>")
    if summary.rows:
        out += _table(summary)
    else:
        out.append("<p>No sample has been assembled in this folder yet.</p>")
    out += _on_the_way(summary)
    out += _methods(summary)
    out.append("</main>")
    out += ["<footer>",
            "<p>%s %s. %s</p>" % (_esc(branding.APP_NAME), _esc(versions.APP_VERSION),
                                  _esc(branding.ATTRIBUTION)),
            '<p>This page was written %s. <a href="%s">%s</a></p>'
            % (_esc(_when(summary.written)), _esc(branding.HOMEPAGE), _esc(branding.HOMEPAGE)),
            "</footer>", "</div>", "</body>", "</html>"]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- the one entry point

def write_run_summary(results_dir: str, *, this_run: Iterable[SampleOutcome],
                      left_out: Iterable[Tuple[str, str]],
                      run_info: Optional[Mapping[str, Any]]) -> List[str]:
    """Rewrite ``run_summary.tsv`` and ``run_summary.html`` atomically. Never raises.

    ``this_run``: the :class:`SampleOutcome` of every sample of this run that has one so far.
    ``left_out``: ``(name, reason)`` for every sample that was left out.
    ``run_info``: a dict with

    * ``started``, ``finished``: ISO times; ``finished`` is None while the run is going;
    * ``seconds``: how long it took (or has been running);
    * ``end_reason``: an :class:`EndReason` or its value; None while running;
    * ``message``: the one-line outcome, as shown in the window;
    * ``settings``: the settings line ("Settings: standard trimming, ...");
    * ``events``: sentences about what happened on the way ("The PC was asleep from ...");
    * optional ``samples``: the names of all samples of the run, in order, so that those with no
      outcome yet are shown as Waiting (Not started once the run has ended);
    * optional ``current``: the name of the sample being worked on.

    Returns the names of the files it could **not** write (``[]`` when all is well), for example
    ``["run_summary.tsv"]`` while that file is open in Excel. The reason is in the log.

    A value of the wrong kind (a number given as text, an entry that is no outcome) is put right
    or left out and logged; it never costs the rest of the summary. Safe to call from two
    threads at once.
    """
    targets = ((results.SUMMARY_TSV, results.summary_tsv_path, _render_tsv),
               (results.SUMMARY_HTML, results.summary_html_path, _render_html))
    started = time.perf_counter()
    try:
        results_dir = os.fspath(results_dir)
    except TypeError:
        results_dir = ""
    if not isinstance(results_dir, str) or not results_dir:
        log.error("the run summary was not written: no results folder was given")
        return [name for name, _, _ in targets]     # never into the working directory
    with _WRITE_LOCK:                               # safe to call from two threads at once
        try:
            summary = _gather(results_dir, this_run, left_out, run_info)
        except Exception:                           # noqa: BLE001 - a summary must never stop a run
            log.exception("the run summary could not be put together for %s", results_dir)
            return [name for name, _, _ in targets]
        not_written: List[str] = []
        for name, path_of, render in targets:
            try:
                fsutil.atomic_write_text(path_of(results_dir), render(summary))
            except Exception:                       # noqa: BLE001
                log.exception("%s could not be written in %s", name, results_dir)
                not_written.append(name)
    log.debug("run summary of %d rows written in %.3f s", len(summary.rows), time.perf_counter() - started)
    return not_written
