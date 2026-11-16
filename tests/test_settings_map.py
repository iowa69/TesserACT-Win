# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""settings_map: ticks -> flags, checks, words; and the proof against the real scepter.

The tests marked ``engines`` run the Linux scepter of ``build/engines/linux`` on a small
generated read pair (a 30 kb genome), two threads at most, one run at a time.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import subprocess
import sys

import pytest

from tesseract_win import appdirs, settings_map as sm
from tesseract_win.model import ASSEMBLY_MODES, AssemblySettings, TrimSettings, default_threads

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(REPO, "tests", "tools", "gen.py")

IO = dict(read1="/data/in_R1.fastq.gz", read2="/data/in_R2.fastq.gz",
          out1="KP_1_trimmed_R1.fastq.gz", out2="KP_1_trimmed_R2.fastq.gz",
          json_report="trimming_report.json", html_report="trimming_report.html",
          title="KP_1", threads=8)

#: Everything scepter_command puts in front of the trimming flags.
HEAD = ["scepter", "-i", "/data/in_R1.fastq.gz", "-I", "/data/in_R2.fastq.gz",
        "-o", "KP_1_trimmed_R1.fastq.gz", "-O", "KP_1_trimmed_R2.fastq.gz",
        "-j", "trimming_report.json", "-h", "trimming_report.html", "-R", "KP_1",
        "-w", "8", "--no-color", "--no-summary", "--verbose"]

#: scepter-port.md B1, "fully explicit form".
EXPLICIT = ["--cut_tail", "--cut_tail_mean_quality", "10",
            "--qualified_quality_phred", "15", "--unqualified_percent_limit", "40",
            "--n_base_limit", "5", "--average_qual", "15", "--length_required", "50"]


def flags(trim: TrimSettings, **over) -> list:
    """The trimming flags only: the command without the fixed head."""
    cmd = sm.scepter_command("scepter", trim, **{**IO, **over})
    assert cmd[:len(HEAD)] == HEAD or over
    return cmd[len(HEAD):]


def change(**kw) -> TrimSettings:
    return dataclasses.replace(TrimSettings(), **kw)


# --------------------------------------------------------------------------- scepter command

def test_standard_command_is_the_fully_explicit_form():
    assert sm.scepter_command("scepter", TrimSettings(), **IO) == HEAD + EXPLICIT


def test_always_quiet_colourless_verbose_and_named_reports():
    cmd = sm.scepter_command("C:\\x y\\scepter.exe", TrimSettings(), **IO)
    assert cmd[0] == "C:\\x y\\scepter.exe"
    for flag in ("--no-color", "--no-summary", "--verbose"):
        assert cmd.count(flag) == 1
    # -h always has a value: a bare -h is scepter's help.
    assert cmd[cmd.index("-h") + 1] == "trimming_report.html"
    assert cmd[cmd.index("-j") + 1] == "trimming_report.json"
    assert cmd[cmd.index("-R") + 1] == "KP_1"


@pytest.mark.parametrize("threads, expected", [(1, "1"), (4, "4"), (16, "16"), (24, "16"),
                                               (128, "16"), (0, "1")])
def test_threads_are_capped_at_sixteen(threads, expected):
    cmd = sm.scepter_command("scepter", TrimSettings(), **{**IO, "threads": threads})
    assert cmd[cmd.index("-w") + 1] == expected
    assert sm.scepter_threads(threads) == int(expected)


def test_untick_remove_adapters_passes_A_and_no_custom_adapters():
    got = flags(change(remove_adapters=False))
    assert got == ["-A"] + EXPLICIT
    got = flags(change(remove_adapters=False, own_adapters=True,
                       adapter_r1="AGATCGGAAGAGCACACGTC", adapter_r2="AGATCGGAAGAGCGTCGTGT"))
    assert got == ["-A"] + EXPLICIT
    assert "-a" not in got and "--adapter_sequence_r2" not in got


def test_untick_trim_low_quality_ends_drops_the_cut():
    got = flags(change(trim_low_quality_ends=False))
    assert "--cut_tail" not in got and "--cut_tail_mean_quality" not in got
    assert got == EXPLICIT[3:]


def test_end_quality_number():
    got = flags(change(end_quality=20))
    assert got[:3] == ["--cut_tail", "--cut_tail_mean_quality", "20"]


def test_untick_remove_poor_reads_passes_Q_only():
    got = flags(change(remove_poor_reads=False))
    assert got == ["--cut_tail", "--cut_tail_mean_quality", "10", "-Q", "--length_required", "50"]
    for gone in ("--average_qual", "--qualified_quality_phred", "--unqualified_percent_limit",
                 "--n_base_limit"):
        assert gone not in got


def test_quality_filter_numbers():
    got = flags(change(min_mean_quality=20, base_quality=18, max_low_percent=30))
    assert got[got.index("--average_qual") + 1] == "20"
    assert got[got.index("--qualified_quality_phred") + 1] == "18"
    assert got[got.index("--unqualified_percent_limit") + 1] == "30"
    assert got[got.index("--n_base_limit") + 1] == "5"


def test_untick_remove_short_reads_passes_L():
    got = flags(change(remove_short_reads=False))
    assert got[-1] == "-L" and "--length_required" not in got
    assert flags(change(min_length=75))[-2:] == ["--length_required", "75"]


def test_untick_polyg_passes_G_and_ticked_passes_nothing():
    assert "-G" in flags(change(trim_polyg=False))
    standard = flags(TrimSettings())
    assert "-G" not in standard and "-g" not in standard


def test_keep_trimmed_reads_is_not_a_scepter_matter():
    assert flags(change(keep_trimmed_reads=False)) == EXPLICIT


def test_trim_starts_uses_the_quality_of_the_ends():
    got = flags(change(trim_low_quality_starts=True))
    assert got == ["--cut_front", "--cut_front_mean_quality", "10"] + EXPLICIT
    got = flags(change(trim_low_quality_starts=True, end_quality=17))
    assert got[:6] == ["--cut_front", "--cut_front_mean_quality", "17",
                       "--cut_tail", "--cut_tail_mean_quality", "17"]
    # Starts without ends still gets its own quality number, never scepter's Q20.
    got = flags(change(trim_low_quality_starts=True, trim_low_quality_ends=False, end_quality=12))
    assert got[:3] == ["--cut_front", "--cut_front_mean_quality", "12"]
    assert "--cut_tail" not in got


def test_polyx_and_fixed_cut():
    assert "-x" in flags(change(trim_polyx=True))
    assert "-x" not in flags(TrimSettings())
    got = flags(change(fixed_trim=True, trim_front=5, trim_tail=3))
    assert got[:4] == ["-f", "5", "-t", "3"]
    assert "-F" not in got and "-T" not in got          # read 2 follows read 1
    # The numbers alone do nothing while the box is unticked.
    assert flags(change(trim_front=5, trim_tail=3)) == EXPLICIT


def test_own_adapters():
    got = flags(change(own_adapters=True, adapter_r1=" agatcggaagagcacacgtc ",
                       adapter_r2="AGATCGGAAGAGCGTCGTGT"))
    assert got[:4] == ["-a", "AGATCGGAAGAGCACACGTC", "--adapter_sequence_r2", "AGATCGGAAGAGCGTCGTGT"]
    got = flags(change(own_adapters=True, adapter_r1="AGATCGGAAGAGCACACGTC"))
    assert got[:2] == ["-a", "AGATCGGAAGAGCACACGTC"] and "--adapter_sequence_r2" not in got
    # Typed sequences without the tick are not used.
    assert flags(change(adapter_r1="AGATCGGAAGAGCACACGTC")) == EXPLICIT


