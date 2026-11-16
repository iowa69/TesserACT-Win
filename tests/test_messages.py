# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""messages: every check line, engine ending and failure of the PC in plain words.

The exact sentences are those of ``docs/superpowers/audits/ux-windows.md`` section A-9.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from tesseract_win import branding, messages, readcheck
from tesseract_win.engine_proc import Progress
from tesseract_win.messages import Explained
from tesseract_win.model import EndReason
from tesseract_win.osapi import ExitKind
from tesseract_win.readcheck import CheckLine, InputProblem

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Words a beginner must never meet on screen (plan, global constraints), and raw verdicts.
JARGON = re.compile(r"\b(mate|mates|Phred|k-mer|kmer|gzip|FAIL|WARN|stderr|exit code|errno)\b")

READ_CHECK_LINES = [
    CheckLine("FAIL", "file", "cannot read '/data/KP_3_R1.fastq.gz'"),
    CheckLine("FAIL", "file", "'/data/KP_3_R1.fastq.gz' is empty"),
    CheckLine("FAIL", "gzip", "'/data/KP_3_R2.fastq.gz' is truncated or corrupt (gzip -t failed)"),
    CheckLine("FAIL", "pairing", "-1 and -2 are the same file"),
    CheckLine("FAIL", "format", "no FASTQ records read"),
    CheckLine("FAIL", "format", "3 malformed records (first at pair 7); is this FASTQ?"),
    CheckLine("FAIL", "format", "2 records whose sequence and quality lengths differ"),
    CheckLine("FAIL", "pairing", "the -1 file has 5 fewer reads than the -2 file (truncated, or not the mate file)"),
    CheckLine("FAIL", "pairing", "the -2 file has 5 fewer reads than the -1 file (truncated, or not the mate file)"),
    CheckLine("FAIL", "pairing", "12 pairs whose mate names differ (first: r1); the files are not in the same "
                                 "order, or one is truncated"),
    CheckLine("FAIL", "quality", "-1 file: quality characters below '!' (code 10): not FASTQ Phred+33"),
    CheckLine("FAIL", "quality", "-2 file: qualities look Phred+64 (codes 66-104); convert them"),
    CheckLine("WARN", "depth", "about 8x for a 5 Mb genome (assumed; set --genome-size for a better estimate): "
                               "too thin for a reliable assembly; expect many breaks"),
    CheckLine("WARN", "depth", "about 22x for a 5.5 Mb genome: low; dropouts are likely"),
    CheckLine("WARN", "depth", "about 600x for a 5.0 Mb genome: very deep; runtime and memory grow with depth, "
                               "results do not improve beyond ~150x"),
    CheckLine("WARN", "adapters", "12.3% of read pairs carry Illumina adapter sequence: trim them"),
    CheckLine("WARN", "poly-G", "4.0% of read pairs end in a poly-G run (two-colour chemistry): trim them"),
    CheckLine("WARN", "N bases", "6.1% of bases are N"),
    CheckLine("FAIL", "disk", "only 3 GB free in assembly; need about 61 GB"),
    CheckLine("WARN", "memory", "only 2 GB available; a bacterial assembly needs 2-6 GB"),
]

SUMMARY_LINES = [
    CheckLine("FAIL", "exit status", "1 (see assembly/assembly.log)"),
    CheckLine("FAIL", "file", "contigs.fasta missing or empty"),
    CheckLine("FAIL", "file", "report.json missing or empty"),
    CheckLine("FAIL", "contigs.fasta", "malformed: 1 duplicate names, 0 empty records, 0 lines with non-ACGTN characters"),
    CheckLine("WARN", "size", "total 7.90 Mb vs ~5.0 Mb expected: contamination, a mixed sample, or the wrong genome size"),
    CheckLine("FAIL", "report.json", "unreadable"),
    CheckLine("WARN", "self-check", "k-mer completeness 99.1, QV 51; about 4.2 kb of sequence seen in the reads at "
                                    ">10x genome depth is not in the assembly (a small high-copy plasmid, rRNA or "
                                    "IS copies); see p2_self_qa.missing_hi.tsv"),
    CheckLine("WARN", "spike-in", "1 PhiX record(s) labelled _spikein: remove them before submission"),
    CheckLine("WARN", "library", "pair orientation check 'outward': not a standard FR library; pair-based joins "
                                 "were disabled"),
    CheckLine("WARN", "thin data", "the default k ladder assembled almost nothing; this result uses k = 21,33,55"),
]


