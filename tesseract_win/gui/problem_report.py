# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
""" "Save a problem report" (audit part A-9): one zip a beginner can attach to an e-mail.

It holds the program's own logs, the log of the last run, and the logs of the samples that
failed. Never a read file, never a contig, never a report with sequence in it: only small
text files, chosen by name. The messages for a crashed engine and for a fault inside the
program send the user here ("About, Save a problem report").

No Tk in this module; the About box calls :func:`write_problem_report`.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import time
import zipfile
from typing import List, Optional, Tuple

from .. import appdirs, branding, results

__all__ = ["REPORT_PREFIX", "report_name", "files_for_report", "write_problem_report"]

log = logging.getLogger(__name__)

REPORT_PREFIX = "%s_problem_report" % branding.APP_NAME
#: Only files with these endings are ever put in a report.
TEXT_ENDINGS = (".log", ".txt", ".json", ".tsv")
#: Files of the results folder itself that describe the run.
RUN_FILES = ("run_log.txt", "last_run.json", "run_summary.tsv")
#: Files of one sample that say how it went (no sequence in any of them).
SAMPLE_FILES = ("sample_status.json", os.path.join("assembly", "preflight.txt"),
                os.path.join("assembly", "SUMMARY.txt"), os.path.join("assembly", "assembly.log"),
                os.path.join("trimming", "trimming.log"))
MOST_BYTES = 8 * 1024 * 1024          # of one file: the end of a longer one is kept
MOST_SAMPLES = 200


def report_name(moment: Optional[time.struct_time] = None) -> str:
    """ "TesserACT-Win_problem_report_2026-11-17_0312.zip" """
    return "%s_%s.zip" % (REPORT_PREFIX, time.strftime("%Y-%m-%d_%H%M", moment or time.localtime()))


def _is_text(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(TEXT_ENDINGS) or any(lowered.endswith(ending + ".%d" % n)
                                                 for ending in (".log",) for n in range(1, 10))


def files_for_report(results_dir: str = "", log_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """``(file on disk, name inside the zip)`` for everything a report holds."""
    found: List[Tuple[str, str]] = []
    logs = appdirs.log_dir() if log_dir is None else log_dir
    try:
        for name in sorted(os.listdir(logs)):
            path = os.path.join(logs, name)
            if os.path.isfile(path) and _is_text(name):
                found.append((path, "program/" + name))
    except OSError:
        pass
    if not results_dir or not os.path.isdir(results_dir):
        return found
    for name in RUN_FILES:
        path = os.path.join(results_dir, name)
        if os.path.isfile(path):
            found.append((path, "run/" + name))
    try:
        records = results.all_records(results_dir)
    except Exception:                                 # a folder that cannot be read: the logs still go
        log.warning("the results folder %s could not be read for the report", results_dir, exc_info=True)
        records = []
    samples = 0
    for record in records:
        failed = getattr(record, "status", None) is not None and record.status.value == "failed"
        folder = results.sample_dir(results_dir, record.name)
        kept = results.last_failed_dir(results_dir, record.name)
        if not failed and not os.path.isdir(kept):
            continue
        samples += 1
        if samples > MOST_SAMPLES:
            break
        for leaf in SAMPLE_FILES:
            path = os.path.join(folder, leaf)
            if os.path.isfile(path):
                found.append((path, "samples/%s/%s" % (record.name, leaf.replace(os.sep, "/"))))
        try:
            for name in sorted(os.listdir(kept)):
                path = os.path.join(kept, name)
                if os.path.isfile(path) and _is_text(name):
                    found.append((path, "samples/%s/last_failed_attempt/%s" % (record.name, name)))
        except OSError:
            pass
    return found


def _about_this_pc(results_dir: str) -> str:
    lines = [
        branding.versions_line(),
        "Written: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "System: %s" % platform.platform(),
        "Python: %s" % sys.version.split()[0],
        "Processors: %s" % (os.cpu_count() or "?"),
        "Results folder: %s" % (results_dir or "(none)"),
        "",
        "This file was made by 'Save a problem report'. It holds logs only: no reads and no assemblies.",
    ]
    return "\n".join(lines) + "\n"


def write_problem_report(folder: str, results_dir: str = "", *, log_dir: Optional[str] = None) -> str:
    """Write the zip into ``folder`` and return its path. Raises ``OSError`` when it cannot be written.

    A file that cannot be read (open in another program, gone meanwhile) is left out; of a
    file longer than :data:`MOST_BYTES` the end is kept, which is where the fault is.
    """
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, report_name())
    partial = path + ".part"
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("about.txt", _about_this_pc(results_dir))
            for source, inside in files_for_report(results_dir, log_dir):
                try:
                    with open(source, "rb") as handle:
                        size = os.fstat(handle.fileno()).st_size
                        if size > MOST_BYTES:
                            handle.seek(size - MOST_BYTES)
                        archive.writestr(inside, handle.read(MOST_BYTES))
                except OSError:
                    log.info("left out of the problem report: %s", source)
        os.replace(partial, path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    return path