def all_tick_combinations():
    names = ["remove_adapters", "trim_low_quality_ends", "remove_poor_reads", "remove_short_reads",
             "trim_polyg", "keep_trimmed_reads", "trim_low_quality_starts", "trim_polyx",
             "fixed_trim", "own_adapters"]
    for mask in range(1 << len(names)):
        yield dataclasses.replace(
            TrimSettings(trim_front=3, trim_tail=2, adapter_r1="ACGTACGTACGT", adapter_r2="TTGCATTGCATT"),
            **{name: bool(mask >> bit & 1) for bit, name in enumerate(names)})


def test_never_preset_outdir_dedup_or_correction():
    forbidden = {"--preset", "--out-dir", "--out_dir", "-D", "--dedup", "-c", "--correction",
                 "-y", "-g", "-2", "--no-kmer", "--no-deep", "--quiet"}
    count = 0
    for trim in all_tick_combinations():
        cmd = sm.scepter_command("scepter", trim, **IO)
        assert not forbidden.intersection(cmd), cmd
        assert all(isinstance(part, str) for part in cmd)
        assert not any(part.startswith("--preset") for part in cmd)
        count += 1
    assert count == 1024


def test_trimmed_names_and_report_names():
    assert sm.trimmed_names("KP_1") == ("KP_1_trimmed_R1.fastq.gz", "KP_1_trimmed_R2.fastq.gz")
    files = sm.trimming_files("KP_1")
    assert files == ("KP_1_trimmed_R1.fastq.gz", "KP_1_trimmed_R2.fastq.gz",
                     "trimming_report.json", "trimming_report.html", "trimming.log")
    assert files.json_report == sm.TRIMMING_REPORT_JSON == "trimming_report.json"
    assert files.html_report == sm.TRIMMING_REPORT_HTML == "trimming_report.html"
    assert files.log == sm.TRIMMING_LOG == "trimming.log"


# --------------------------------------------------------------------------- validation

def test_standard_settings_are_valid():
    assert sm.validate_trim(TrimSettings()) == []
    assert sm.validate_assembly(AssemblySettings()) == []


@pytest.mark.parametrize("field, low, high", [
    ("end_quality", 1, 36), ("min_mean_quality", 0, 60), ("base_quality", 0, 60),
    ("max_low_percent", 0, 100), ("min_length", 0, 10000)])
def test_trim_number_ranges(field, low, high):
    assert sm.TRIM_RANGES[field] == (low, high) == sm.number_range(field)
    assert sm.validate_trim(change(**{field: low})) == []
    assert sm.validate_trim(change(**{field: high})) == []
    for bad in (low - 1, high + 1, 2.5, "7", None, True):
        problems = sm.validate_trim(change(**{field: bad}))
        assert [name for name, _ in problems] == [field], (field, bad)
        message = problems[0][1]
        assert "from %d to %d" % (low, high) in message and message.endswith(".")


@pytest.mark.parametrize("field", ["trim_front", "trim_tail"])
def test_fixed_cut_range(field):
    assert sm.TRIM_RANGES[field] == (0, 100)
    assert sm.validate_trim(change(fixed_trim=True, **{field: 100})) == []
    assert [n for n, _ in sm.validate_trim(change(fixed_trim=True, **{field: 101}))] == [field]
    assert [n for n, _ in sm.validate_trim(change(fixed_trim=True, **{field: -1}))] == [field]
    # Behind an unticked box the number is not used and cannot stop a run.
    assert sm.validate_trim(change(**{field: 101})) == []


def test_numbers_behind_unticked_boxes_are_not_checked():
    assert sm.validate_trim(change(trim_low_quality_ends=False, end_quality=99)) == []
    assert sm.validate_trim(change(trim_low_quality_ends=False, trim_low_quality_starts=True,
                                   end_quality=99)) != []
    assert sm.validate_trim(change(remove_poor_reads=False, min_mean_quality=99, base_quality=99,
                                   max_low_percent=999)) == []
    assert sm.validate_trim(change(remove_short_reads=False, min_length=-5)) == []


def test_own_adapters_need_read_1():
    problems = sm.validate_trim(change(own_adapters=True))
    assert [name for name, _ in problems] == ["adapter_r1"]
    problems = sm.validate_trim(change(own_adapters=True, adapter_r1="   ",
                                       adapter_r2="AGATCGGAAGAGC"))
    assert [name for name, _ in problems] == ["adapter_r1"]


def test_adapter_letters_and_length():
    ok = change(own_adapters=True, adapter_r1="agatcGGAAGAGC", adapter_r2="AGATCGGAAG")
    assert sm.validate_trim(ok) == []
    assert sm.validate_trim(change(own_adapters=True, adapter_r1="AGATCGGAAG")) == []     # 10 letters
    for bad in ("AGATCGGAA", "hello", "AGATCGGAAGNNNN", "AGATC GGAAGAGC", "AGATCGGAAGAGC-", "A" * 201):
        problems = sm.validate_trim(change(own_adapters=True, adapter_r1=bad))
        assert [name for name, _ in problems] == ["adapter_r1"], bad
    problems = sm.validate_trim(change(own_adapters=True, adapter_r1="AGATCGGAAGAGC", adapter_r2="ACGU"))
    assert [name for name, _ in problems] == ["adapter_r2"]
    letters = sm.validate_trim(change(own_adapters=True, adapter_r1="AGATCGGAAGXXXX"))[0][1]
    assert "A, C, G and T" in letters
    short = sm.validate_trim(change(own_adapters=True, adapter_r1="ACGT"))[0][1]
    assert "10" in short
    # Not ticked, or adapter removal off altogether: whatever is typed is not used.
    assert sm.validate_trim(change(adapter_r1="hello")) == []
    assert sm.validate_trim(change(own_adapters=True, remove_adapters=False, adapter_r1="hello")) == []


def test_assembly_mode():
    for mode in ASSEMBLY_MODES:
        assert sm.validate_assembly(AssemblySettings(mode=mode)) == []
    for bad in ("Careful", "", "meta", None):
        assert [n for n, _ in sm.validate_assembly(AssemblySettings(mode=bad))] == ["mode"]


def test_genome_size_range():
    assert sm.ASSEMBLY_RANGES["genome_size_mb"] == (0.1, 20.0)
    for good in (0.1, 2.8, 5, 20, 20.0):
        assert sm.validate_assembly(AssemblySettings(know_genome_size=True, genome_size_mb=good)) == []
    for bad in (0, 0.05, 20.1, -1, "5", None, float("nan")):
        problems = sm.validate_assembly(AssemblySettings(know_genome_size=True, genome_size_mb=bad))
        assert [n for n, _ in problems] == ["genome_size_mb"], bad
        assert "from 0.1 to 20" in problems[0][1]
    assert sm.validate_assembly(AssemblySettings(know_genome_size=False, genome_size_mb=99)) == []


def test_min_contig_range():
    assert sm.validate_assembly(AssemblySettings(drop_short_contigs=True, min_contig=1)) == []
    assert sm.validate_assembly(AssemblySettings(drop_short_contigs=True, min_contig=100000)) == []
    for bad in (0, 100001, 1.5):
        problems = sm.validate_assembly(AssemblySettings(drop_short_contigs=True, min_contig=bad))
        assert [n for n, _ in problems] == ["min_contig"]
    assert sm.validate_assembly(AssemblySettings(min_contig=0)) == []