def plain(found: Explained) -> None:
    """A finding is two real sentences without jargon and without a raw file path prefix."""
    assert found.note and found.note[0].isupper() and found.note.rstrip()[-1] in ".?", found.note
    assert found.next_step and found.next_step.rstrip()[-1] in ".?", found
    assert not JARGON.search(found.note + " " + found.next_step), found
    assert found.code


@pytest.mark.parametrize("line", READ_CHECK_LINES, ids=lambda l: "%s-%s" % (l.name, l.message[:18]))
def test_every_read_check_line_becomes_a_note_and_a_next_step(line):
    for trimmed in (False, True):
        plain(messages.explain_read_check(line, trimmed=trimmed))


@pytest.mark.parametrize("line", SUMMARY_LINES, ids=lambda l: "%s-%s" % (l.name, l.message[:18]))
def test_every_summary_line_becomes_a_note_and_a_next_step(line):
    plain(messages.explain_summary(line))


def test_lines_that_say_all_is_well_give_nothing():
    assert messages.explain_read_check(CheckLine("OK", "depth", "about 60x for a 5 Mb genome")) is None
    assert messages.explain_summary(CheckLine("OK", "exit status", "0")) is None


def test_checks_this_version_does_not_know_still_get_a_sentence():
    plain(messages.explain_read_check(CheckLine("FAIL", "something new", "x")))
    assert messages.explain_read_check(CheckLine("WARN", "something new", "x")).note.endswith(".")
    plain(messages.explain_summary(CheckLine("FAIL", "something new", "x")))
    assert messages.explain_summary(CheckLine("WARN", "something new", "x")).note.endswith(".")


def test_the_catalogue_of_failures_word_for_word():
    """Audit A-9, rows 9.1 to 9.5."""
    damaged = messages.explain_read_check(READ_CHECK_LINES[2])
    assert damaged.text == ("A read file is incomplete or damaged (KP_3_R2.fastq.gz). It may have been cut "
                            "short while copying or downloading. Copy it again from the original, then try again.")
    for index in (7, 8, 9):
        assert messages.explain_read_check(READ_CHECK_LINES[index]).text == (
            "Read 1 and Read 2 do not match: one file has fewer reads or they are in a different order. "
            "Use the two original files from the sequencer.")
    for index in (4, 5, 10, 11):
        assert messages.explain_read_check(READ_CHECK_LINES[index]).text == (
            "The read files are not in the standard FASTQ format the program needs. Ask whoever produced "
            "them for standard Illumina FASTQ files.")
    assert messages.explain_summary(SUMMARY_LINES[1]).text == (
        "Too little data to assemble: no contigs were produced. This sample needs more sequencing.")
    memory = messages.explain_engine_end("tesseract-asm", ExitKind.OUT_OF_MEMORY)
    assert memory.text == ("This sample needs more memory than this PC could give. Close other programs and "
                           "try again, or assemble it on a PC with more memory.")
    assert messages.explain_read_check(READ_CHECK_LINES[0]).note == \
        "The read files can no longer be read (/data/KP_3_R1.fastq.gz)."
    assert "Is the drive still connected?" in messages.explain_read_check(READ_CHECK_LINES[0]).next_step


def test_data_failures_are_marked_as_the_sample_s_and_disk_as_the_pc_s():
    assert all(messages.explain_read_check(line).about_data for line in READ_CHECK_LINES[1:18])
    assert not messages.explain_read_check(READ_CHECK_LINES[0]).about_data       # the drive may be gone
    disk = messages.explain_read_check(READ_CHECK_LINES[18])
    assert disk.environment == "disk_low" and "3 GB free, about 61 GB needed" in disk.note
    assert not messages.explain_read_check(READ_CHECK_LINES[19]).environment


