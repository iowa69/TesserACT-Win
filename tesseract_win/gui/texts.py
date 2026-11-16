# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Every word the window shows, in one place.

Plain English, British spelling ("Summarise"). No error codes and no jargon: a reader
who knows what a FASTQ pair is must understand every line. Hover texts are one or two
sentences: what the option does, and when to change it. They never name an engine flag.

This module imports no Tk, so the words can be read, checked and tested anywhere. Vendor
names come from ``branding``; the reasons for a failed sample come from ``messages``.

Layout of the module:

- steps, page titles and opening sentences
- labels: buttons, table columns, options (keyed by the field names of
  ``model.TrimSettings`` and ``model.AssemblySettings``)
- hover texts (the same text is the tooltip and the help line)
- status words for ``model.SampleState``, ``model.RunStatus``, ``model.Stage``
- sentences with numbers in them, as small functions
- the questions (Stop, close, overwrite) as :class:`Question` templates
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from .. import branding
from ..model import EndReason, Existing, RunStatus, SampleState, Stage, StopMode

__all__ = [
    "number", "count", "minutes", "STEPS", "STEP_LABEL", "STEP_STANDARD", "STEP_CHANGED",
    "STEP_NO_TRIMMING", "STEP_NO_SAMPLES", "STEP_RUNNING", "STEP_FINISHED", "STEP_STOPPED",
    "PAGE_TITLE", "PAGE_INTRO", "FIRST_TIME_HINT", "BUTTON", "SAMPLES_COLUMNS", "RUN_COLUMNS",
    "EMPTY_STATE", "EMPTY_STATE_NO_DROP", "EMPTY_STATE_LINK_LEAD", "EXAMPLE_NOTE", "MORE_LANES",
    "RESULTS_FOLDER_LABEL", "RESULTS_FOLDER_NOTE", "RESULTS_FOLDER_SUGGESTED", "TRIMMING_LABEL",
    "TRIM_LABEL", "TRIM_SUFFIX", "TRIM_HOVER", "NO_TRIMMING_NOTICE", "ASSEMBLY_LABEL",
    "ASSEMBLY_SUFFIX", "ASSEMBLY_HOVER", "MODE_LABEL", "MODE_HOVER", "HOVER",
    "SAMPLE_STATE_LABEL", "RUN_STATUS_LABEL", "STAGE_LABEL", "STAGE_STATE_LABEL",
    "STOP_MODE_LABEL", "EXISTING_LABEL", "END_REASON_LABEL", "STARTING", "ADVICE",
    "SILENCE_IS_NORMAL", "ESTIMATE_PENDING", "STOPPING_AFTER", "NOTHING_LOST", "SELECT_A_ROW",
    "THIN_DATA_RETRY", "ON_BATTERY", "ALREADY_OPEN", "STILL_IN_ZIP", "RENAME_BAD_CHARACTERS",
    "RENAME_DUPLICATE", "RENAME_TOO_LONG", "RENAME_EMPTY", "RENAME_HINT", "number_reset",
    "start_label", "next_label", "step_ready", "ready_counter", "start_reason", "settings_line",
    "sample_header", "clocks", "time_left", "working_line", "space_line", "existing_line",
    "missing_part", "blocked_part", "low_memory", "slept", "battery_low", "Choice", "Question",
    "stop_question", "close_question", "overwrite_question",
]


# --------------------------------------------------------------------------- small helpers

def number(value: int) -> str:
    """A whole number as shown on screen: 5,482,113."""
    return "{:,}".format(int(value))


def count(n: int, one: str, many: Optional[str] = None) -> str:
    """``count(1, "sample")`` is "1 sample"; ``count(38, "sample")`` is "38 samples"."""
    return "%s %s" % (number(n), one if n == 1 else (many or one + "s"))