def test_threads_from_one_to_the_logical_processors():
    cpus = default_threads()
    assert sm.number_range("threads") == (1, cpus)
    assert sm.validate_assembly(AssemblySettings(threads=1)) == []
    assert sm.validate_assembly(AssemblySettings(threads=cpus)) == []
    for bad in (0, -2, cpus + 1, 2.0, "4"):
        assert [n for n, _ in sm.validate_assembly(AssemblySettings(threads=bad))] == ["threads"], bad
    assert sm.validate_assembly(AssemblySettings(threads=4), logical_processors=4) == []
    problems = sm.validate_assembly(AssemblySettings(threads=5), logical_processors=4)
    assert problems == [("threads", "Processor threads: use a whole number from 1 to 4 "
                                    "(this PC has 4).")]
    assert sm.validate_assembly(AssemblySettings(threads=2), logical_processors=1)[0][0] == "threads"


def test_every_problem_is_a_field_and_a_sentence():
    trim = TrimSettings(end_quality=0, min_mean_quality=61, base_quality=-1, max_low_percent=101,
                        min_length=10001, fixed_trim=True, trim_front=-1, trim_tail=101,
                        own_adapters=True, adapter_r1="x", adapter_r2="y")
    problems = sm.validate_trim(trim)
    assert [name for name, _ in problems] == [
        "end_quality", "min_mean_quality", "base_quality", "max_low_percent", "min_length",
        "trim_front", "trim_tail", "adapter_r1", "adapter_r2"]
    names = {f.name for f in dataclasses.fields(TrimSettings)}
    for name, message in problems:
        assert name in names and message.startswith(sm.FIELD_LABELS[name]) and message.endswith(".")
    asm = AssemblySettings(know_genome_size=True, genome_size_mb=50, drop_short_contigs=True,
                           min_contig=0, mode="x", threads=0)
    problems = sm.validate_assembly(asm)
    assert [name for name, _ in problems] == ["genome_size_mb", "min_contig", "mode", "threads"]


# --------------------------------------------------------------------------- clamp helpers

def test_clamp_number():
    assert sm.clamp_number("end_quality", 0) == 1
    assert sm.clamp_number("end_quality", 99) == 36
    assert sm.clamp_number("end_quality", 12) == 12
    assert sm.clamp_number("max_low_percent", 40.4) == 40
    assert isinstance(sm.clamp_number("max_low_percent", 40.4), int)
    assert sm.clamp_number("genome_size_mb", 0.01) == 0.1
    assert sm.clamp_number("genome_size_mb", 250) == 20.0
    assert sm.clamp_number("genome_size_mb", 2.8) == 2.8
    assert sm.clamp_number("min_contig", 0) == 1
    assert sm.clamp_number("threads", 0) == 1
    assert sm.clamp_number("threads", 10 ** 6) == default_threads()
    with pytest.raises(KeyError):
        sm.number_range("no_such_field")


def test_typed_number_goes_back_to_the_last_good_value():
    assert sm.typed_number("max_low_percent", "35", 40) == (35, "")
    assert sm.typed_number("max_low_percent", " 0 ", 40) == (0, "")
    assert sm.typed_number("max_low_percent", "101", 40) == (
        40, "Use a whole number from 0 to 100. Set back to 40.")
    for typed in ("", "abc", "3.5", "-1", "1e2", "12 3"):
        value, message = sm.typed_number("end_quality", typed, 10)
        assert value == 10 and message == "Use a whole number from 1 to 36. Set back to 10.", typed
    top = default_threads()
    assert sm.typed_number("threads", str(top), 1) == (top, "")
    assert sm.typed_number("threads", str(top + 1), top)[0] == top


def test_genome_size_accepts_comma_and_dot():
    assert sm.parse_genome_size("5.5") == 5.5
    assert sm.parse_genome_size("5,5") == 5.5
    assert sm.parse_genome_size(" 2,8 ") == 2.8
    assert sm.parse_genome_size("6") == 6.0
    for bad in ("", "abc", "5.5.5", "-3", "5 Mb"):
        assert sm.parse_genome_size(bad) is None
    assert sm.typed_number("genome_size_mb", "5,5", 5.0) == (5.5, "")
    assert sm.typed_number("genome_size_mb", "25", 5.0) == (
        5.0, "Use a number from 0.1 to 20. Set back to 5.0.")
    assert sm.typed_number("genome_size_mb", "big", 2.8)[0] == 2.8


# --------------------------------------------------------------------------- assembler command

ASM_IO = dict(read1="trimming/KP_1_trimmed_R1.fastq.gz", read2="trimming/KP_1_trimmed_R2.fastq.gz",
              out_dir_name="assembly")


def test_standard_assembly_adds_nothing():
    cmd = sm.tesseract_command("tesseract-asm", AssemblySettings(threads=8), **ASM_IO)
    assert cmd == ["tesseract-asm", "-1", "trimming/KP_1_trimmed_R1.fastq.gz",
                   "-2", "trimming/KP_1_trimmed_R2.fastq.gz", "-o", "assembly", "-t", "8"]
    cmd = sm.tesseract_command("tesseract-asm", AssemblySettings(), **ASM_IO)
    assert cmd[-2:] == ["-t", str(default_threads())] and len(cmd) == 9


def test_assembly_options():
    def extra(**kw):
        return sm.tesseract_command("asm", AssemblySettings(threads=4, **kw), **ASM_IO)[9:]

    assert extra(mode="careful") == ["--mode", "careful"]
    assert extra(mode="fast") == ["--mode", "fast"]
    assert extra(mode="aggressive") == ["--mode", "aggressive"]
    assert extra(mode="standard") == []
    assert extra(drop_short_contigs=True) == ["--min-contig", "500"]
    assert extra(drop_short_contigs=True, min_contig=1000) == ["--min-contig", "1000"]
    assert extra(min_contig=1000) == []                     # the number alone, box unticked
    assert extra(scaffold=False) == ["--no-scaffold"]
    assert extra(polish=False) == ["--no-polish"]
    assert extra(mode="careful", drop_short_contigs=True, scaffold=False, polish=False) == [
        "--mode", "careful", "--min-contig", "500", "--no-scaffold", "--no-polish"]


def test_genome_size_and_force_are_not_engine_options():
    asm = AssemblySettings(threads=4, know_genome_size=True, genome_size_mb=2.8, force=True)
    cmd = sm.tesseract_command("asm", asm, **ASM_IO)
    assert cmd == ["asm", "-1", ASM_IO["read1"], "-2", ASM_IO["read2"], "-o", "assembly", "-t", "4"]
    assert sm.expected_genome_size_mb(asm) == 2.8
    assert sm.expected_genome_size_mb(AssemblySettings()) is None
    assert sm.expected_genome_size_mb(AssemblySettings(genome_size_mb=2.8)) is None


def test_thin_data_retry_puts_k_right_after_the_threads():
    # The bash script: "$ASM" -1 R1 -2 R2 -o OUT -t TH -k 21,33,55 <the user's options>
    assert sm.THIN_DATA_KMERS == "21,33,55"
    asm = AssemblySettings(threads=4, mode="careful", polish=False)
    cmd = sm.tesseract_command("asm", asm, kmers=sm.THIN_DATA_KMERS, **ASM_IO)
    assert cmd[7:] == ["-t", "4", "-k", "21,33,55", "--mode", "careful", "--no-polish"]
    assert "-k" not in sm.tesseract_command("asm", asm, **ASM_IO)
    assert "-k" not in sm.tesseract_command("asm", asm, kmers="", **ASM_IO)


# --------------------------------------------------------------------------- environment

NINE = {
    "TESSERACT_P2_LIBGUARD": "1", "TESSERACT_P2_CIRC": "verify", "TESSERACT_P2_SPIKEIN": "1",
    "TESSERACT_P2_ENDS": "1", "TESSERACT_P2_TIPS_LOWERCASE": "1", "TESSERACT_P2_SELF_QA": "1",
    "TESSERACT_P2_PROVENANCE": "1", "TESSERACT_DEEP_NORM": "30", "TESSERACT_DEEP_NORM_MATE": "both",
}


