# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Every sentence about a result: what happened, what it means for the data, what to do next.

The read check and the summary keep the check names, verdicts and numbers of the Ubuntu
``tesseract`` command in their files (decision D11). The screen never shows those lines:
this module turns each of them, each way an engine can end and each failure of the PC into
plain English. Error codes and the engines' own text go to the logs only (rule R10).

No Tk, no files, no processes: text in, text out. The wording follows the catalogue of
``docs/superpowers/audits/ux-windows.md`` section A-9.

An :class:`Explained` carries, beside the two sentences, a ``code`` for the program (never
shown; two failures with the same code "failed the same way") and two marks: ``about_data``
(the fault lies in this sample's read files) and ``environment`` (the fault lies in the PC
or the results folder, so the batch must stop: decision D9).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import branding
from .fsutil import format_bytes, format_duration
from .model import EndReason, RunStatus
from .osapi import ExitKind

__all__ = [
    "Explained", "ENGINE_WORDS", "ASSEMBLY_STEPS",
    "explain_input_problem", "explain_read_check", "explain_summary", "explain_engine_end",
    "explain_trimming_result", "explain_environment", "internal_error", "reads_gone",
    "too_little_data", "worst_first",
    "environment_reason", "environment_stop", "start_problem",
    "with_earlier_kept", "stopped_note", "stopped_by_environment", "second_time",
    "SECOND_TIME", "skipped_note", "new_result_note", "left_out_same_name",
    "left_out_foreign_folder", "left_out_reserved_name", "left_out_name_twice", "left_out_bad_name",
    "step_text", "joining_text", "trimming_text", "pair_check_text", "checking_text",
    "THIN_DATA_RETRY", "STARTING_TRIMMER", "STARTING_ASSEMBLER", "WRITING_SUMMARY",
    "ON_BATTERY", "battery_low", "low_memory", "slept", "summary_not_updated",
    "batch_message", "interrupted_sentence", "count_words",
    "date_words", "clock_words", "drive_words",
]


# --------------------------------------------------------------------------- small helpers

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")


def date_words(iso: str) -> str:
    """``2026-11-03T14:02:11`` as ``3 Nov 2026``; "" when the text is not a date."""
    match = _ISO.match(iso or "")
    if not match:
        return ""
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return ""
    return "%d %s %s" % (int(match.group(3)), _MONTHS[month - 1], match.group(1))


def clock_words(when: Any) -> str:
    """A time of day as ``03:42``. ``when`` is an ISO time or seconds since the epoch."""
    if isinstance(when, (int, float)):
        return time.strftime("%H:%M", time.localtime(when))
    match = _ISO.match(str(when or ""))
    if match and match.group(4):
        return "%s:%s" % (match.group(4), match.group(5))
    return ""


def drive_words(path: str) -> str:
    """``drive D:`` for a Windows path with a drive letter, otherwise ``the drive that holds <path>``."""
    drive = os.path.splitdrive(path or "")[0]
    if len(drive) == 2 and drive[1] == ":":
        return "drive %s" % drive.upper()
    if drive:
        return "the network folder %s" % drive
    return "the drive that holds %s" % path if path else "the drive"


def count_words(number: int, one: str, many: Optional[str] = None) -> str:
    """``1 sample``, ``38 samples``, ``5,482 reads``."""
    return "%s %s" % ("{:,}".format(int(number)), one if int(number) == 1 else (many or one + "s"))


def _file_name(path: str) -> str:
    return os.path.basename(str(path).replace("\\", "/")) or str(path)


def _quoted(message: str, whole_path: bool = False) -> str:
    """The first thing between single quotes in a check line: a file, by its name or as it stands."""
    match = re.search(r"'(.+)'", message or "")          # greedy: a file name may hold an apostrophe
    if not match:
        return ""
    return match.group(1) if whole_path else _file_name(match.group(1))


def _in_brackets(names: Iterable[str]) -> str:
    listed = [n for n in names if n]
    return " (%s)" % ", ".join(listed) if listed else ""


@dataclass(frozen=True)
class Explained:
    """One finding in plain words."""
    note: str                    #: what happened and what it means for the data
    next_step: str = ""          #: what to do about it
    code: str = ""               #: for the program, never shown
    about_data: bool = False     #: the fault lies in this sample's read files
    environment: str = ""        #: not empty: the PC's failure; the code for :func:`environment_stop`
    path: str = ""               #: the file or folder the environment failure is about

    @property
    def text(self) -> str:
        """Both sentences, for a place that shows one text."""
        return ("%s %s" % (self.note, self.next_step)).strip()