def minutes(seconds: float) -> str:
    """A length of time in words: "less than a minute", "14 minutes", "2 hours 5 minutes"."""
    whole = int(max(0.0, seconds) // 60)
    if whole < 1:
        return "less than a minute"
    if whole < 60:
        return count(whole, "minute")
    hours, rest = divmod(whole, 60)
    return count(hours, "hour") if rest == 0 else "%s %s" % (count(hours, "hour"), count(rest, "minute"))


# --------------------------------------------------------------------------- steps and pages

#: The four steps, in order: (key, name on the stepper).
STEPS: Tuple[Tuple[str, str], ...] = (
    ("samples", "Samples"), ("trimming", "Trimming"), ("assembly", "Assembly"), ("run", "Run"))
STEP_LABEL: Dict[str, str] = dict(STEPS)

#: The small line under "Trimming" and "Assembly" on the stepper.
STEP_STANDARD = "standard"
STEP_CHANGED = "changed"
STEP_NO_TRIMMING = "not used"
STEP_NO_SAMPLES = "none yet"
STEP_RUNNING = "running"
STEP_FINISHED = "finished"
STEP_STOPPED = "stopped"

PAGE_TITLE: Dict[str, str] = {
    "samples": "Samples",
    "trimming": "Trimming",
    "assembly": "Assembly",
    "run": "Run",
}
PAGE_INTRO: Dict[str, str] = {
    "samples": "Add the read files of your samples and choose where the results go. "
               "Each sample needs two files: Read 1 and Read 2.",
    "trimming": "Trimming cleans the reads before assembly. The standard settings are tuned "
                "for assembly. Most people never change them.",
    "assembly": "The standard settings suit most bacterial isolates. You can press Start "
                "without changing anything.",
    "run": "The samples are trimmed and assembled one at a time. When the run has finished, "
           "this page shows the results. You do not have to stay.",
}

#: Beside Start on the Samples page, the first time.
FIRST_TIME_HINT = ("The standard settings suit most bacterial isolates. You can press Start now, "
                   "or look at the options first.")

# --------------------------------------------------------------------------- buttons

BUTTON: Dict[str, str] = {
    # every page
    "back": "Back",
    "next": "Next: {step}",
    "start": "Start",
    "start_one": "Start (1 sample)",
    "start_many": "Start ({n} samples)",
    "reset": "Reset to standard",
    "more_options": "More options",
    # samples
    "add_files": "Add files...",
    "add_folder": "Add folder...",
    "remove": "Remove",
    "clear_list": "Clear list",
    "trim_all": "Trim all",
    "trim_none": "Trim none",
    "browse": "Browse...",
    "try_example": "Try the example",
    "make_names_different": "Make the names different (add the folder name)",
    "stop_looking": "Stop looking",
    "show": "Show",
    "check_again": "Check again",
    "continue_run": "Continue the run",
    "not_now": "Not now",
    # run
    "stop": "Stop",
    "stop_now": "Stop now",
    "keep_going": "Keep going after all",
    "show_log": "Show log",
    "hide_log": "Hide log",
    "open_results": "Open results folder",
    "open_summary": "Open summary",
    "open_report": "Open report",
    "open_folder": "Open folder",
    "open_log": "Open log",
    "continue": "Continue",
    "retry_failed_one": "Try the failed sample again",
    "retry_failed_many": "Try the {n} failed samples again",
    "new_run": "New run",
    "back_to_samples": "Back to the samples",
    "write_summary_again": "Write the summary again",
    # questions and boxes
    "keep_running": "Keep running",
    "stop_after": "Stop after {sample}",
    "stop_and_close": "Stop and close",
    "skip": "Skip them",
    "overwrite": "Overwrite them",
    "cancel": "Cancel",
    "close": "Close",
    "ok": "OK",
    "show_details": "Show details",
    "hide_details": "Hide details",
    "about": "About",
    "open_log_folder": "Open log folder",
    "save_report": "Save a problem report",
    "dismiss": "Dismiss",
}

# --------------------------------------------------------------------------- tables

#: Samples page: column key -> heading. ``trim`` is the tick column.
SAMPLES_COLUMNS: Dict[str, str] = {
    "trim": "Trim", "sample": "Sample", "read1": "Read 1", "read2": "Read 2",
    "size": "Size", "status": "Status",
}
#: Run page: column key -> heading.
RUN_COLUMNS: Dict[str, str] = {
    "sample": "Sample", "status": "Status", "contigs": "Contigs",
    "total_length": "Total length", "n50": "N50", "notes": "Notes",
}

#: The empty Samples list. The second form is for a PC where drag and drop is not available.
EMPTY_STATE = ("Drop FASTQ files or folders here, or use Add files / Add folder. "
               "Each sample needs two files: Read 1 and Read 2.")
EMPTY_STATE_NO_DROP = ("Use Add files or Add folder to choose your FASTQ files. "
                       "Each sample needs two files: Read 1 and Read 2.")
EMPTY_STATE_LINK_LEAD = "No data at hand?"
EXAMPLE_NOTE = ("This is a small practice sample. Press Start to see how a run looks. "
                "It takes about a minute.")
MORE_LANES = "(+{n} lanes)"

RESULTS_FOLDER_LABEL = "Results folder"
RESULTS_FOLDER_NOTE = "Each sample gets its own folder here."
RESULTS_FOLDER_SUGGESTED = "Suggested. Change it if you like."
TRIMMING_LABEL = "Trimming:"

# --------------------------------------------------------------------------- trimming options
# Keys are the field names of model.TrimSettings.

TRIM_LABEL: Dict[str, str] = {
    "remove_adapters": "Remove adapters",
    "trim_low_quality_ends": "Trim low-quality ends",
    "remove_poor_reads": "Remove poor-quality reads",
    "remove_short_reads": "Remove very short reads",
    "trim_polyg": "Trim poly-G tails",
    "keep_trimmed_reads": "Keep the trimmed reads",
    "end_quality": "Trim ends below quality",
    "min_mean_quality": "Remove reads with an average quality below",
    "max_low_percent": "...or with more than",
    "base_quality": "% of bases below quality",
    "min_length": "Remove reads shorter than",
    "trim_low_quality_starts": "Trim low-quality starts too",
    "trim_polyx": "Trim repeated-base tails",
    "fixed_trim": "Cut a fixed number of bases from every read:",
    "trim_front": "start",
    "trim_tail": "end",
    "own_adapters": "Use my own adapter sequences:",
    "adapter_r1": "Read 1",
    "adapter_r2": "Read 2",
}
#: Words that follow the field, where there are any.
TRIM_SUFFIX: Dict[str, str] = {"min_length": "bases"}

TRIM_HOVER: Dict[str, str] = {
    "remove_adapters":
        "Removes leftover adapters: short artificial pieces added to the DNA for sequencing, "
        "which are found automatically. Leave it on.",
    "trim_low_quality_ends":
        "Cuts off the end of a read where its quality becomes very poor; the cut is gentle on "
        "purpose, because harder trimming gives worse assemblies. Leave it on.",
    "remove_poor_reads":
        "Throws away reads that are poor along their whole length or contain many unknown "
        "bases (N). Leave it on.",
    "remove_short_reads":
        "Throws away reads that are very short after trimming, because they are too short to "
        "help the assembly. Leave it on.",
    "trim_polyg":
        "Removes the false run of G that NextSeq and NovaSeq machines can add at the end of a "
        "read. It acts only on data from those machines, so it is safe to leave on.",
    "keep_trimmed_reads":
        "Keeps the cleaned read files in each sample's trimming folder; they take about as much "
        "disk space as the original reads. Untick to delete them once a sample has been "
        "assembled (the trimming report is always kept).",
    "end_quality":
        "The quality below which the end of a read is cut off. 10 is gentle and suits assembly; "
        "higher numbers remove more and, in tests, gave shorter contigs.",
    "min_mean_quality":
        "Reads whose average quality is under this number are thrown away. Raise it only for "
        "an unusually poor run.",
    "max_low_percent":
        "A read is also thrown away when more than this share of its bases is of low quality. "
        "Change it only if you know that your run needs it.",
    "base_quality":
        "The quality below which a base counts as low for the percentage beside it. Change it "
        "only together with that percentage.",
    "min_length":
        "Reads shorter than this after trimming are thrown away. Do not go much below 50.",
    "trim_low_quality_starts":
        "Also cuts off the start of a read where its quality is very poor. Rarely needed: "
        "Illumina reads are usually good at the start, so leave it off unless your trimming "
        "report shows poor first bases.",
    "trim_polyx":
        "Cuts off a long run of one repeated base, such as AAAAAAAA, at the end of a read. "
        "Rarely needed for bacterial genomes: leave it off unless you were told that your "
        "library needs it.",
    "fixed_trim":
        "Removes a set number of bases from the start and the end of every read, whatever "
        "their quality. A few library kits need it; if nobody told you to use it, leave it off.",
    "trim_front":
        "How many bases to remove from the start of every read. Used only while the tick "
        "beside it is on.",
    "trim_tail":
        "How many bases to remove from the end of every read. Used only while the tick beside "
        "it is on.",
    "own_adapters":
        "Adapters are normally found automatically. Tick this only if your library kit uses "
        "unusual adapters and you know their sequences (letters A, C, G, T).",
    "adapter_r1":
        "The adapter sequence to remove from Read 1, written in the letters A, C, G and T. "
        "Copy it from the instructions of your library kit.",
    "adapter_r2":
        "The adapter sequence to remove from Read 2, written in the letters A, C, G and T. "
        "Leave it empty if your kit names only one adapter.",
}

#: Shown instead of the options when no sample has Trim ticked.
NO_TRIMMING_NOTICE = ("No sample is set to be trimmed. Tick Trim on the Samples page to use "
                      "these options.")

# --------------------------------------------------------------------------- assembly options
# Keys are the field names of model.AssemblySettings.

ASSEMBLY_LABEL: Dict[str, str] = {
    "know_genome_size": "I know the genome size: about",
    "genome_size_mb": "Genome size",
    "drop_short_contigs": "Leave out contigs shorter than",
    "min_contig": "Shortest contig kept",
    "mode": "Assembly mode",
    "scaffold": "Join contigs across gaps (scaffolding)",
    "polish": "Polish the final sequence",
    "force": "Assemble even if the read check fails",
    "threads": "Processor threads",
}
ASSEMBLY_SUFFIX: Dict[str, str] = {
    "genome_size_mb": "Mb",
    "min_contig": "bases",
    "threads": "of {n}",
}

ASSEMBLY_HOVER: Dict[str, str] = {
    "know_genome_size":
        "The size is used to judge your data and the result: is there enough data, and is the "
        "assembly about the right size? Tick it if you know the size (2.8 for Staphylococcus "
        "aureus, 5.5 for Klebsiella pneumoniae); otherwise about 5 Mb is assumed.",
    "genome_size_mb":
        "The size of the genome in megabases (millions of bases), for example 5.5. You can "
        "type 5.5 or 5,5.",
    "drop_short_contigs":
        "Very short contigs are mostly fragments of repeats. Tick this if the tool or database "
        "you will use next asks for a minimum length; unticked, every contig the assembler "
        "reports is kept.",
    "min_contig":
        "Contigs shorter than this many bases are left out of the result. 500 is a common choice.",
    "mode":
        "How the assembler balances speed, caution and contig length. Standard is the mode "
        "used in the published tests: choose another only if you have a reason.",
    "scaffold":
        "Uses the read pairs to put contigs in order and to close the gaps between them. "
        "Leave it on.",
    "polish":
        "Corrects small errors in the finished sequence, using the reads. Leave it on: turning "
        "it off saves a little time and costs accuracy.",
    "force":
        "Assembles a sample even when the check of its read files (complete, valid and "
        "matching) fails. Tick it only if you are sure the check is wrong for your data, "
        "because a result made from such reads may be wrong.",
    "threads":
        "How much of the processor the work may use: all threads is fastest and the result is "
        "the same either way. Lower it if you need this PC for other heavy work during the run.",
}

#: Keys are model.ASSEMBLY_MODES.
MODE_LABEL: Dict[str, str] = {
    "standard": "Standard (recommended)",
    "fast": "Fast",
    "careful": "Careful",
    "aggressive": "Aggressive",
}
MODE_HOVER: Dict[str, str] = {
    "standard": "Balanced, and the mode used in the published tests. Choose it unless you "
                "have a reason not to.",
    "fast": "Quicker: fewer steps and no polishing. For a first look, not for final results.",
    "careful": "More steps and two rounds of polishing. Slower, and not always better.",
    "aggressive": "Joins more, so contigs get longer, at the price of about one extra wrong "
                  "join per genome. Only if you know you want that trade.",
}

# --------------------------------------------------------------------------- other hover texts

HOVER: Dict[str, str] = {
    # every page
    "step": "Go to this step. Nothing you have set is lost.",
    "step_locked": "The steps cannot be changed while a run is in progress.",
    "back": "Goes back one step. Nothing you have set is lost.",
    "next": "Goes to the next step. You can press Start from any of the first three steps.",
    "start": "Trims and assembles the ready samples with the settings shown above. It begins "
             "at once.",
    "more_options": "Shows the settings that are rarely changed. All of them begin at the standard.",
    "reset_trimming": "Puts every setting on this page back to the standard, which is tuned "
                      "for assembly.",
    "reset_assembly": "Puts every setting on this page back to the standard.",
    # samples
    "add_files": "Choose FASTQ files: both files of each sample. You can select many at once.",
    "add_folder": "Adds every pair of FASTQ files found in a folder and its sub-folders.",
    "remove": "Takes the selected samples off the list. The files themselves are not touched.",
    "clear_list": "Empties the list. The files themselves are not touched.",
    "trim_column": "Ticked: the reads are cleaned before assembly. Leave it ticked unless the "
                   "reads were already trimmed elsewhere; trimming twice does no harm.",
    "trim_all": "Ticks Trim for every sample.",
    "trim_none": "Unticks Trim for every sample. Use it when all the reads were trimmed already.",
    "sample_column": "The name used for this sample's folder and files. Double-click to change it.",
    "read1_column": "The first read file of the sample. Rest the mouse on a name to see where "
                    "the file is.",
    "read2_column": "The second read file of the sample. Rest the mouse on a name to see where "
                    "the file is.",
    "size_column": "Size of the two read files together.",
    "status_column": "Ready, or the reason why a sample is left out. Rest the mouse on a row "
                     "to read it in full.",
    "results_folder": "Where the results are written; each sample gets its own folder there. "
                      "A folder on this PC, outside OneDrive, is fastest.",
    "browse": "Choose the results folder.",
    "try_example": "Loads a small practice sample that comes with the program. Nothing is "
                   "downloaded.",
    "make_names_different": "Adds the name of its folder to every sample that shares its name "
                            "with another, so that all names differ.",
    "left_out_link": "Selects the samples that are left out, so that you can read why.",
    # run
    "stop": "Stops the run. You are asked first, and told what would be lost.",
    "show_log": "Shows the technical messages of the trimmer and the assembler. You do not "
                "need them unless something goes wrong.",
    "open_results": "Opens the folder with all the results in Explorer.",
    "open_summary": "Opens the summary of all samples in your web browser.",
    "open_report": "Opens this sample's assembly report in your web browser.",
    "open_folder": "Opens this sample's folder in Explorer.",
    "open_log": "Opens the technical messages of this sample's failed attempt.",
    "continue": "Carries on with the samples that were stopped or not started. Finished "
                "samples are left alone.",
    "retry_failed": "Runs the failed samples once more. Do this after you have fixed what "
                    "their notes point to.",
    "new_run": "Empties the list and goes back to the Samples step. The results stay where "
               "they are.",
    "back_to_samples": "Goes back to the Samples step and keeps the list.",
    "run_sample_column": "The name of the sample.",
    "run_status_column": "Where this sample stands. Select a row to read its note in full.",
    "contigs_column": "Number of pieces in the assembly. Fewer is usually better.",
    "total_length_column": "All contigs added together. It should be close to the genome size "
                           "of your organism.",
    "n50_column": "Half of the assembly is in contigs at least this long. Larger is better.",
    "notes_column": "What to know about this sample, in words. Select a row to read it in full.",
}

# --------------------------------------------------------------------------- status words

SAMPLE_STATE_LABEL: Dict[SampleState, str] = {
    SampleState.READY: "Ready",
    SampleState.READY_NOTE: "Ready with a note",
    SampleState.LEFT_OUT: "Left out",
}

RUN_STATUS_LABEL: Dict[RunStatus, str] = {
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

#: The chips of the running sample.
STAGE_LABEL: Dict[Stage, str] = {
    Stage.JOIN_LANES: "Join lanes",
    Stage.TRIM: "Trim",
    Stage.CHECK_READS: "Check reads",
    Stage.ASSEMBLE: "Assemble",
    Stage.SUMMARISE: "Summarise",
}
#: What a chip says when the mouse rests on it.
STAGE_STATE_LABEL: Dict[str, str] = {
    "waiting": "not started yet",
    "running": "in progress",
    "done": "finished",
    "skipped": "not needed for this sample",
    "failed": "did not succeed",
}

STOP_MODE_LABEL: Dict[StopMode, str] = {
    StopMode.NOW: BUTTON["stop_now"],
    StopMode.AFTER_CURRENT: BUTTON["stop_after"],
}
EXISTING_LABEL: Dict[Existing, str] = {
    Existing.SKIP: BUTTON["skip"],
    Existing.OVERWRITE: BUTTON["overwrite"],
}
END_REASON_LABEL: Dict[EndReason, str] = {
    EndReason.COMPLETED: "Finished",
    EndReason.STOPPED: "Stopped",
    EndReason.ENVIRONMENT: "Stopped: this PC needs attention",
}

# --------------------------------------------------------------------------- run page lines

STARTING = "Starting..."
ADVICE = ("This PC stays awake until the run has finished. You can lock the screen "
          "(Windows key + L). Do not shut down, do not put the PC to sleep and, on a laptop, "
          "do not close the lid.")
SILENCE_IS_NORMAL = "Some steps show nothing new for several minutes. That is normal."
ESTIMATE_PENDING = "The first sample will show how long the rest should take."
STOPPING_AFTER = "Stopping after {sample}..."
NOTHING_LOST = "Nothing that was finished has been lost."
SELECT_A_ROW = "Select a row to see its note, its report and its folder."
THIN_DATA_RETRY = "Very little data: trying again with settings for thin data."
ON_BATTERY = ("Running on battery. Connect the charger: a long run can empty the battery, and "
              "Windows may then put the PC to sleep or switch it off.")
ALREADY_OPEN = "%s is already open." % branding.APP_NAME
STILL_IN_ZIP = ("%s is still inside the zip file. Close this window, right-click the zip file, "
                "choose Extract All, then start %s from the extracted folder."
                % (branding.APP_NAME, branding.APP_NAME))

# --------------------------------------------------------------------------- field messages

RENAME_BAD_CHARACTERS = "Use letters, numbers, - _ and . only."
RENAME_DUPLICATE = "There is already a sample with this name."
RENAME_TOO_LONG = "Too long: 60 characters at most."
RENAME_EMPTY = "A sample needs a name."
RENAME_HINT = "Type the new name. Enter accepts it, Esc keeps the old one."


def number_reset(minimum: float, maximum: float, last_good: float, decimal: bool = False,
                 decimals: int = 1) -> str:
    """Told in the help line when a number field is left with something it cannot use.

    ``"Use a whole number from 0 to 40. Set back to 10."``
    """
    if decimal:
        return "Use a number from %g to %g. Set back to %.*f." % (minimum, maximum, decimals, last_good)
    return "Use a whole number from %s to %s. Set back to %s." % (
        number(int(minimum)), number(int(maximum)), number(int(last_good)))


# --------------------------------------------------------------------------- sentences with numbers

def start_label(ready: int) -> str:
    """The Start button: "Start", "Start (1 sample)", "Start (38 samples)"."""
    if ready <= 0:
        return BUTTON["start"]
    return BUTTON["start_one"] if ready == 1 else BUTTON["start_many"].format(n=number(ready))


def next_label(step_key: str) -> str:
    """ "Next: Trimming" """
    return BUTTON["next"].format(step=STEP_LABEL[step_key])


def step_ready(ready: int) -> str:
    """Under "Samples" on the stepper: "40 ready"."""
    return STEP_NO_SAMPLES if ready <= 0 else "%s ready" % number(ready)


def ready_counter(ready: int, left_out: int) -> Tuple[str, str]:
    """Under the list: the plain part and the part that is a link.

    ``("38 ready.", "2 left out: why?")``; the second is empty when nothing is left out.
    """
    plain = "%s ready." % number(ready)
    return plain, ("%s left out: why?" % number(left_out) if left_out > 0 else "")


def start_reason(samples: int, ready: int, has_results_folder: bool) -> str:
    """Why Start is disabled; empty when it is not."""
    if samples <= 0:
        return "Add samples to begin."
    if ready <= 0:
        return "No sample is ready: see the notes in the list."
    if not has_results_folder:
        return "Choose a results folder to begin."
    return ""


def settings_line(trimming: Optional[bool], assembly_standard: bool, threads: int) -> str:
    """ "Settings: standard trimming, standard assembly, 8 threads."

    ``trimming``: True standard, False changed, None when no sample is trimmed.
    """
    trim_words = "no trimming" if trimming is None else (
        "%s trimming" % (STEP_STANDARD if trimming else STEP_CHANGED))
    return "Settings: %s, %s assembly, %s." % (
        trim_words, STEP_STANDARD if assembly_standard else STEP_CHANGED, count(threads, "thread"))


def sample_header(index: int, total: int, name: str) -> str:
    """ "Sample 13 of 40: KP_013" """
    return "Sample %s of %s: %s" % (number(index), number(total), name)


def clocks(sample_time: str, run_time: str) -> str:
    """ "This sample 06:41. Whole run 2 h 14 min." (times already formatted)"""
    return "This sample %s. Whole run %s." % (sample_time, run_time)


def time_left(seconds: Optional[float]) -> str:
    """ "About 4 hours left (rough estimate)." or the sentence used before the first sample."""
    if seconds is None:
        return ESTIMATE_PENDING
    if seconds < 60:
        return "Less than a minute left (rough estimate)."
    whole = int(seconds // 60)
    if whole < 90:
        return "About %s left (rough estimate)." % count(whole, "minute")
    return "About %s left (rough estimate)." % count(int(round(whole / 60.0)), "hour")


def working_line(busy_threads: float, memory: str) -> str:
    """ "Working: 8 processor threads busy, 3.1 GB of memory in use" (memory already formatted)"""
    threads = max(0, int(round(busy_threads)))
    return "Working: %s busy, %s of memory in use" % (count(threads, "processor thread"), memory)


def space_line(needed: str, drive: str, free: str) -> str:
    """ "Needs about 61 GB. Free on D: 212 GB." """
    return "Needs about %s. Free on %s %s." % (needed, drive, free) if drive else \
        "Needs about %s. Free: %s." % (needed, free)


def existing_line(existing: int, failed_before: int = 0) -> str:
    """Under the results folder: "12 of these samples already have results here." """
    if existing <= 0 and failed_before <= 0:
        return ""
    parts = []
    if existing > 0:
        parts.append("%s of these samples already %s results here."
                     % (number(existing), "has" if existing == 1 else "have"))
    if failed_before > 0:
        parts.append("%s failed last time and will be tried again." % number(failed_before))
    return " ".join(parts)


def missing_part(file_name: str) -> str:
    return ("Part of the program is missing: %s. Antivirus software sometimes removes it by "
            "mistake. Extract the zip again. If it disappears again, ask your IT support to "
            "allow the %s folder." % (file_name, branding.APP_NAME))


def blocked_part(file_name: str) -> str:
    return ("Windows or your antivirus stopped part of the program from starting (%s). %s "
            "cannot assemble until it is allowed. Ask your IT support to allow the %s folder."
            % (file_name, branding.APP_NAME, branding.APP_NAME))


def low_memory(free: str) -> str:
    return ("Only %s of memory is free. Close other programs, web browsers above all, for a "
            "faster and safer run." % free)


def slept(from_time: str, to_time: str) -> str:
    return "The PC was asleep from %s to %s. The run carried on when it woke up." % (from_time, to_time)


def battery_low(percent: int) -> str:
    return "Battery at %d %%. Connect the charger now, or the run will be interrupted." % percent


# --------------------------------------------------------------------------- questions

@dataclass(frozen=True)
class Choice:
    """One button of a question. ``kind``: "plain" or "danger". ``description`` is the
    sentence shown beside the button when the question lists its choices."""
    key: str
    label: str
    kind: str = "plain"
    description: str = ""


@dataclass(frozen=True)
class Question:
    """Everything a dialog needs: ``widgets.Dialog.ask(parent, question)`` shows it and
    returns the key of the chosen button. Esc and the close box give ``cancel``."""
    title: str
    body: str
    choices: Tuple[Choice, ...]
    default: str
    cancel: str
    details: str = ""
    details_label: str = ""

    def choice(self, key: str) -> Choice:
        for item in self.choices:
            if item.key == key:
                return item
        raise KeyError(key)


_ACTIVITY: Dict[RunStatus, str] = {
    RunStatus.JOINING_LANES: "is being prepared",
    RunStatus.TRIMMING: "is being trimmed",
    RunStatus.CHECKING_READS: "is being checked",
    RunStatus.ASSEMBLING: "is being assembled",
    RunStatus.SUMMARISING: "is being summarised",
}


def _finished_kept(finished: int) -> str:
    if finished <= 0:
        return "No other sample is affected."
    if finished == 1:
        return "The 1 sample already finished is kept."
    return "The %s samples already finished are kept." % number(finished)


def stop_question(sample: str, status: RunStatus, seconds: float, finished: int) -> Question:
    """The Stop button. Keys: ``"keep"`` (default, also Esc), ``StopMode.AFTER_CURRENT.value``,
    ``StopMode.NOW.value``."""
    activity = _ACTIVITY.get(status, "is being worked on")
    body = ("%s %s (%s so far).\n"
            "If you stop now, that work is thrown away and %s starts from the beginning next time.\n"
            "%s" % (sample, activity, minutes(seconds), sample, _finished_kept(finished)))
    return Question(
        title="Stop the run?", body=body,
        choices=(Choice("keep", BUTTON["keep_running"]),
                 Choice(StopMode.AFTER_CURRENT.value, BUTTON["stop_after"].format(sample=sample)),
                 Choice(StopMode.NOW.value, BUTTON["stop_now"], kind="danger")),
        default="keep", cancel="keep")


def close_question(sample: str, status: RunStatus, seconds: float, finished: int) -> Question:
    """Closing the window during a run. Keys: ``"keep"`` (default, also Esc), ``"stop_and_close"``."""
    kept = ("The %s are kept, and you can continue later." % (
        "1 finished sample" if finished == 1 else "%s finished samples" % number(finished))
        if finished > 0 else "You can continue later.")
    body = ("A run is in progress. Closing the program stops it.\n"
            "%s %s (%s so far); that work would be lost.\n%s"
            % (sample, _ACTIVITY.get(status, "is being worked on"), minutes(seconds), kept))
    return Question(
        title="Close %s?" % branding.APP_NAME, body=body,
        choices=(Choice("keep", BUTTON["keep_running"]),
                 Choice("stop_and_close", BUTTON["stop_and_close"], kind="danger")),
        default="keep", cancel="keep")


def overwrite_question(existing: int, total: int, different: int = 0, settings_summary: str = "",
                       names: Sequence[str] = ()) -> Question:
    """Asked once when Start is pressed and some samples are already assembled.

    Keys: ``Existing.SKIP.value``, ``Existing.OVERWRITE.value``, ``"cancel"`` (Esc, close box).
    When every sample is already assembled, Skip would do nothing and is not offered;
    Cancel is then the default.
    """
    replaced = "Each old result is replaced when its new one is ready."
    details = "\n".join(names)
    details_label = "Show the %s" % count(len(names), "sample") if names else ""
    different_line = ""
    if different > 0:
        made = "It was" if existing == 1 else (
            "All of them were" if different >= existing else "%s of them were" % number(different))
        different_line = "\n%s made with different settings%s." % (
            made, " (%s)" % settings_summary if settings_summary else "")
    if existing >= total:
        subject = ("This sample already has" if total == 1 else
                   "All %s samples already have" % number(total))
        again = "Assemble it again?" if total == 1 else "Assemble them again?"
        body = "%s an assembly in this results folder.%s\n%s %s" % (
            subject, different_line, again, replaced)
        return Question(
            title="These samples are already assembled" if total != 1 else "This sample is already assembled",
            body=body,
            choices=(Choice(Existing.OVERWRITE.value,
                            BUTTON["overwrite"] if total != 1 else "Overwrite it", kind="danger"),
                     Choice("cancel", BUTTON["cancel"])),
            default="cancel", cancel="cancel", details=details, details_label=details_label)
    have = "already has" if existing == 1 else "already have"
    those = "that one" if existing == 1 else "those %s" % number(existing)
    body = "%s of the %s samples %s an assembly in this results folder.%s\nWhat should happen to %s?" % (
        number(existing), number(total), have, different_line, those)
    rest = total - existing
    return Question(
        title="Some samples are already assembled", body=body,
        choices=(Choice(Existing.SKIP.value, BUTTON["skip"] if existing != 1 else "Skip it",
                        description="Keep the existing results. Assemble only the other %s." % number(rest)),
                 Choice(Existing.OVERWRITE.value, BUTTON["overwrite"] if existing != 1 else "Overwrite it",
                        kind="danger", description="Assemble them again. " + replaced
                        if existing != 1 else "Assemble it again. The old result is replaced when the new one is ready."),
                 Choice("cancel", BUTTON["cancel"])),
        default=Existing.SKIP.value, cancel="cancel", details=details, details_label=details_label)