def test_warnings_carry_their_numbers():
    assert messages.explain_read_check(READ_CHECK_LINES[13]).note == \
        "Low depth (about 22x): expect a fragmented assembly."
    thin = messages.explain_read_check(READ_CHECK_LINES[12]).note
    assert thin.startswith("Very low depth (about 8x)") and "5 Mb" in thin and "I know the genome size" in thin
    assert "600x" in messages.explain_read_check(READ_CHECK_LINES[14]).note
    adapters = messages.explain_read_check(READ_CHECK_LINES[15], trimmed=False)
    assert adapters.note.startswith("Adapter sequence is still in 12.3 % of the read pairs")
    assert "Tick Trim" in adapters.next_step
    assert "Remove adapters" in messages.explain_read_check(READ_CHECK_LINES[15], trimmed=True).next_step
    assert "Trim poly-G tails" in messages.explain_read_check(READ_CHECK_LINES[16], trimmed=True).next_step
    assert "7.90 Mb" in messages.explain_summary(SUMMARY_LINES[4]).note
    assert "4.2 kb" in messages.explain_summary(SUMMARY_LINES[6]).note
    assert messages.explain_summary(SUMMARY_LINES[7]).note.startswith(
        "The assembly holds 1 contig of the sequencer's control DNA")


@pytest.mark.parametrize("code, expected", [
    ("unreadable", "reads_gone"), ("empty", "input_empty"), ("damaged", "input_damaged"),
    ("length_mismatch", "input_damaged"), ("same_file", "input_same_file"), ("no_reads", "input_format"),
    ("not_fastq", "input_format"), ("fewer_reads", "input_mismatch"), ("names_differ", "input_mismatch"),
    ("blank_lines", "input_blank_lines"), ("line_ends", "input_line_ends"),
])
def test_every_problem_of_a_raw_pair(code, expected):
    problem = InputProblem(code, "The file has a problem.", "Do something about it.", "/data/KP_1_R2.fastq.gz")
    found = messages.explain_input_problem(problem)
    assert found.code == expected
    plain(found)
    if code == "damaged":
        assert "(KP_1_R2.fastq.gz)" in found.note


def test_real_problems_of_a_raw_pair_are_explained(tmp_path):
    good = tmp_path / "a_R1.fastq"
    good.write_text("@r1\nACGT\n+\nIIII\n@r2\nACGT\n+\nIIII\n")
    short = tmp_path / "a_R2.fastq"
    short.write_text("@r1\nACGT\n+\nIIII\n")
    check = readcheck.check_input_pair(str(good), str(short))
    assert not check.ok
    assert messages.explain_input_problem(check.problems[0]).code == "input_mismatch"


# --------------------------------------------------------------------------- engines

@pytest.mark.parametrize("engine, word", [("scepter", "trimmer"), ("tesseract-asm", "assembler"),
                                          (r"C:\x\engines\scepter.exe", "trimmer")])
def test_every_way_an_engine_can_end(engine, word):
    assert messages.explain_engine_end(engine, ExitKind.OK) is None
    for kind in ExitKind:
        if kind is ExitKind.OK:
            continue
        found = messages.explain_engine_end(engine, kind, exe="/app/engines/x.exe")
        plain(found)
    crash = messages.explain_engine_end(engine, ExitKind.CRASH)
    assert crash.text == ("The %s stopped unexpectedly. This is a fault in the program, not in your data. The "
                          "other samples continue. Please send a problem report (About, Save a problem report)."
                          % word)
    hung = messages.explain_engine_end(engine, ExitKind.CRASH, hung=True)
    assert hung.note == "The %s stopped responding and was ended after 30 minutes without activity." % word
    assert hung.code != crash.code


def test_engine_endings_that_are_the_pc_s_failure():
    missing = messages.explain_engine_end("scepter", ExitKind.COULD_NOT_START, exe="/app/engines/scepter.exe")
    assert missing.environment == "engine_missing" and missing.path == "/app/engines/scepter.exe"
    blocked = messages.explain_engine_end("tesseract-asm", ExitKind.BLOCKED, exe="/app/engines/tesseract-asm.exe")
    assert blocked.environment == "engine_blocked" and "antivirus" in blocked.note
    full = messages.explain_engine_end("tesseract-asm", ExitKind.ENGINE_ERROR,
                                       log_tail="error: cannot write to assembly/contigs.fasta")
    assert full.environment == "results_unwritable"