# --------------------------------------------------------------------------- A-9: the read files

_SEND_REPORT = "please send a problem report (About, Save a problem report)"
_COPY_AGAIN = "Copy it again from the original, then try again."
_COPY_BOTH_AGAIN = "Copy the files again from the original, then try again."
_ORIGINALS = "Use the two original files from the sequencer."
_ASK_FASTQ = "Ask whoever produced them for standard Illumina FASTQ files."


def _damaged(file_name: str = "") -> Explained:                                    # row 9.1
    if file_name:
        return Explained(
            "A read file is incomplete or damaged (%s). It may have been cut short while copying "
            "or downloading." % file_name, _COPY_AGAIN, "input_damaged", about_data=True)
    return Explained("The read files are incomplete or damaged. They may have been cut short while "
                     "copying or downloading.", _COPY_BOTH_AGAIN, "input_damaged", about_data=True)


def _mismatch() -> Explained:                                                      # row 9.2
    return Explained("Read 1 and Read 2 do not match: one file has fewer reads or they are in a "
                     "different order.", _ORIGINALS, "input_mismatch", about_data=True)


def _not_fastq() -> Explained:                                                     # row 9.3
    return Explained("The read files are not in the standard FASTQ format the program needs.",
                     _ASK_FASTQ, "input_format", about_data=True)


def _empty(file_name: str = "") -> Explained:
    return Explained("A read file is empty%s: it holds no reads." % _in_brackets([file_name]),
                     _COPY_AGAIN, "input_empty", about_data=True)


def _same_file() -> Explained:
    return Explained("The same file was given as Read 1 and as Read 2. A sample needs its two "
                     "different read files.", "Choose the two files that belong to this sample.",
                     "input_same_file", about_data=True)


def reads_gone(path: str = "") -> Explained:                                       # row 9.10
    """A read file that was there when the sample was added cannot be read any more."""
    return Explained("The read files can no longer be read%s." % _in_brackets([path]),
                     "Is the drive still connected? Reconnect it, then try this sample again.",
                     "reads_gone")


def too_little_data() -> Explained:                                                # row 9.4
    return Explained("Too little data to assemble: no contigs were produced.",
                     "This sample needs more sequencing.", "too_little_data", about_data=True)


def explain_input_problem(problem: Any) -> Explained:
    """A :class:`readcheck.InputProblem` (the check of a raw pair before trimming) in A-9 words."""
    code = getattr(problem, "code", "")
    name = _file_name(getattr(problem, "file", "") or "")
    if code == "unreadable":
        return reads_gone(getattr(problem, "file", "") or "")
    if code == "empty":
        return _empty(name)
    if code in ("damaged", "length_mismatch"):
        return _damaged(name)
    if code == "same_file":
        return _same_file()
    if code in ("no_reads", "not_fastq"):
        return _not_fastq()
    if code in ("fewer_reads", "names_differ"):
        return _mismatch()
    # blank_lines, line_ends and anything newer: readcheck's own sentences are already plain.
    return Explained(str(getattr(problem, "message", "")) or _mismatch().note,
                     str(getattr(problem, "next_step", "")) or _ORIGINALS,
                     "input_%s" % (code or "problem"), about_data=True)


# --------------------------------------------------------------------------- the read check

_DEPTH = re.compile(r"about (\d+)x for a (\S+) Mb genome( \(assumed)?")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")
_GB_FREE = re.compile(r"only (\d+) GB")
_NEED_GB = re.compile(r"need about (\d+) GB")
_TRIM_ADVICE = ("Tick Trim for this sample on the Samples page (with the standard trimming "
                "options) and run it again.")