def test_profile_is_the_nine_variables_of_the_wrapper():
    assert dict(sm.PROFILE_ENVIRONMENT) == NINE and len(sm.PROFILE_ENVIRONMENT) == 9
    # The same nine, with the same values, in the pinned bash script.
    script = os.path.join(REPO, "engines", "tesseract", "upstream", "tesseract")
    if os.path.isfile(script):
        with open(script, encoding="utf-8") as handle:
            text = handle.read()
        start = text.index("PROFILE=(")
        block = text[start + len("PROFILE=("):text.index(")", start)]
        assert dict(item.split("=", 1) for item in block.split()) == NINE


def test_environment_drops_stray_tesseract_variables():
    base = {"PATH": "/usr/bin", "TESSERACT_FOO": "1", "TESSERACT_P2_CIRC": "off",
            "TESSERACT_WIN_ENGINES": "/x", "tesseract_lower": "1", "TESSERACT": "kept",
            "NO_COLOR": "", "SystemRoot": "C:\\Windows"}
    snapshot = dict(base)
    env = sm.engine_environment(base, "/opt/engines/om2detect.sketch")
    assert base == snapshot                                 # the caller's mapping is untouched
    assert "TESSERACT_FOO" not in env and "TESSERACT_WIN_ENGINES" not in env
    assert "tesseract_lower" not in env
    assert env["PATH"] == "/usr/bin" and env["SystemRoot"] == "C:\\Windows"
    assert env["TESSERACT"] == "kept"                       # not a TESSERACT_* variable
    assert env["NO_COLOR"] == "1"
    assert env["TESSERACT_P2_CIRC"] == "verify"             # the profile, not the user's value
    engine_vars = {k: v for k, v in env.items() if k.startswith("TESSERACT_")}
    assert engine_vars == {**NINE, "TESSERACT_P2_DETECT_REPORT": "1",
                           "TESSERACT_P2_DETECT_SKETCH": "/opt/engines/om2detect.sketch"}


def test_environment_without_a_sketch_has_no_detection_variables():
    env = sm.engine_environment({"HOME": "/home/x"}, "")
    assert {k: v for k, v in env.items() if k.startswith("TESSERACT_")} == NINE
    assert env == {"HOME": "/home/x", "NO_COLOR": "1", **NINE}
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())


def test_environment_accepts_os_environ(monkeypatch):
    monkeypatch.setenv("TESSERACT_FOO", "stray")
    env = sm.engine_environment(os.environ, "")
    assert "TESSERACT_FOO" not in env and os.environ["TESSERACT_FOO"] == "stray"
    assert env.get("PATH") == os.environ.get("PATH")


# --------------------------------------------------------------------------- words

def test_describe():
    eight = AssemblySettings(threads=8)
    assert sm.describe(TrimSettings(), eight, True) == "standard trimming, standard assembly, 8 threads"
    assert sm.describe(change(end_quality=20), AssemblySettings(threads=8, mode="careful"), True) \
        == "trimming changed, mode Careful, 8 threads"
    assert sm.describe(change(end_quality=20), eight, False) == "no trimming, standard assembly, 8 threads"
    assert sm.describe(TrimSettings(), AssemblySettings(threads=1), True).endswith(", 1 thread")
    # Keeping or deleting the trimmed reads, and unused numbers, do not change the trimming.
    assert sm.describe(change(keep_trimmed_reads=False, trim_front=9), eight, True).startswith(
        "standard trimming")
    two = AssemblySettings(threads=8, mode="fast", polish=False)
    assert sm.describe(TrimSettings(), two, True) == "standard trimming, mode Fast, no polishing, 8 threads"
    many = AssemblySettings(threads=8, mode="fast", polish=False, scaffold=False)
    assert sm.describe(TrimSettings(), many, True) == "standard trimming, assembly changed, 8 threads"


def test_differences_one_phrase_per_changed_setting():
    assert sm.differences(TrimSettings(), AssemblySettings()) == []
    one_each = [
        (change(remove_adapters=False), "adapters not removed"),
        (change(trim_low_quality_ends=False), "low-quality ends not trimmed"),
        (change(remove_poor_reads=False), "poor-quality reads kept"),
        (change(remove_short_reads=False), "very short reads kept"),
        (change(trim_polyg=False), "poly-G tails not trimmed"),
        (change(keep_trimmed_reads=False), "trimmed reads not kept"),
        (change(end_quality=20), "ends trimmed below quality 20"),
        (change(min_mean_quality=20), "reads with an average quality below 20 removed"),
        (change(base_quality=20), "reads with more than 40 % of bases below quality 20 removed"),
        (change(max_low_percent=30), "reads with more than 30 % of bases below quality 15 removed"),
        (change(min_length=30), "reads shorter than 30 bases removed"),
        (change(trim_low_quality_starts=True), "low-quality starts trimmed too"),
        (change(trim_polyx=True), "repeated-base tails trimmed"),
        (change(fixed_trim=True, trim_front=5, trim_tail=3),
         "5 bases cut from the start and 3 from the end of every read"),
        (change(own_adapters=True, adapter_r1="AGATCGGAAGAGC"), "own adapter sequences used"),
    ]
    for trim, phrase in one_each:
        assert sm.differences(trim, AssemblySettings()) == [phrase]
    asm_each = [
        (dict(know_genome_size=True, genome_size_mb=2.8), "genome size about 2.8 Mb"),
        (dict(drop_short_contigs=True), "contigs shorter than 500 bases left out"),
        (dict(mode="careful"), "mode Careful"),
        (dict(scaffold=False), "no scaffolding"),
        (dict(polish=False), "no polishing"),
        (dict(force=True), "assembled even if the read check fails"),
    ]
    for kw, phrase in asm_each:
        assert sm.differences(TrimSettings(), AssemblySettings(**kw)) == [phrase]


def test_differences_ignore_what_is_not_in_use():
    unused = change(trim_low_quality_ends=False, end_quality=30, remove_poor_reads=False,
                    min_mean_quality=30, remove_short_reads=False, min_length=10,
                    trim_front=9, adapter_r1="ACGTACGTACGT")
    assert sm.differences(unused, AssemblySettings(min_contig=999, genome_size_mb=3.0)) == [
        "low-quality ends not trimmed", "poor-quality reads kept", "very short reads kept"]
    assert sm.differences(change(fixed_trim=True), AssemblySettings()) == []     # 0 and 0 cut nothing


def test_differences_for_an_untrimmed_sample_and_threads():
    careful = AssemblySettings(mode="careful")
    assert sm.differences(change(end_quality=20), careful, trimmed=False) == ["not trimmed", "mode Careful"]
    fewer = AssemblySettings(threads=default_threads() + 1)
    assert sm.differences(TrimSettings(), fewer) == []
    assert sm.differences(TrimSettings(), fewer, include_threads=True) == [
        "%d threads" % (default_threads() + 1)]


def test_fingerprint_is_stable_and_short():
    first = sm.settings_fingerprint(TrimSettings(), AssemblySettings(), True)
    assert first == sm.settings_fingerprint(TrimSettings(), AssemblySettings(), True)
    assert first.startswith("s1-") and len(first) == 19
    # A fixed value: the fingerprint must not change from one release or Python to the next.
    assert first == "s1-30f89624050d4096"