def test_the_tail_of_the_log_decides_what_an_engine_error_means():
    tail = ("[detect] whole_file sample\nerror: /data/KP_3_R2.fastq.gz: record 'M01:1' has sequence length 150 "
            "but quality length 45\nRun 'scepter --help' for usage.")
    damaged = messages.explain_engine_end("scepter", ExitKind.ENGINE_ERROR, log_tail=tail)
    assert damaged.code == "input_damaged" and "(KP_3_R2.fastq.gz)" in damaged.note and damaged.about_data
    for text in ("error: x.fq: truncated record (missing '+' line)", "error: x.fq: malformed record 501 (expected '@', got 'X')"):
        assert messages.explain_engine_end("scepter", ExitKind.ENGINE_ERROR, log_tail=text).code == "input_damaged"
    gone = messages.explain_engine_end("scepter", ExitKind.ENGINE_ERROR,
                                       log_tail="error: input file 'x' (-i/--in1) does not exist")
    assert gone.code == "reads_gone"
    memory = messages.explain_engine_end(
        "tesseract-asm", ExitKind.CRASH, log_tail="terminate called after throwing an instance of 'std::bad_alloc'")
    assert memory.code == "out_of_memory"
    other = messages.explain_engine_end("tesseract-asm", ExitKind.ENGINE_ERROR, log_tail="error: k must be odd")
    assert other.code == "engine_error:assembler" and "k must be odd" not in other.text
    assert "log" in other.note


def test_trimming_that_ends_well_but_cannot_be_used():
    for problem in ("no_reads_left", "files_missing", "report_unreadable"):
        plain(messages.explain_trimming_result(problem, "trimming_report.json"))
    assert messages.explain_trimming_result("no_reads_left").about_data
    assert "(a.json)" in messages.explain_trimming_result("files_missing", "a.json").note
    plain(messages.internal_error())
    assert branding.APP_NAME in messages.internal_error().note


# --------------------------------------------------------------------------- the PC's failures

def test_the_batch_endings_of_rows_9_7_to_9_9():
    assert messages.environment_stop("results_gone", r"E:\Results", finished=12) == (
        "Stopped: the results folder can no longer be reached (E:\\Results). Reconnect the drive, then press "
        "Continue. The 12 finished samples are safe.")
    full = messages.environment_stop("disk_full", "/mnt/results", finished=1)
    assert full.startswith("Stopped: ") and "is full. Free some space, then press Continue." in full
    assert full.endswith("The 1 finished sample is safe.")
    gone = messages.environment_stop("engine_missing", os.path.join("engines", "tesseract-asm.exe"), finished=0)
    assert gone == ("Stopped: part of the program has disappeared (%s). Antivirus software sometimes removes it "
                    "by mistake. Extract the zip again and press Continue. If it happens again, ask your IT "
                    "support to allow the TesserACT-Win folder." % os.path.join("engines", "tesseract-asm.exe"))
    assert "safe" not in gone


def test_drive_words():
    assert messages.drive_words(r"D:\Results") in ("drive D:", "the drive that holds D:\\Results")
    assert messages.drive_words("") == "the drive"
    if os.name == "nt":
        assert messages.environment_stop("disk_full", r"d:\x") .startswith("Stopped: drive D: is full.")


@pytest.mark.parametrize("code", ["results_gone", "results_unwritable", "disk_full", "disk_low", "engine_missing",
                                  "engine_broken", "engine_blocked", "reads_gone", "same_failure", "folder_in_use",
                                  "newer_version", "no_samples", "internal", "never heard of it"])
def test_every_environment_code_gives_whole_sentences(code):
    for text in (messages.environment_stop(code, "/x/results", finished=3, detail="about 61 GB needed, 12 GB free"),
                 messages.start_problem(code, "/x/results", detail="Ada on LAB-PC")):
        assert text[0].isupper() and text.rstrip().endswith(".") and "%" not in text and "  " not in text
        assert not JARGON.search(text)
    found = messages.explain_environment(code, "/x/results")
    assert found.environment == code and found.note.endswith(".") and found.next_step.endswith(".")
    assert "press Start again" not in messages.environment_stop(code, "/x")
    assert "press Continue" not in messages.start_problem(code, "/x")


def test_start_problems_name_what_is_wrong():
    assert "another run (Ada on LAB-PC)" in messages.start_problem("folder_in_use", "/r", detail="Ada on LAB-PC")
    assert "newer version of TesserACT-Win" in messages.start_problem("newer_version", "/r")
    low = messages.start_problem("disk_low", "/r", detail="about 61 GB needed, 12 GB free")
    assert "(about 61 GB needed, 12 GB free)" in low and "Free some space" in low


# --------------------------------------------------------------------------- endings of a sample