def explain_read_check(line: Any, *, trimmed: bool = False) -> Optional[Explained]:
    """One line of ``preflight.txt`` in plain words; None for a line that says all is well.

    ``line`` is a :class:`readcheck.CheckLine`. ``trimmed`` says whether the reads that were
    checked had been trimmed by this program (the advice then no longer says "tick Trim").
    """
    verdict, name, message = line.verdict, line.name, line.message
    if verdict == "OK":
        return None
    code = "read_check_%s" % name.replace(" ", "_")
    if name == "file":
        if "is empty" in message:
            return _empty(_quoted(message))
        return reads_gone(_quoted(message, whole_path=True))      # the path says which drive to look at
    if name == "gzip":
        return _damaged(_quoted(message))
    if name == "pairing":
        return _same_file() if "same file" in message else _mismatch()
    if name == "format":
        return _damaged() if "lengths differ" in message else _not_fastq()
    if name == "quality":
        return _not_fastq()
    if name == "depth":
        found = _DEPTH.search(message)
        depth = found.group(1) if found else "?"
        assumed = (" This assumes a genome of %s Mb; if yours is smaller, tick \"I know the "
                   "genome size\" on the Assembly page." % found.group(2)) if found and found.group(3) else ""
        if "too thin" in message:
            return Explained("Very low depth (about %sx): too little data for a reliable assembly; "
                             "expect many gaps.%s" % (depth, assumed),
                             "More sequencing of this sample would give a better assembly.", code,
                             about_data=True)
        if "very deep" in message:
            return Explained("Very high depth (about %sx): the run takes longer and needs more "
                             "memory, and the result is no better than at about 150x.%s" % (depth, assumed),
                             "No action is needed.", code, about_data=True)
        return Explained("Low depth (about %sx): expect a fragmented assembly.%s" % (depth, assumed),
                         "More sequencing of this sample would give a better assembly.", code,
                         about_data=True)
    if name in ("adapters", "poly-G"):
        found = _PERCENT.search(message)
        share = "%s %%" % found.group(1) if found else "many"
        what = ("Adapter sequence is still in %s of the read pairs; it can break or mis-join "
                "contigs." if name == "adapters" else
                "A run of G, an artefact of some sequencers, ends %s of the read pairs; it can "
                "mis-join contigs.") % share
        advice = ("On the Trimming page, make sure \"%s\" is ticked, then run this sample again."
                  % ("Remove adapters" if name == "adapters" else "Trim poly-G tails")) if trimmed else _TRIM_ADVICE
        return Explained(what, advice, code, about_data=True)
    if name == "N bases":
        found = _PERCENT.search(message)
        return Explained("The sequencer could not read %s of the bases (N). The assembly will have "
                         "gaps." % ("%s %%" % found.group(1) if found else "many"),
                         "Trimming removes the worst reads; a new sequencing run is the real cure.",
                         code, about_data=True)
    if name == "disk":
        free, need = _GB_FREE.search(message), _NEED_GB.search(message)
        detail = (" (%s GB free, about %s GB needed)" % (free.group(1), need.group(1))) if free and need else ""
        return Explained("There is not enough free space in the results folder for this sample%s." % detail,
                         "Free some space, or choose a results folder on another drive.",
                         "disk_low", environment="disk_low")
    if name == "memory":
        free = _GB_FREE.search(message)
        return Explained("Only %s GB of memory was free when this sample started; the assembly may "
                         "have been slow." % (free.group(1) if free else "a few"),
                         "Close other programs, web browsers above all, before the next run.", code)
    if verdict == "FAIL":
        return Explained("The read check found a problem with the read files (%s)." % name,
                         _ORIGINALS, code, about_data=True)
    return Explained("The read check has a remark about the read files (%s). The details are in "
                     "preflight.txt in the sample's assembly folder." % name, "", code, about_data=True)


# --------------------------------------------------------------------------- the summary

_MB_PAIR = re.compile(r"total (\S+) Mb vs ~(\S+) Mb")
_LOST_KB = re.compile(r"about (\S+) kb")
_LEADING_COUNT = re.compile(r"^(\d+)")
_PROGRAM_FAULT = ("This is a fault in the program, not in your data. Try the sample again; if it "
                  "happens again, %s." % _SEND_REPORT)