def test_fingerprint_ignores_what_cannot_change_a_result():
    base = sm.settings_fingerprint(TrimSettings(), AssemblySettings(), True)
    same = [
        (TrimSettings(), AssemblySettings(threads=1)),
        (TrimSettings(), AssemblySettings(force=True)),
        (change(keep_trimmed_reads=False), AssemblySettings()),
        (change(trim_front=7, trim_tail=7), AssemblySettings()),             # box unticked
        (change(fixed_trim=True), AssemblySettings()),                       # 0 and 0
        (change(adapter_r1="ACGTACGTACGT"), AssemblySettings()),             # box unticked
        (TrimSettings(), AssemblySettings(min_contig=900, genome_size_mb=3.3)),
    ]
    for trim, asm in same:
        assert sm.settings_fingerprint(trim, asm, True) == base
    off = change(trim_low_quality_ends=False)
    assert sm.settings_fingerprint(off, AssemblySettings(), True) == sm.settings_fingerprint(
        dataclasses.replace(off, end_quality=33), AssemblySettings(), True)
    lower = change(own_adapters=True, adapter_r1="acgtacgtacgt")
    upper = change(own_adapters=True, adapter_r1="ACGTACGTACGT")
    assert sm.settings_fingerprint(lower, AssemblySettings(), True) == sm.settings_fingerprint(
        upper, AssemblySettings(), True)


def test_fingerprint_changes_with_every_setting_that_matters():
    seen = {sm.settings_fingerprint(TrimSettings(), AssemblySettings(), True)}
    trims = [change(remove_adapters=False), change(trim_low_quality_ends=False),
             change(remove_poor_reads=False), change(remove_short_reads=False),
             change(trim_polyg=False), change(end_quality=11), change(min_mean_quality=16),
             change(base_quality=16), change(max_low_percent=41), change(min_length=51),
             change(trim_low_quality_starts=True), change(trim_polyx=True),
             change(fixed_trim=True, trim_front=1), change(fixed_trim=True, trim_tail=1),
             change(own_adapters=True, adapter_r1="ACGTACGTACGT"),
             change(own_adapters=True, adapter_r1="ACGTACGTACGT", adapter_r2="TTGCATTGCATT")]
    for trim in trims:
        seen.add(sm.settings_fingerprint(trim, AssemblySettings(), True))
    asms = [AssemblySettings(know_genome_size=True), AssemblySettings(know_genome_size=True, genome_size_mb=2.8),
            AssemblySettings(drop_short_contigs=True), AssemblySettings(drop_short_contigs=True, min_contig=501),
            AssemblySettings(mode="fast"), AssemblySettings(mode="careful"),
            AssemblySettings(mode="aggressive"), AssemblySettings(scaffold=False),
            AssemblySettings(polish=False)]
    for asm in asms:
        seen.add(sm.settings_fingerprint(TrimSettings(), asm, True))
    seen.add(sm.settings_fingerprint(TrimSettings(), AssemblySettings(), False))
    assert len(seen) == 1 + len(trims) + len(asms) + 1


def test_fingerprint_of_an_untrimmed_sample_ignores_the_trimming_page():
    plain = sm.settings_fingerprint(TrimSettings(), AssemblySettings(), False)
    assert plain == sm.settings_fingerprint(change(end_quality=30, trim_polyx=True), AssemblySettings(), False)
    assert plain != sm.settings_fingerprint(TrimSettings(), AssemblySettings(), True)


def test_the_words_never_raise_on_settings_read_back_damaged():
    trim = TrimSettings(end_quality="x", min_mean_quality=None, base_quality=[], max_low_percent="40",
                        min_length=None, fixed_trim=True, trim_front="a", trim_tail=None,
                        own_adapters=True, adapter_r1=None, adapter_r2=7)
    asm = AssemblySettings(know_genome_size=True, genome_size_mb="big", drop_short_contigs=True,
                           min_contig=None, mode=None, threads="many")
    assert isinstance(sm.describe(trim, asm, True), str)
    assert all(isinstance(phrase, str) for phrase in sm.differences(trim, asm, include_threads=True))
    assert sm.settings_fingerprint(trim, asm, True).startswith("s1-")
    # ...and the checks say what is wrong instead of raising.
    assert {name for name, _ in sm.validate_trim(trim)} == {
        "end_quality", "min_mean_quality", "base_quality", "max_low_percent", "min_length",
        "trim_front", "trim_tail", "adapter_r1", "adapter_r2"}
    assert {name for name, _ in sm.validate_assembly(asm)} == {
        "genome_size_mb", "min_contig", "mode", "threads"}


# --------------------------------------------------------------------------- the real scepter

def _scepter() -> str:
    try:
        return appdirs.find_engines().scepter
    except appdirs.EngineMissing as exc:
        pytest.skip("scepter is not built here: %s" % exc)


@pytest.fixture(scope="module")
def reads(tmp_path_factory):
    """A small read pair: 30 kb genome, 20x, 2 x 100 bases (3 000 pairs), gzipped."""
    folder = tmp_path_factory.mktemp("reads")
    genome = str(folder / "genome.fa")
    prefix = str(folder / "tiny")
    for args in (["genome", genome, "7", "30000"],
                 ["reads", genome, prefix, "20", "100", "300", "30", "0.005", "3", "paired", "fastq", "gz"]):
        subprocess.run([sys.executable, GEN] + args, check=True, capture_output=True, timeout=120)
    return prefix + "_1.fq.gz", prefix + "_2.fq.gz"


def _run(cmd, cwd):
    done = subprocess.run(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    return done.returncode, done.stderr.decode("utf-8", "replace")


def _run_settings(scepter, trim, reads, folder, name="KP_1", threads=2):
    """Run scepter the way the program does: inside the trimming folder, bare output names."""
    folder.mkdir(parents=True, exist_ok=True)
    files = sm.trimming_files(name)
    cmd = sm.scepter_command(scepter, trim, read1=reads[0], read2=reads[1],
                             out1=files.read1, out2=files.read2, json_report=files.json_report,
                             html_report=files.html_report, title=name, threads=threads)
    code, err = _run(cmd, folder)
    return code, err, files


def _config(path):
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)
    return report["scepter"]["config"], report


def _records(path):
    with gzip.open(path, "rb") as handle:
        return handle.read()


#: scepter-port.md B3: "Against --preset assembly, scepter.config then differs only in
#: preset, the paths, threads, report_title, verbose and no_summary."
ALLOWED_DIFFERENCES = {"preset", "in1", "in2", "out1", "out2", "json", "html",
                       "threads", "report_title", "verbose", "no_summary"}


@pytest.mark.engines
def test_standard_ticks_resolve_to_the_assembly_preset(reads, tmp_path):
    scepter = _scepter()
    ours = tmp_path / "ours"
    code, err, files = _run_settings(scepter, TrimSettings(), reads, ours, threads=2)
    assert code == 0, err
    preset = tmp_path / "preset"
    preset.mkdir()
    code, err = _run([scepter, "-i", reads[0], "-I", reads[1], "-o", "p_1.fastq.gz", "-O", "p_2.fastq.gz",
                      "-j", "p.json", "-h", "p.html", "-w", "1", "--preset", "assembly"], preset)
    assert code == 0, err

    mine, _ = _config(ours / files.json_report)
    theirs, _ = _config(preset / "p.json")
    assert set(mine) == set(theirs)
    different = {key for key in mine if mine[key] != theirs[key]}
    assert different <= ALLOWED_DIFFERENCES, {k: (mine[k], theirs[k]) for k in different}
    # The differences that must be there, so that the comparison is known to be a real one.
    assert theirs["preset"] == "assembly" and mine["preset"] == ""
    assert (mine["threads"], theirs["threads"]) == (2, 1)
    assert mine["report_title"] == "KP_1" and mine["verbose"] is True and mine["no_summary"] is True
    # What the preset stands for, spelled out.
    assert (mine["cut_tail"], mine["cut_tail_mean_quality"], mine["cut_front"]) == (True, 10, False)
    assert (mine["average_qual"], mine["length_required"]) == (15, 50)
    assert (mine["correction_enabled"], mine["dedup"], mine["deep_kmer_spectrum"]) == (False, False, True)
    # And the reads that come out are the same reads.
    assert _records(ours / files.read1) == _records(preset / "p_1.fastq.gz")
    assert _records(ours / files.read2) == _records(preset / "p_2.fastq.gz")