def test_redo_and_repeat_wordings():
    note = "The assembler stopped unexpectedly."
    assert messages.with_earlier_kept(note, "2026-11-03T10:00:00") == \
        "The assembler stopped unexpectedly. The earlier assembly (3 Nov 2026) was kept."
    assert messages.with_earlier_kept(note) == "The assembler stopped unexpectedly. The earlier assembly was kept."
    assert messages.stopped_note(True) == "Stopped. The earlier assembly was kept."
    assert messages.stopped_note(False).startswith("Stopped")
    twice = messages.second_time(note)
    assert twice == note + " This is the second time. Trying again will not help unless something changes."
    assert messages.second_time(twice) == twice
    assert messages.skipped_note("2026-11-03T10:00:00") == "Already assembled on 3 Nov 2026."
    assert messages.skipped_note("2026-11-03T10:00:00", same_settings=False) == \
        "Already assembled on 3 Nov 2026, with other settings."
    assert messages.skipped_note("") == "Already assembled."
    assert messages.new_result_note("KP_001") == (
        "Done, but the old result could not be replaced because one of its files is open in another program. "
        "The new result is in KP_001%snew_result." % os.sep)
    assert "did not make" in messages.new_result_note("KP_001", "not_ours")
    environment = messages.explain_environment("results_gone", "/r")
    assert messages.stopped_by_environment(environment).startswith(
        "Not finished: the results folder can no longer be reached (/r).")
    assert messages.stopped_by_environment(environment, True).endswith("The earlier assembly was kept.")
    for text in (messages.left_out_same_name("KP_1"), messages.left_out_foreign_folder("KP_1", "assembly"),
                 messages.left_out_reserved_name("assemblies")):
        assert text.endswith(".") and "Rename this sample" in text


def test_dates_and_times():
    assert messages.date_words("2026-11-03T14:02:11") == "3 Nov 2026"
    assert messages.date_words("2026-01-31") == "31 Jan 2026"
    assert messages.date_words("yesterday") == "" and messages.date_words("") == ""
    assert messages.date_words("2026-13-01") == ""
    assert messages.clock_words("2026-11-03T04:02:11") == "04:02"
    assert re.fullmatch(r"\d\d:\d\d", messages.clock_words(1_800_000_000))
    assert messages.clock_words("") == ""
    assert messages.count_words(1, "sample") == "1 sample" and messages.count_words(5482, "read") == "5,482 reads"


# --------------------------------------------------------------------------- progress and notices

def test_the_seven_steps_in_words():
    assert messages.step_text(Progress(step=3, total_steps=7, detail="de Bruijn iterations", k=55)) == \
        "Step 3 of 7: building the assembly graph (k = 55)"
    assert messages.step_text(Progress(step=1, total_steps=7, detail="loading reads")) == \
        "Step 1 of 7: reading the reads"
    assert messages.step_text(Progress(step=7, total_steps=7, detail="writing output")) == \
        "Step 7 of 7: writing the results"
    assert messages.step_text(Progress(step=9, total_steps=9, detail="a new step")) == "Step 9 of 9: a new step"
    assert messages.step_text(Progress(fraction=0.5)) == ""
    assert "second attempt" in messages.step_text(Progress(step=2, total_steps=7), retry=True)
    assert len(messages.ASSEMBLY_STEPS) == 7


def test_percent_lines():
    assert messages.trimming_text(0.47) == "Trimming: 47 %"
    assert messages.checking_text(0.62) == "Checking reads: 62 %"
    assert messages.checking_text(7.0) == "Checking reads: 100 %"
    assert messages.joining_text(0.5) == "Joining lanes: 50 %" and messages.joining_text() == "Joining lanes"
    assert messages.pair_check_text(0.1).endswith(": 10 %")
    assert messages.THIN_DATA_RETRY == "Very little data: trying again with settings for thin data."


def test_notices_use_the_words_of_the_window():
    from tesseract_win.gui import texts
    assert messages.ON_BATTERY == texts.ON_BATTERY
    assert messages.battery_low(18) == texts.battery_low(18) == \
        "Battery at 18 %. Connect the charger now, or the run will be interrupted."
    assert messages.low_memory(2.9 * 1024 ** 3) == texts.low_memory("2.9 GB")
    assert messages.slept("2026-11-16T22:14:00", "2026-11-17T07:31:00") == texts.slept("22:14", "07:31")
    assert messages.THIN_DATA_RETRY == texts.THIN_DATA_RETRY
    assert messages.summary_not_updated(["run_summary.tsv"]) == (
        "run_summary.tsv could not be updated because it is open in another program. Close it and press "
        "'Write the summary again'.")
    assert "they are open" in messages.summary_not_updated(["run_summary.tsv", "run_summary.html"])