def explain_summary(line: Any, facts: Any = None) -> Optional[Explained]:
    """One line of ``SUMMARY.txt`` in plain words; None for a line that says all is well.

    ``line`` is a :class:`readcheck.CheckLine` from :func:`postflight.summarise`; ``facts``
    its :class:`postflight.RunFacts` (used for the numbers when given).
    """
    verdict, name, message = line.verdict, line.name, line.message
    if verdict == "OK":
        return None
    code = "summary_%s" % name.replace(" ", "_").replace(".", "_")
    if name == "exit status":
        return Explained("The assembler did not finish.", "Open the log to see why.", "engine_error:assembler")
    if name == "file":
        if message.startswith("contigs.fasta"):
            return too_little_data()
        return Explained("The assembler did not write all of its files (%s is missing), so the "
                         "result cannot be trusted." % message.split(" ")[0], _PROGRAM_FAULT, code)
    if name == "contigs.fasta":
        return Explained("The assembler wrote a contigs file that is not well formed, so the result "
                         "cannot be trusted.", _PROGRAM_FAULT, code)
    if name == "report.json":
        return Explained("The assembler's report could not be read, so the result cannot be "
                         "checked.", _PROGRAM_FAULT, code)
    if name == "size":
        found = _MB_PAIR.search(message)
        sizes = ("The assembly is %s Mb long but about %s Mb was expected" % found.groups()) if found \
            else "The assembly is not as long as expected"
        return Explained("%s: the sample may be contaminated or mixed, or the genome size is not "
                         "the right one." % sizes,
                         "Check the genome size on the Assembly page, and check the culture for "
                         "contamination.", code, about_data=True)
    if name == "self-check":
        lost = getattr(facts, "lost_kb", None)
        if lost is None:
            found = _LOST_KB.search(message)
            amount = found.group(1) if found else "some"
        else:
            amount = "%.1f" % lost
        return Explained("About %s kb of sequence that is plentiful in the reads is not in the "
                         "assembly (often a small plasmid with many copies, or repeated genes)." % amount,
                         "If small plasmids matter for your work, look at p2_self_qa.missing_hi.tsv "
                         "in the sample's assembly folder.", code, about_data=True)
    if name == "spike-in":
        found = _LEADING_COUNT.match(message)
        number = int(found.group(1)) if found else 1
        return Explained("The assembly holds %s of the sequencer's control DNA (PhiX), labelled "
                         "_spikein." % count_words(number, "contig"),
                         "Remove the contigs labelled _spikein before you submit or publish the "
                         "assembly.", code, about_data=True)
    if name == "library":
        return Explained("The two reads of the pairs do not face each other as in a standard "
                         "library, so pairs were not used to join contigs. Expect more and shorter "
                         "contigs.", "Check with whoever prepared the library that it is a standard "
                         "paired-end library.", code, about_data=True)
    if name == "thin data":
        return Explained("Very little data: the first attempt assembled almost nothing, so this "
                         "result was made with settings for thin data. Expect a fragmented assembly.",
                         "More sequencing of this sample would give a better assembly.", code,
                         about_data=True)
    if verdict == "FAIL":
        return Explained("The check of the finished assembly failed (%s), so the result cannot be "
                         "trusted." % name, _PROGRAM_FAULT, code)
    return Explained("The check of the finished assembly has a remark (%s). The details are in "
                     "SUMMARY.txt in the sample's assembly folder." % name, "", code)


def worst_first(found: Iterable[Optional[Explained]]) -> list:
    """The findings that are not None, in the order given, without repeats of the same sentence."""
    seen, out = set(), []
    for item in found:
        if item is not None and item.note not in seen:
            seen.add(item.note)
            out.append(item)
    return out


# --------------------------------------------------------------------------- how an engine ended

#: What an engine is called on screen.
ENGINE_WORDS = {"scepter": "trimmer", "tesseract-asm": "assembler"}

_LENGTHS_DIFFER = re.compile(r"sequence length \d+ but quality length \d+")
_BROKEN_RECORD = re.compile(r"truncated record|malformed record")
_ERROR_FILE = re.compile(r"error:\s+(.+?):\s+(?:record|truncated|malformed)")
_NOT_FOUND = re.compile(r"does not exist|input file not found|No such file or directory", re.IGNORECASE)
_NO_SPACE = re.compile(r"No space left on device|not enough space|disk full|cannot write|"
                       r"cannot create output|write failed|failed to write|error writing", re.IGNORECASE)
_NO_MEMORY = re.compile(r"bad_alloc|out of memory|cannot allocate|memory limit", re.IGNORECASE)


def _engine_word(engine: str) -> str:
    name = _file_name(engine).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    for key, word in ENGINE_WORDS.items():
        if key in name:
            return word
    return "engine"