def _expect(**expected):
    return expected


SWITCHES = [
    # More options: the five numbers
    ("end_quality", change(end_quality=20), _expect(cut_tail=True, cut_tail_mean_quality=20)),
    ("min_mean_quality", change(min_mean_quality=25), _expect(average_qual=25)),
    ("base_quality", change(base_quality=20), _expect(qualified_quality_phred=20)),
    ("max_low_percent", change(max_low_percent=20), _expect(unqualified_percent_limit=20)),
    ("min_length", change(min_length=80), _expect(length_required=80)),
    # More options: the switches
    ("starts", change(trim_low_quality_starts=True),
     _expect(cut_front=True, cut_front_mean_quality=10, cut_tail=True, cut_tail_mean_quality=10)),
    ("starts_other_quality", change(trim_low_quality_starts=True, end_quality=18),
     _expect(cut_front=True, cut_front_mean_quality=18, cut_tail_mean_quality=18)),
    ("polyx", change(trim_polyx=True), _expect(polyx_enabled=True)),
    ("fixed_cut", change(fixed_trim=True, trim_front=5, trim_tail=3), _expect(trim_front1=5, trim_tail1=3)),
    ("own_adapter_r1", change(own_adapters=True, adapter_r1="agatcggaagagcacacgtctgaactccagtca"),
     _expect(adapter_enabled=True, adapter_sequence="AGATCGGAAGAGCACACGTCTGAACTCCAGTCA")),
    ("own_adapters_both", change(own_adapters=True, adapter_r1="AGATCGGAAGAGCACACGTCTGAACTCCAGTCA",
                                 adapter_r2="AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT"),
     _expect(adapter_sequence="AGATCGGAAGAGCACACGTCTGAACTCCAGTCA",
             adapter_sequence_r2="AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT")),
    # The ticks on the page, unticked one at a time
    ("no_adapters", change(remove_adapters=False), _expect(adapter_enabled=False)),
    ("no_adapters_with_own", change(remove_adapters=False, own_adapters=True, adapter_r1="AGATCGGAAGAGCACACG"),
     _expect(adapter_enabled=False, adapter_sequence="auto")),
    ("no_end_trim", change(trim_low_quality_ends=False), _expect(cut_tail=False)),
    ("keep_poor_reads", change(remove_poor_reads=False), _expect(quality_filtering_enabled=False)),
    ("keep_short_reads", change(remove_short_reads=False), _expect(length_filtering_enabled=False)),
    ("no_polyg", change(trim_polyg=False), _expect(polyg_disabled=True, polyg_forced=False)),
    ("everything_at_once", TrimSettings(
        remove_adapters=True, trim_low_quality_ends=True, remove_poor_reads=True, remove_short_reads=True,
        trim_polyg=False, end_quality=12, min_mean_quality=18, base_quality=12, max_low_percent=50,
        min_length=60, trim_low_quality_starts=True, trim_polyx=True, fixed_trim=True, trim_front=2,
        trim_tail=1, own_adapters=True, adapter_r1="AGATCGGAAGAGCACACG", adapter_r2="AGATCGGAAGAGCGTCGT"),
     _expect(cut_front=True, cut_front_mean_quality=12, cut_tail_mean_quality=12, average_qual=18,
             qualified_quality_phred=12, unqualified_percent_limit=50, length_required=60,
             polyx_enabled=True, polyg_disabled=True, trim_front1=2, trim_tail1=1,
             adapter_sequence="AGATCGGAAGAGCACACG", adapter_sequence_r2="AGATCGGAAGAGCGTCGT")),
]


@pytest.mark.engines
@pytest.mark.parametrize("label, trim, expected", SWITCHES, ids=[s[0] for s in SWITCHES])
def test_each_switch_runs_and_writes_four_files(reads, tmp_path, label, trim, expected):
    scepter = _scepter()
    assert sm.validate_trim(trim) == []
    # A sample name with a space-free but awkward shape, and a folder with spaces and accents.
    folder = tmp_path / "données [run 1]" / "trimming"
    code, err, files = _run_settings(scepter, trim, reads, folder, name="KP-1.a")
    assert code == 0, err
    assert "error" not in err.lower(), err
    for name in (files.read1, files.read2, files.json_report, files.html_report):
        path = folder / name
        assert path.is_file() and path.stat().st_size > 0, name
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        [files.read1, files.read2, files.json_report, files.html_report])
    config, report = _config(folder / files.json_report)
    for key, value in expected.items():
        assert config[key] == value, (label, key, config[key])
    # Never anything the page does not offer.
    assert config["preset"] == "" and config["out_dir"] == ""
    assert config["dedup"] is False and config["correction_enabled"] is False
    assert config["report_title"] == "KP-1.a" and config["threads"] == 2
    assert config["n_base_limit"] == 5 and config["low_complexity_filter"] is False
    assert report["summary"]["after_filtering"]["total_reads"] > 0
    first = _records(folder / files.read1)
    second = _records(folder / files.read2)
    assert first.count(b"\n") == second.count(b"\n") > 0