def test_the_line_at_the_end_of_a_batch():
    counts = {"done": 36, "done_notes": 2, "failed": 2}
    done = messages.batch_message(EndReason.COMPLETED, counts, seconds=9 * 3600 + 14 * 60,
                                  finished_at="2026-11-17T03:42:00")
    assert done == "Finished at 03:42. It took 9 h 14 min. 36 done, 2 done (check notes), 2 failed."
    more = messages.batch_message(EndReason.COMPLETED, {"done": 1, "skipped": 12, "left_out": 2}, seconds=75,
                                  finished_at="2026-11-17T03:42:00")
    assert more == "Finished at 03:42. It took 1 min 15 s. 1 done, 12 skipped (already assembled), 2 left out."
    stopped = messages.batch_message(EndReason.STOPPED, {"done": 10, "done_notes": 2, "stopped": 1, "not_started": 27})
    assert stopped == "Stopped. 12 finished, 1 stopped, 27 not started. Nothing that was finished has been lost."
    assert messages.batch_message(EndReason.ENVIRONMENT, counts, environment="Stopped: x.") == "Stopped: x."
    assert messages.batch_message(EndReason.COMPLETED, {}, seconds=3).endswith("It took 3 seconds. No sample was run.")


def test_what_the_bar_says_about_an_interrupted_run():
    windows = messages.interrupted_sentence("windows", "2026-11-17T03:12:44", 23, 40)
    assert windows == ("The last run was interrupted on 17 Nov at 03:12 because Windows shut down or restarted. "
                       "23 of 40 samples are finished. Nothing that was finished has been lost.")
    unknown = messages.interrupted_sentence("unknown", "2026-11-17T03:12:44", 23, 40)
    assert unknown.startswith("The last run did not finish: it ended unexpectedly on 17 Nov at about 03:12.")
    assert messages.interrupted_sentence("stopped", "2026-11-17T03:12:44", 0, 1).startswith(
        "The last run was stopped on 17 Nov at 03:12. 0 of 1 sample is finished.")
    assert "unknown time" in messages.interrupted_sentence("unknown", "", 1, 2)


def test_messages_needs_no_tk_and_no_window():
    code = ("import sys; import tesseract_win.messages; "
            "sys.exit(1 if any(m == 'tkinter' or m.startswith('tkinter.') for m in sys.modules) else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=REPO).returncode == 0


# ============================================================================ added by the review

def test_a_file_name_with_an_apostrophe_is_named_whole():
    from types import SimpleNamespace
    line = SimpleNamespace(verdict="FAIL", name="gzip",
                           message="'/data/O'Brien's run/KP's_R2.fastq.gz' is truncated or corrupt (gzip -t failed)")
    assert "(KP's_R2.fastq.gz)" in messages.explain_read_check(line).note
    line = SimpleNamespace(verdict="FAIL", name="file", message="cannot read '/data/O'Brien/KP_R1.fastq.gz'")
    assert "(/data/O'Brien/KP_R1.fastq.gz)" in messages.explain_read_check(line).note
    line = SimpleNamespace(verdict="FAIL", name="file", message="'it's_R1.fastq' is empty")
    assert "(it's_R1.fastq)" in messages.explain_read_check(line).note


def test_names_that_cannot_run_are_left_out_with_what_to_do():
    twice = messages.left_out_name_twice("KP1", "kp1")
    assert twice.startswith("Another sample in this run is called kp1.") and "KP1" in twice
    assert twice.endswith("Rename one of them on the Samples page.")
    bad = messages.left_out_bad_name("a/b")
    assert '"a/b"' in bad and bad.endswith("Rename this sample on the Samples page.")


def test_the_other_fourth_step_of_the_assembler_is_not_called_repeat_resolution():
    from types import SimpleNamespace
    usual = SimpleNamespace(step=4, total_steps=7, detail="paired-end repeat resolution", k=None)
    other = SimpleNamespace(step=4, total_steps=7, detail="collecting contigs", k=None)
    assert messages.step_text(usual) == "Step 4 of 7: resolving repeats with the read pairs"
    assert messages.step_text(other) == "Step 4 of 7: collecting the contigs"