def explain_engine_end(engine: str, kind: ExitKind, *, log_tail: str = "", hung: bool = False,
                       exe: str = "", idle_minutes: int = 30) -> Optional[Explained]:
    """Why an engine run that did not end well failed; None for one that ended well.

    ``engine`` is ``"scepter"`` or ``"tesseract-asm"`` (a path is fine), ``kind`` the
    :class:`osapi.ExitKind` of the run, ``log_tail`` the last lines the engine printed,
    ``hung`` whether it was ended for doing nothing. ``exe`` is the engine's file, named
    when the fault is that it could not be started.
    """
    word = _engine_word(engine)
    tail = log_tail or ""
    if kind is ExitKind.OK:
        return None
    if hung:                                                                       # row 7.8
        return Explained("The %s stopped responding and was ended after %s without "
                         "activity." % (word, count_words(idle_minutes, "minute")),
                         "Try this sample again. If it happens again, %s." % _SEND_REPORT,
                         "engine_hung:%s" % word)
    if kind is ExitKind.COULD_NOT_START:
        return Explained("Part of the program could not be started%s." % _in_brackets([_file_name(exe)]),
                         "Extract the zip again and try again.", "engine_missing",
                         environment="engine_missing", path=exe)
    if kind is ExitKind.BLOCKED:
        return Explained("Windows or your antivirus stopped part of the program from starting%s."
                         % _in_brackets([_file_name(exe)]),
                         "Ask your IT support to allow the %s folder." % branding.APP_NAME,
                         "engine_blocked", environment="engine_blocked", path=exe)
    if kind is ExitKind.OUT_OF_MEMORY or _NO_MEMORY.search(tail):                  # row 9.5
        return Explained("This sample needs more memory than this PC could give.",
                         "Close other programs and try again, or assemble it on a PC with more memory.",
                         "out_of_memory")
    if kind is ExitKind.STOPPED:
        return Explained("The %s was ended from outside the program (Windows was shutting down, or "
                         "it was ended in Task Manager), so this sample was not finished." % word,
                         "Try this sample again.", "engine_ended:%s" % word)
    if kind is ExitKind.CRASH:                                                     # row 9.6
        return Explained("The %s stopped unexpectedly. This is a fault in the program, not in your "
                         "data. The other samples continue." % word,
                         "P%s." % _SEND_REPORT[1:], "engine_crash:%s" % word)
    # The engine reported an error of its own: its last lines say which.
    if _LENGTHS_DIFFER.search(tail) or _BROKEN_RECORD.search(tail):
        found = _ERROR_FILE.search(tail)
        return _damaged(_file_name(found.group(1)) if found else "")
    if _NOT_FOUND.search(tail):
        return reads_gone()
    if _NO_SPACE.search(tail):
        return Explained("The %s could not write its files to the results folder." % word,
                         "Check that the drive of the results folder is connected and has free space.",
                         "results_unwritable", environment="results_unwritable")
    return Explained("The %s could not process this sample and reported an error. Its own message "
                     "is in the sample's log." % word,
                     "Open the log to read it. If other samples fail in the same way, %s." % _SEND_REPORT,
                     "engine_error:%s" % word)


def explain_trimming_result(problem: str, detail: str = "") -> Explained:
    """Trimming ended without an error but its result cannot be used.

    ``problem``: ``"no_reads_left"`` (every read was removed), ``"files_missing"`` (a file
    the trimmer should have written is not there; ``detail`` names it) or ``"report_unreadable"``.
    """
    if problem == "no_reads_left":
        return Explained("No reads were left after trimming: every read was removed as too short or "
                         "of too poor quality.",
                         "Look at the quality of this sequencing run. Loosening the options on the "
                         "Trimming page keeps more reads, but poor reads give a poor assembly.",
                         "trim_no_reads", about_data=True)
    if problem == "files_missing":
        return Explained("The trimmer ended without writing all of its files%s, so the trimmed reads "
                         "cannot be trusted." % _in_brackets([detail]),
                         "Try this sample again. If it happens again, %s." % _SEND_REPORT, "trim_incomplete")
    return Explained("The trimmer's report could not be read, so the trimmed reads cannot be trusted.",
                     "Try this sample again. If it happens again, %s." % _SEND_REPORT, "trim_incomplete")


def internal_error() -> Explained:                                                 # row 9.14
    return Explained("Something went wrong inside %s while it worked on this sample. Your data is "
                     "not at fault, and nothing that was finished has been lost." % branding.APP_NAME,
                     "Try this sample again. If this keeps happening, %s." % _SEND_REPORT, "internal")


# --------------------------------------------------------------------------- the PC's failures

_EXTRACT_AGAIN = ("Antivirus software sometimes removes it by mistake. Extract the zip again%s. If it "
                  "happens again, ask your IT support to allow the %s folder.")