@pytest.mark.engines
def test_scepter_ignores_the_engine_environment(reads, tmp_path):
    """The one environment built for both engines must not upset scepter."""
    scepter = _scepter()
    files = sm.trimming_files("KP_1")
    cmd = sm.scepter_command(scepter, TrimSettings(), read1=reads[0], read2=reads[1], out1=files.read1,
                             out2=files.read2, json_report=files.json_report,
                             html_report=files.html_report, title="KP_1", threads=2)
    env = sm.engine_environment({**os.environ, "TESSERACT_FOO": "stray"}, "/nowhere/om2detect.sketch")
    done = subprocess.run(cmd, cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr
    assert b"\x1b[" not in done.stderr              # no colour codes in what will become the log
    assert (tmp_path / files.json_report).is_file()


# --------------------------------------------------------------------------- the real assembler

def _engines():
    try:
        return appdirs.find_engines()
    except appdirs.EngineMissing as exc:
        pytest.skip("the engines are not built here: %s" % exc)


def _assemble(engines, assembly, reads, folder, base_env, kmers=None):
    folder.mkdir(parents=True, exist_ok=True)
    cmd = sm.tesseract_command(engines.tesseract_asm, assembly, read1=reads[0], read2=reads[1],
                               out_dir_name="assembly", kmers=kmers)
    env = sm.engine_environment(base_env, engines.detect_sketch)
    done = subprocess.run(cmd, cwd=str(folder), env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    log = done.stdout.decode("utf-8", "replace")
    report = {}
    if done.returncode == 0:
        with open(folder / "assembly" / "report.json", encoding="utf-8") as handle:
            report = json.load(handle)
    return done.returncode, log, report, cmd


@pytest.mark.engines
def test_assembler_takes_the_standard_command_and_the_environment(reads, tmp_path):
    engines = _engines()
    # What a user's shell might hold: an unknown variable (a warning line in the log) and a
    # bad value of a real one (the engine would stop with exit status 2).
    dirty = {**os.environ, "TESSERACT_FOO": "1", "TESSERACT_P2_CIRC": "banana"}
    code, log, report, cmd = _assemble(engines, AssemblySettings(threads=2), reads, tmp_path / "s p é", dirty)
    assert code == 0, log[-2000:]
    assert cmd[1:] == ["-1", reads[0], "-2", reads[1], "-o", "assembly", "-t", "2"]
    assert "TESSERACT_FOO" not in log and "is not valid" not in log
    assert "\x1b[" not in log
    assert report["command"].endswith(" -o assembly -t 2")
    assert report["mode"] == "standard" and report["threads"] == 2
    assert report["p2"]["enabled"] == 1                       # the wrapper's profile is in force
    if engines.detect_sketch:
        assert report["p2"]["counters"]["detect_run"] == 1    # and so is the detection sketch
    contigs = tmp_path / "s p é" / "assembly" / "contigs.fasta"
    assert contigs.is_file() and contigs.stat().st_size > 20000


@pytest.mark.engines
def test_assembler_takes_every_option_and_the_retry_ladder(reads, tmp_path):
    engines = _engines()
    assembly = AssemblySettings(threads=2, mode="careful", drop_short_contigs=True, min_contig=300,
                                scaffold=False, polish=False, know_genome_size=True, genome_size_mb=0.1,
                                force=True)
    assert sm.validate_assembly(assembly) == []
    code, log, report, cmd = _assemble(engines, assembly, reads, tmp_path / "run", dict(os.environ),
                                       kmers=sm.THIN_DATA_KMERS)
    assert code == 0, log[-2000:]
    assert report["command"].endswith(
        " -o assembly -t 2 -k 21,33,55 --mode careful --min-contig 300 --no-scaffold --no-polish")
    assert report["mode"] == "careful" and report["polishing"]["run"] is False
    assert [step["k"] for step in report["iterations"]] == [21, 33, 55]
    assert (tmp_path / "run" / "assembly" / "contigs.fasta").stat().st_size > 20000



# --------------------------------------------------------------------------- found in review

def test_ranges_are_the_ones_in_the_comments_of_model_py():
    """"Ranges are in the comments of model.py": read them there, so the two cannot drift."""
    import re
    with open(os.path.join(REPO, "tesseract_win", "model.py"), encoding="utf-8") as handle:
        source = handle.read()
    found = {}
    for name, low, high in re.findall(
            r"^\s+(\w+): (?:int|float) = [^#\n]*#[^\n]*?(\d+(?:\.\d+)?)\.\.(\d+(?:\.\d+)?)", source, re.M):
        found[name] = (float(low), float(high))
    assert set(found) == set(sm.TRIM_RANGES) | set(sm.ASSEMBLY_RANGES)
    for name, limits in found.items():
        assert tuple(float(x) for x in sm.number_range(name)) == limits, name
    assert sm.number_range("threads") == (1, default_threads())
    # Every standard value lies inside its own range, so "Reset to standard" is always valid.
    for name in found:
        low, high = sm.number_range(name)
        settings = TrimSettings() if name in sm.TRIM_RANGES else AssemblySettings()
        assert low <= getattr(settings, name) <= high, name


def test_clamp_number_never_raises_and_always_lands_in_the_range():
    nan, inf = float("nan"), float("inf")
    fields = list(sm.TRIM_RANGES) + list(sm.ASSEMBLY_RANGES) + ["threads"]
    for field in fields:
        low, high = sm.number_range(field)
        for value in (None, "", "abc", nan, inf, -inf, True, False, [], {}, b"3", "3", " 7 ", "2,5",
                      -1, 0, 1, 10 ** 30, -10 ** 400, 10 ** 400, 0.5, 1e308, "1e5", "٣"):
            got = sm.clamp_number(field, value)
            assert low <= got <= high, (field, value, got)
            assert isinstance(got, float if field == "genome_size_mb" else int), (field, value, got)
            assert not isinstance(got, bool)
    assert sm.clamp_number("end_quality", inf) == 36 and sm.clamp_number("end_quality", -inf) == 1
    assert sm.clamp_number("end_quality", nan) == TrimSettings().end_quality       # no number: standard
    assert sm.clamp_number("genome_size_mb", nan) == 5.0 and sm.clamp_number("genome_size_mb", None) == 5.0
    assert sm.clamp_number("genome_size_mb", "2,8") == 2.8 and sm.clamp_number("min_length", "75") == 75
    assert sm.clamp_number("threads", None) == default_threads()
    assert sm.clamp_number("min_contig", 10 ** 400) == 100000
    with pytest.raises(KeyError):
        sm.clamp_number("no_such_field", 1)


def test_typed_number_never_raises_whatever_the_last_good_value_was():
    for last in (None, "ten", float("nan"), [], True):
        assert sm.typed_number("end_quality", "x", last) == (
            10, "Use a whole number from 1 to 36. Set back to 10.")
        assert sm.typed_number("genome_size_mb", "", last) == (
            5.0, "Use a number from 0.1 to 20. Set back to 5.0.")
        assert sm.typed_number("end_quality", "20", last) == (20, "")
    assert sm.typed_number("end_quality", "x", float("inf")) == (
        36, "Use a whole number from 1 to 36. Set back to 36.")
    assert sm.typed_number("min_contig", "", -5) == (1, "Use a whole number from 1 to 100000. Set back to 1.")
    for typed in (None, 12, 3.5, b"1", [], "9" * 500, "\x00", "１２", "²", "1_0", "0x1F", "+5", "1 000"):
        value, message = sm.typed_number("min_length", typed, 50)
        assert isinstance(value, int) and 0 <= value <= 10000 and isinstance(message, str), typed
        value, message = sm.typed_number("genome_size_mb", typed, 5.0)
        assert isinstance(value, float) and 0.1 <= value <= 20 and isinstance(message, str), typed


def test_own_adapters_ticked_with_empty_boxes_is_one_story_everywhere():
    """The tick alone sends nothing to scepter; the words and the fingerprint must agree."""
    ticked = change(own_adapters=True)
    eight = AssemblySettings(threads=8)
    assert flags(ticked) == EXPLICIT
    assert sm.settings_fingerprint(ticked, eight, True) == sm.settings_fingerprint(TrimSettings(), eight, True)
    assert sm.describe(ticked, eight, True).startswith("standard trimming")
    assert sm.differences(ticked, eight) == []
    assert [name for name, _ in sm.validate_trim(ticked)] == ["adapter_r1"]       # and it cannot be run
    # Read 2 alone does reach scepter, so it is a difference (and is refused by the check).
    second = change(own_adapters=True, adapter_r2="AGATCGGAAGAGCGTCGTGT")
    assert flags(second)[:2] == ["--adapter_sequence_r2", "AGATCGGAAGAGCGTCGTGT"]
    assert sm.differences(second, eight) == ["own adapter sequences used"]
    assert sm.describe(second, eight, True).startswith("trimming changed")
    assert sm.settings_fingerprint(second, eight, True) != sm.settings_fingerprint(TrimSettings(), eight, True)


def test_words_and_fingerprint_agree_for_every_combination_of_ticks():
    """describe, differences and the fingerprint are three views of one thing."""
    eight = AssemblySettings(threads=8)
    standard = sm.settings_fingerprint(TrimSettings(), eight, True)
    by_print = {}
    for trim in all_tick_combinations():
        mark = sm.settings_fingerprint(trim, eight, True)
        phrases = [p for p in sm.differences(trim, eight) if p != "trimmed reads not kept"]
        is_standard = sm.describe(trim, eight, True).startswith("standard trimming")
        assert (mark == standard) == is_standard == (phrases == []), trim
        command = tuple(sm.scepter_command("scepter", trim, **IO))
        # Same fingerprint, same command; a different command, a different fingerprint.
        assert by_print.setdefault(mark, command) == command, trim
    assert len(set(by_print.values())) == len(by_print)


def test_only_the_flags_of_the_audit_are_ever_passed():
    """scepter-port.md B2 and B3 name every flag the page may use. Nothing else, in any combination."""
    with_value = {"-i", "-I", "-o", "-O", "-j", "-h", "-R", "-w", "-a", "--adapter_sequence_r2", "-f", "-t",
                  "--cut_front_mean_quality", "--cut_tail_mean_quality", "--qualified_quality_phred",
                  "--unqualified_percent_limit", "--n_base_limit", "--average_qual", "--length_required"}
    alone = {"--no-color", "--no-summary", "--verbose", "-A", "-G", "-x", "-Q", "-L", "--cut_front", "--cut_tail"}
    for trim in all_tick_combinations():
        cmd = sm.scepter_command("scepter", trim, **IO)
        seen, position = [], 1
        while position < len(cmd):
            flag = cmd[position]
            assert flag in with_value or flag in alone, (flag, cmd)
            seen.append(flag)
            if flag in with_value:
                assert position + 1 < len(cmd) and cmd[position + 1] != "", (flag, cmd)
                position += 1
            position += 1
        assert len(seen) == len(set(seen)), cmd                 # no flag twice
        assert ("-A" in seen) == (not trim.remove_adapters)
        assert not ("-A" in seen and ("-a" in seen or "--adapter_sequence_r2" in seen))
        assert ("-Q" in seen) != ("--average_qual" in seen) and ("-L" in seen) != ("--length_required" in seen)
        assert ("--cut_front" in seen) == ("--cut_front_mean_quality" in seen) == trim.trim_low_quality_starts
        assert ("--cut_tail" in seen) == ("--cut_tail_mean_quality" in seen) == trim.trim_low_quality_ends
        if trim.trim_low_quality_starts and trim.trim_low_quality_ends:
            assert cmd[cmd.index("--cut_front_mean_quality") + 1] == cmd[cmd.index("--cut_tail_mean_quality") + 1]
        assert cmd[cmd.index("-h") + 1] == "trimming_report.html"


def test_environment_from_nothing_and_from_windows_style_names():
    assert sm.engine_environment(None, "") == {"NO_COLOR": "1", **NINE}
    assert sm.engine_environment({}, None) == {"NO_COLOR": "1", **NINE}
    windows = {"Path": "C:\\Windows", "=C:": "C:\\Users\\é", "SystemRoot": "C:\\Windows", "no_color": "0",
               "Tesseract_P2_Circ": "banana", "tesseract_deep_norm": "x", "TESSERACT_MODEL_DIR": "D:\\m",
               "XTESSERACT_Y": "kept", "TESSERACTS": "kept"}
    env = sm.engine_environment(windows, "C:\\Prog [1]\\engines\\om2detect.sketch")
    # Windows compares names without regard to case: no second spelling of a name may remain.
    assert len({name.upper() for name in env}) == len(env)
    assert env["Path"] == "C:\\Windows" and env["=C:"] == "C:\\Users\\é"
    assert env["XTESSERACT_Y"] == "kept" and env["TESSERACTS"] == "kept"
    assert env["NO_COLOR"] == "1" and "no_color" not in env and "TESSERACT_MODEL_DIR" not in env
    assert env["TESSERACT_P2_DETECT_SKETCH"] == "C:\\Prog [1]\\engines\\om2detect.sketch"
    assert {name: value for name, value in env.items() if name.upper().startswith("TESSERACT_")} == {
        **NINE, "TESSERACT_P2_DETECT_REPORT": "1",
        "TESSERACT_P2_DETECT_SKETCH": "C:\\Prog [1]\\engines\\om2detect.sketch"}


RANGE_ENDS = [
    change(end_quality=1), change(end_quality=36),
    change(trim_low_quality_starts=True, end_quality=1), change(trim_low_quality_starts=True, end_quality=36),
    change(min_mean_quality=0), change(min_mean_quality=60), change(base_quality=0), change(base_quality=60),
    change(max_low_percent=0), change(max_low_percent=100), change(min_length=0), change(min_length=10000),
    change(fixed_trim=True, trim_front=0, trim_tail=0), change(fixed_trim=True, trim_front=100, trim_tail=100),
    change(own_adapters=True, adapter_r1="ACGTACGTAC"),
    change(own_adapters=True, adapter_r1="ACGT" * 50, adapter_r2="tgca" * 50),
]


@pytest.mark.engines
def test_scepter_accepts_both_ends_of_every_range(reads, tmp_path):
    """What validate_trim lets through, scepter must take: a value it refuses would be a
    failed sample twenty minutes into a batch. --dry-run reads the options and stops."""
    scepter = _scepter()
    files = sm.trimming_files("KP_1")
    for trim in RANGE_ENDS:
        assert sm.validate_trim(trim) == [], trim
        cmd = sm.scepter_command(scepter, trim, read1=reads[0], read2=reads[1], out1=files.read1,
                                 out2=files.read2, json_report=files.json_report,
                                 html_report=files.html_report, title="KP_1", threads=16)
        code, err = _run(cmd + ["--dry-run"], tmp_path)
        assert code == 0 and "error" not in err.lower(), (trim, err)
    assert list(tmp_path.iterdir()) == []                       # a dry run writes nothing


@pytest.mark.engines
def test_scepter_with_awkward_paths_and_sample_names(reads, tmp_path):
    """Review focus 2: spaces, accents, [ ] and non-Latin characters, in the read files, the
    folders and the sample name; and a name that begins like an option."""
    import shutil
    scepter = _scepter()
    source = tmp_path / "Séquençage 2026 [MiSeq] (run 1) 日本語 & co 'x' %TEMP%"
    source.mkdir()
    first = str(source / "KP [1] é 日本_R1.fastq.gz")
    second = str(source / "KP [1] é 日本_R2.fastq.gz")
    shutil.copyfile(reads[0], first)
    shutil.copyfile(reads[1], second)
    for name in ("KP_[1]_é_日本", "-KP", "--help", "a=b"):
        folder = tmp_path / "Résultats [2]" / name / "_in_progress" / "trimming"
        code, err, files = _run_settings(scepter, TrimSettings(), (first, second), folder, name=name)
        assert code == 0 and "error" not in err.lower(), (name, err)
        assert sorted(p.name for p in folder.iterdir()) == sorted(
            [files.read1, files.read2, files.json_report, files.html_report]), name
        config, report = _config(folder / files.json_report)
        assert config["report_title"] == name and config["in1"].replace("\\", "/") == first.replace("\\", "/") and config["out1"] == files.read1
        assert report["summary"]["after_filtering"]["total_reads"] > 0
        assert _records(folder / files.read1).count(b"\n") == _records(folder / files.read2).count(b"\n") > 0


@pytest.mark.engines
def test_scepter_reads_lanes_joined_end_to_end(reads, tmp_path):
    """ux-windows.md D-6: lanes are joined by putting the .gz files one after the other."""
    scepter = _scepter()
    joined = []
    for number, path in enumerate(reads, start=1):
        with open(path, "rb") as handle:
            data = handle.read()
        target = tmp_path / ("joined_R%d.fastq.gz" % number)
        target.write_bytes(data + data)
        joined.append(str(target))
    code, err, files = _run_settings(scepter, TrimSettings(), reads, tmp_path / "one")
    assert code == 0, err
    _, single = _config(tmp_path / "one" / files.json_report)
    code, err, files = _run_settings(scepter, TrimSettings(), joined, tmp_path / "two")
    assert code == 0, err
    _, double = _config(tmp_path / "two" / files.json_report)
    assert double["summary"]["before_filtering"]["total_reads"] == \
        2 * single["summary"]["before_filtering"]["total_reads"] > 0
    assert _records(tmp_path / "two" / files.read1) == 2 * _records(tmp_path / "one" / files.read1)