def environment_reason(code: str, path: str = "", *, detail: str = "") -> str:
    """What is wrong with the PC or the results folder, as one sentence without advice.

    Codes: ``results_gone``, ``results_unwritable``, ``disk_full``, ``disk_low``,
    ``engine_missing``, ``engine_broken``, ``engine_blocked``, ``reads_gone``, ``same_failure``,
    ``folder_in_use``, ``newer_version``, ``no_samples``, ``internal``.
    """
    shown = _in_brackets([path])
    if code == "results_gone":
        return "the results folder can no longer be reached%s" % shown
    if code == "results_unwritable":
        return "the results folder cannot be written to%s" % shown
    if code == "disk_full":
        return "%s is full" % drive_words(path)
    if code == "disk_low":
        return "there is not enough free space on %s for the next sample%s" % (
            drive_words(path), (" (%s)" % detail) if detail else "")
    if code == "engine_missing":
        return "part of the program has disappeared%s" % shown
    if code == "engine_broken":
        return "part of the program does not start%s" % shown
    if code == "engine_blocked":
        return "Windows or your antivirus stopped part of the program from starting%s" % shown
    if code == "internal":
        return "something went wrong inside %s" % branding.APP_NAME
    if code == "reads_gone":
        return "the read files can no longer be read%s" % shown
    if code == "same_failure":
        return "three samples in a row failed in the same way, straight after starting"
    if code == "folder_in_use":
        return "the results folder is being used by another run%s" % ((" (%s)" % detail) if detail else "")
    if code == "newer_version":
        return "the results folder was made by a newer version of %s" % branding.APP_NAME
    if code == "no_samples":
        return "no sample is ready to run"
    return detail or "this PC could not go on with the run"


def _advice(code: str, again: str) -> str:
    """What to do about an environment failure; ``again`` is how to go on ("press Continue")."""
    if code == "results_gone":
        return "Reconnect the drive, then %s." % again
    if code == "results_unwritable":
        return "Check that its drive is connected and is not read-only, then %s." % again
    if code in ("disk_full", "disk_low"):
        return "Free some space, then %s." % again
    if code == "engine_missing":
        return _EXTRACT_AGAIN % (" and %s" % again, branding.APP_NAME)
    if code == "engine_broken":
        return ("Extract the zip again and %s. If it happens again, ask your IT support to allow the "
                "%s folder." % (again, branding.APP_NAME))
    if code == "internal":
        return ("Nothing that was finished has been lost. You can %s; if this keeps happening, %s."
                % (again, _SEND_REPORT))
    if code == "engine_blocked":
        return "Ask your IT support to allow the %s folder, then %s." % (branding.APP_NAME, again)
    if code == "reads_gone":
        return "Is the drive still connected? Reconnect it, then %s." % again
    if code == "same_failure":
        return ("That points to this PC rather than to the samples. Select a failed row to see the "
                "reason, put it right, then %s." % again)
    if code == "folder_in_use":
        return "Wait for that run to finish, or choose another results folder."
    if code == "newer_version":
        return "Open it with that version, or choose another results folder."
    if code == "no_samples":
        return "Add samples on the Samples page."
    return "Put it right, then %s." % again


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def environment_stop(code: str, path: str = "", *, finished: int = 0, detail: str = "") -> str:
    """The header line of a batch the PC brought to a stop (rows 9.7 to 9.9).

    ``"Stopped: the results folder can no longer be reached (E:\\Results). Reconnect the
    drive, then press Continue. The 12 finished samples are safe."``
    """
    text = "Stopped: %s. %s" % (environment_reason(code, path, detail=detail), _advice(code, "press Continue"))
    if finished > 0:
        text += " The %s safe." % ("1 finished sample is" if finished == 1
                                   else "%s finished samples are" % "{:,}".format(finished))
    return text


def start_problem(code: str, path: str = "", *, detail: str = "") -> str:
    """Why a run cannot start, for :func:`batch.preflight_batch`: what is wrong and what to do."""
    return "%s. %s" % (_capital(environment_reason(code, path, detail=detail)), _advice(code, "press Start again"))


def explain_environment(code: str, path: str = "", *, detail: str = "") -> Explained:
    """An environment failure as it is told for the sample it interrupted."""
    return Explained(_capital(environment_reason(code, path, detail=detail)) + ".",
                     _advice(code, "press Continue"), code, environment=code, path=path)


# --------------------------------------------------------------------------- endings of a sample

SECOND_TIME = "This is the second time. Trying again will not help unless something changes."


def with_earlier_kept(note: str, finished_at: str = "") -> str:
    """Row 5.5: ``"<reason> The earlier assembly (3 Nov 2026) was kept."``"""
    when = date_words(finished_at)
    kept = "The earlier assembly%s was kept." % ((" (%s)" % when) if when else "")
    return ("%s %s" % (note.strip(), kept)).strip()


def stopped_note(kept_earlier: bool = False) -> str:
    """The note of a sample that was running when the user pressed Stop now."""
    if kept_earlier:
        return "Stopped. The earlier assembly was kept."
    return "Stopped before it was finished. It starts from the beginning next time."


def stopped_by_environment(reason: Explained, kept_earlier: bool = False) -> str:
    """The note of a sample that was running when the PC's failure ended the batch."""
    note = "Not finished: %s" % (reason.note[:1].lower() + reason.note[1:])
    return with_earlier_kept(note) if kept_earlier else note + " It starts from the beginning next time."


def second_time(note: str) -> str:
    """Row 9.12: the same sample failed again for the same reason."""
    return note if note.endswith(SECOND_TIME) else "%s %s" % (note.strip(), SECOND_TIME)


def skipped_note(finished_at: str = "", same_settings: bool = True) -> str:
    """Row 5.3: ``"Already assembled on 3 Nov 2026, with other settings."``"""
    when = date_words(finished_at)
    text = "Already assembled%s" % ((" on %s" % when) if when else "")
    return text + ("." if same_settings else ", with other settings.")


def new_result_note(name: str, reason: str = "in_use") -> str:
    """Row 5.6: the old result could not be replaced; the new one waits beside it."""
    where = "%s%snew_result" % (name, os.sep)
    if reason == "not_ours":
        return ("Done, but the sample's folder holds an assembly or trimming folder that this program "
                "did not make, so it was not replaced. The new result is in %s." % where)
    return ("Done, but the old result could not be replaced because one of its files is open in "
            "another program. The new result is in %s." % where)


def left_out_same_name(name: str) -> str:
    """Decision D3."""
    return ("A different sample called %s is already assembled in this results folder (it was made "
            "from other read files). Rename this sample on the Samples page, or choose another "
            "results folder." % name)


def left_out_foreign_folder(name: str, folder: str) -> str:
    """Audit row 3.7: the sample's folder holds an ``assembly`` or ``trimming`` folder of someone else."""
    return ("The results folder already holds a folder %s%s%s that this program did not make, and it "
            "will not overwrite it. Rename this sample on the Samples page, or choose another "
            "results folder." % (name, os.sep, folder))


def left_out_name_twice(name: str, other: str) -> str:
    """Two samples of one run whose names differ only in capital letters: one folder on Windows."""
    return ("Another sample in this run is called %s. Windows cannot tell that name from %s, so "
            "the two would share one folder. Rename one of them on the Samples page." % (other, name))


def left_out_bad_name(name: str) -> str:
    return ("The name \"%s\" cannot be used as the name of a folder. Rename this sample on the "
            "Samples page." % name)


def left_out_reserved_name(name: str) -> str:
    return ("The name %s is used by the results folder itself. Rename this sample on the Samples "
            "page." % name)


# --------------------------------------------------------------------------- progress lines

#: The assembler's seven steps in words (audit B.5).
ASSEMBLY_STEPS = (
    "reading the reads", "correcting read errors", "building the assembly graph",
    "resolving repeats with the read pairs", "closing gaps", "polishing the sequence",
    "writing the results")

THIN_DATA_RETRY = "Very little data: trying again with settings for thin data."
STARTING_TRIMMER = "Trimming: starting"
STARTING_ASSEMBLER = "Starting the assembler"
WRITING_SUMMARY = "Checking the assembly and writing the summary"


def step_text(progress: Any, *, retry: bool = False) -> str:
    """``"Step 3 of 7: building the assembly graph (k = 55)"`` from an ``engine_proc.Progress``."""
    step, total = getattr(progress, "step", None), getattr(progress, "total_steps", None)
    if not step:
        return ""
    total = total or len(ASSEMBLY_STEPS)
    detail = getattr(progress, "detail", "") or ""
    words = ASSEMBLY_STEPS[step - 1] if 1 <= step <= len(ASSEMBLY_STEPS) else (detail or "working")
    if step == 4 and detail.startswith("collecting contigs"):
        words = "collecting the contigs"                 # the engine's other step 4: the pairs are not used
    text = "Step %d of %d: %s" % (step, total, words)
    k = getattr(progress, "k", None)
    if k:
        text += " (k = %d)" % k
    if retry:
        text += " (second attempt, for thin data)"
    return text


def _percent(fraction: Optional[float]) -> str:
    return "%d %%" % int(round(100 * min(1.0, max(0.0, fraction or 0.0))))


def joining_text(fraction: Optional[float] = None) -> str:
    return "Joining lanes" if fraction is None else "Joining lanes: %s" % _percent(fraction)


def pair_check_text(fraction: Optional[float] = None) -> str:
    return ("Checking the read files before trimming" if fraction is None
            else "Checking the read files before trimming: %s" % _percent(fraction))


def trimming_text(fraction: Optional[float] = None) -> str:
    return "Trimming" if fraction is None else "Trimming: %s" % _percent(fraction)


def checking_text(fraction: Optional[float] = None) -> str:
    return "Checking reads" if fraction is None else "Checking reads: %s" % _percent(fraction)


# --------------------------------------------------------------------------- notices

ON_BATTERY = ("Running on battery. Connect the charger: a long run can empty the battery, and "
              "Windows may then put the PC to sleep or switch it off.")


def battery_low(percent: int) -> str:
    return "Battery at %d %%. Connect the charger now, or the run will be interrupted." % percent


def low_memory(free_bytes: float) -> str:
    return ("Only %s of memory is free. Close other programs, web browsers above all, for a faster "
            "and safer run." % format_bytes(free_bytes))


def slept(from_time: Any, to_time: Any) -> str:
    return ("The PC was asleep from %s to %s. The run carried on when it woke up."
            % (clock_words(from_time), clock_words(to_time)))


def summary_not_updated(file_names: Sequence[str]) -> str:
    """Row 9.13."""
    names = " and ".join(file_names)
    return ("%s could not be updated because %s open in another program. Close it and press "
            "'Write the summary again'." % (names, "it is" if len(file_names) == 1 else "they are"))


# --------------------------------------------------------------------------- the end of a batch

_COUNT_WORDS = (
    (RunStatus.DONE, "done"), (RunStatus.DONE_NOTES, "done (check notes)"), (RunStatus.FAILED, "failed"),
    (RunStatus.SKIPPED, "skipped (already assembled)"), (RunStatus.STOPPED, "stopped"),
    (RunStatus.NOT_STARTED, "not started"), (RunStatus.LEFT_OUT, "left out"),
)


def _number(counts: Mapping[str, int], status: RunStatus) -> int:
    try:
        return int(counts.get(status.value, 0) or 0)
    except (TypeError, ValueError):
        return 0


def batch_message(reason: EndReason, counts: Mapping[str, int], *, seconds: float = 0.0,
                  finished_at: Any = None, environment: str = "") -> str:
    """The one line for the header when a batch has ended (rows 8.2, 9.7 to 9.9, 10.1).

    ``counts`` maps a ``RunStatus`` value to a number of samples. ``environment`` is the
    ready sentence of :func:`environment_stop` when the PC ended the batch.
    """
    finished = _number(counts, RunStatus.DONE) + _number(counts, RunStatus.DONE_NOTES)
    if reason is EndReason.ENVIRONMENT:
        return environment or environment_stop("", finished=finished)
    if reason is EndReason.STOPPED:
        parts = ["%d finished" % finished]
        for status, word in ((RunStatus.FAILED, "failed"), (RunStatus.SKIPPED, "skipped"),
                             (RunStatus.STOPPED, "stopped"), (RunStatus.NOT_STARTED, "not started")):
            if _number(counts, status):
                parts.append("%d %s" % (_number(counts, status), word))
        return "Stopped. %s. Nothing that was finished has been lost." % ", ".join(parts)
    parts = ["%d %s" % (_number(counts, status), word) for status, word in _COUNT_WORDS
             if _number(counts, status)]
    when = clock_words(time.time() if finished_at is None else finished_at)
    return "Finished at %s. It took %s. %s." % (when, _took(seconds), ", ".join(parts) or "No sample was run")


def _took(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return count_words(seconds, "second")
    if seconds < 3600:
        return "%d min %02d s" % (seconds // 60, seconds % 60)
    return format_duration(seconds)


def interrupted_sentence(cause: str, when: str, finished: int, total: int) -> str:
    """Rows 8.9 and 8.10: what the bar says about a run that did not reach its end."""
    day, clock = date_words(when), clock_words(when)
    day = " ".join(day.split(" ")[:2]) if day else "an earlier day"
    stand = "%d of %s finished. Nothing that was finished has been lost." % (
        finished, count_words(total, "sample is", "samples are"))
    if cause == "windows":
        return ("The last run was interrupted on %s at %s because Windows shut down or restarted. %s"
                % (day, clock or "an unknown time", stand))
    if cause == "stopped":
        return "The last run was stopped on %s at %s. %s" % (day, clock or "an unknown time", stand)
    return ("The last run did not finish: it ended unexpectedly on %s at about %s. %s"
            % (day, clock or "an unknown time", stand))
