# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Unit tests of ``tesseract_win.readcheck``.

The byte-for-byte comparison with the bash script is in ``test_wrapper_parity.py``. Here
each rule is tested on read sets small enough to check by hand, the block-at-a-time code
is compared with the row-by-row code and with the specification model on random and
deliberately broken input, and gzip completeness is compared with ``gzip -t`` itself.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import os
import random
import shutil
import struct
import subprocess
import threading
import time
import zlib

import pytest

from tesseract_win import readcheck
from tesseract_win.readcheck import Cancelled, check_input_pair, check_reads
from tests.parity import harness as H


# --------------------------------------------------------------------------- helpers

def fastq(records, eol=b"\n") -> bytes:
    """``records``: (name, sequence[, quality]) with str or bytes; quality defaults to all ``I``."""
    out = []
    for record in records:
        name, seq = (x if isinstance(x, bytes) else x.encode() for x in record[:2])
        qual = b"I" * len(seq) if len(record) < 3 else (record[2] if isinstance(record[2], bytes) else record[2].encode())
        out.append(b"@" + name + eol + seq + eol + b"+" + eol + qual + eol)
    return b"".join(out)


def gz(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as handle:
        handle.write(data)
    return buffer.getvalue()


def put(folder, name: str, data: bytes) -> str:
    path = os.path.join(str(folder), name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def simple_pair(n: int, length: int = 20, names=("r%d/1", "r%d/2")):
    rng = random.Random(n * 1000 + length)
    one = [(names[0] % i, "".join(rng.choice("ACGT") for _ in range(length))) for i in range(n)]
    two = [(names[1] % i, "".join(rng.choice("ACGT") for _ in range(length))) for i in range(n)]
    return one, two


def check(folder, data1: bytes, data2: bytes, names=("a_1.fq", "a_2.fq"), **options):
    options.setdefault("wording", "upstream")
    return check_reads(put(folder, names[0], data1), put(folder, names[1], data2),
                       shown_read1=names[0], shown_read2=names[1], **options)


def verdicts(result):
    return [(line.verdict, line.name) for line in result.lines]


def line(result, name: str, which: int = 0):
    return [entry for entry in result.lines if entry.name == name][which]


@pytest.fixture
def small_blocks(monkeypatch):
    """Make every file span many blocks, so that block boundaries fall everywhere."""
    monkeypatch.setattr(readcheck, "_IN_BLOCK", 97)
    monkeypatch.setattr(readcheck, "_PLAIN_BLOCK", 113)
    monkeypatch.setattr(readcheck, "_OUT_BLOCK", 211)


def _model():
    spec = importlib.util.spec_from_file_location(
        "tesseract_model", os.path.join(H.HERE, "model", "tesseract_model.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODEL = _model()


def no_reader_threads() -> bool:
    deadline = time.time() + 5
    while time.time() < deadline:
        if not any(t.name == "readcheck-reader" and t.is_alive() for t in threading.enumerate()):
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- the lines of a good pair

def test_a_good_pair_prints_what_the_script_prints(tmp_path):
    one, two = simple_pair(3)
    result = check(tmp_path, gz(fastq(one)), fastq(two), names=("s_1.fq.gz", "s_2.fq"))
    assert result.text == (
        "TesserACT 1.6.2 preflight: s_1.fq.gz + s_2.fq\n"
        "  OK     gzip                   's_1.fq.gz' is complete\n"
        "  OK     format                 3 read pairs, every record well-formed\n"
        "  OK     pairing                mate names agree on every pair\n"
        "  OK     quality                Phred+33 (codes 73-73 / 73-73)\n"
        "  OK     read length            max 20 bp, mean 20.0 bp (short-read library: 2x150 or shorter)\n"
        "  WARN   depth                  about 0x for a 5 Mb genome (assumed; set --genome-size for a better "
        "estimate): too thin for a reliable assembly; expect many breaks\n"
        "  OK     adapters               0.00% of read pairs with adapter read-through\n"
        "  OK     poly-G                 0.00% of read pairs end in a poly-G run\n"
        "preflight: 0 failed, 1 warnings\n")
    assert (result.fails, result.warns, result.ok) == (0, 1, True)
    assert result.data == result.text.encode("ascii")
    stats = result.stats
    assert stats.ran and result.has_statistics
    assert (stats.pairs, stats.bases, stats.max_length, stats.mean_length) == (3, 120, 20, 20.0)
    assert (stats.depth, stats.genome_size_mb, stats.genome_size_assumed) == (0, "5", True)
    assert (stats.adapter_fraction, stats.polyg_fraction, stats.n_fraction) == (0.0, 0.0, 0.0)
    assert result.retry_genome_size == "5"
    assert result.preflight_path == ""


def test_the_path_printed_can_differ_from_the_path_opened(tmp_path):
    one, two = simple_pair(2)
    a, b = put(tmp_path, "x_1.fq.gz", gz(fastq(one))), put(tmp_path, "x_2.fq.gz", gz(fastq(two)))
    shown = check_reads(a, b, wording="upstream", shown_read1="trimming/x_1.fq.gz", shown_read2="trimming/x_2.fq.gz")
    assert shown.header == "TesserACT 1.6.2 preflight: trimming/x_1.fq.gz + trimming/x_2.fq.gz"
    assert line(shown, "gzip").message == "'x_1.fq.gz' is complete"          # the base name, as the script
    plain = check_reads(a, b, wording="upstream")
    assert plain.header == "TesserACT 1.6.2 preflight: %s + %s" % (a, b)
    assert plain.lines == shown.lines


def test_names_with_spaces_accents_and_brackets(tmp_path):
    folder = tmp_path / "Séquençage [run 3] 试验"
    folder.mkdir()
    one, two = simple_pair(4)
    a = put(folder, "KP 001 [a]_R1.fastq.gz", gz(fastq(one)))
    b = put(folder, "KP 001 [a]_R2.fastq.gz", gz(fastq(two)))
    result = check_reads(a, b, out_dir=str(folder / "out put"), free_bytes=50 << 30, available_memory_bytes=8 << 30)
    assert result.fails == 0
    assert "'KP 001 [a]_R1.fastq.gz' is complete" in result.text
    with open(result.preflight_path, "rb") as handle:
        assert handle.read() == result.text.encode("utf-8")
    assert check_input_pair(a, b).ok


# --------------------------------------------------------------------------- rounding

def _thirty(i: int) -> str:
    return ("ACGT" * 9)[i % 4:][:30]


def _threshold_set(adapters: int, polyg: int, n_bases: int):
    """40,000 pairs of 30 bases: so many with adapter, with a poly-G tail, and so many N in all."""
    one, two = [], []
    for i in range(40000):
        a, b = _thirty(i), _thirty(i + 1)
        if i < adapters:
            a = a[:10] + "AGATCGGAAGAGC" + "ACGTACT"
        elif i < adapters + polyg:
            b = b[:20] + "G" * 10
        elif n_bases >= 30:
            a, n_bases = "N" * 30, n_bases - 30
        elif n_bases > 0:
            a, n_bases = "N" * n_bases + a[n_bases:], 0
        one.append(("e%d/1" % i, a))
        two.append(("e%d/2" % i, b))
    return fastq(one), fastq(two)


def test_limits_are_applied_to_the_rounded_fractions(tmp_path):
    """0.050025 is above 0.05 but prints, and is judged, as 0.0500; 0.050075 becomes 0.0501."""
    below = check(tmp_path, *_threshold_set(2001, 801, 120001))
    assert below.stats.sampled_pairs == 40000
    assert (below.stats.adapter_fraction, below.stats.polyg_fraction, below.stats.n_fraction) == (0.05, 0.02, 0.05)
    assert line(below, "adapters").text == "  OK     adapters               5.00% of read pairs with adapter read-through"
    assert line(below, "poly-G").text == "  OK     poly-G                 2.00% of read pairs end in a poly-G run"
    assert not [entry for entry in below.lines if entry.name == "N bases"]
    above = check(tmp_path, *_threshold_set(2003, 803, 120013))
    assert (above.stats.adapter_fraction, above.stats.polyg_fraction, above.stats.n_fraction) == (0.0501, 0.0201, 0.05001)
    assert line(above, "adapters").text == ("  WARN   adapters               5.0% of read pairs carry Illumina adapter "
                                            "sequence: trim them first (fastp, cutadapt)")
    assert line(above, "poly-G").text == ("  WARN   poly-G                 2.0% of read pairs end in a poly-G run "
                                          "(two-colour chemistry): consider fastp --trim_poly_g")
    assert line(above, "N bases").text == "  WARN   N bases                5.0% of bases are N"
    assert above.warns == below.warns + 3


@pytest.mark.parametrize("bases1, bases2, genome, depth, verdict, ending", [
    (15, 14, "0.000002", 14, "WARN", "too thin for a reliable assembly; expect many breaks"),   # 14.5 -> 14
    (16, 15, "0.000002", 16, "WARN", "low; dropouts are likely"),                              # 15.5 -> 16
    (15, 15, "0.000002", 15, "WARN", "low; dropouts are likely"),                              # 15 is not thin
    (14, 14, "0.000002", 14, "WARN", "too thin"),
    (3, 2, "0.000002", 2, "WARN", "too thin"),                                                 # 2.5 -> 2
    (2, 1, "0.000002", 2, "WARN", "too thin"),                                                 # 1.5 -> 2
    (1, 0, "0.000002", 0, "WARN", "too thin"),                                                 # 0.5 -> 0
    (15, 14, "0.000001", 29, "WARN", "low; dropouts are likely"),
    (15, 15, "0.000001", 30, "OK", "for a 0.000001 Mb genome"),
    (20, 20, "0.0000001", 400, "OK", "for a 0.0000001 Mb genome"),
    (20, 20, "0.0000000999", 400, "OK", "Mb genome"),                                          # 400.4 -> 400
    (21, 20, "0.0000001", 410, "WARN", "very deep; runtime and memory grow with depth, results do not improve beyond ~150x"),
])
def test_depth_is_rounded_half_to_even_and_judged_as_an_integer(tmp_path, bases1, bases2, genome, depth, verdict, ending):
    result = check(tmp_path, fastq([("t0/1", "A" * bases1)]), fastq([("t0/2", "C" * bases2)]), genome_size_mb=genome)
    found = line(result, "depth")
    assert result.stats.depth == depth
    assert found.verdict == verdict
    assert found.message.startswith("about %dx for a %s Mb genome" % (depth, genome))
    assert ending in found.message
    assert "assumed" not in found.message
    assert result.stats.genome_size_mb == genome and not result.stats.genome_size_assumed


def test_genome_size_is_printed_as_typed(tmp_path):
    one, two = simple_pair(2)
    assert "for a 5.0 Mb genome:" in line(check(tmp_path, fastq(one), fastq(two), genome_size_mb="5.0"), "depth").message
    assert "for a 5.0 Mb genome:" in line(check(tmp_path, fastq(one), fastq(two), genome_size_mb=5.0), "depth").message
    assert "for a 2.8 Mb genome:" in line(check(tmp_path, fastq(one), fastq(two), genome_size_mb=2.8), "depth").message
    assert line(check(tmp_path, fastq(one), fastq(two), genome_size_mb="1e-6"), "depth").message == (
        "about 80x for a 1e-6 Mb genome")
    assert "for a 5 Mb genome (assumed;" in line(check(tmp_path, fastq(one), fastq(two), genome_size_mb=""), "depth").message


@pytest.mark.parametrize("value, text", [
    (None, None), ("", None), ("5", "5"), ("5.0", "5.0"), ("0.3", "0.3"), (".5", ".5"), ("5.", "5."), ("1e-6", "1e-6"),
    (5, "5"), (5.0, "5.0"), (0.3, "0.3"), (4.6000000000000005, "4.6"), (12.25, "12.25"), (0.000002, "0.000002"),
])
def test_genome_size_text(value, text):
    assert readcheck.genome_size_text(value) == text


@pytest.mark.parametrize("value", ["0", "0.0", "abc", "5,5", "0.3x", "0x10", "-1", "+5", " 5", "5 ", "inf", "nan",
                                   "1e999", "1e-999", 0, -2.5, float("inf"), float("nan"), True])
def test_departure_f2_bad_genome_size_is_refused(tmp_path, value):
    with pytest.raises(ValueError):
        readcheck.genome_size_text(value)
    one, two = simple_pair(1)
    with pytest.raises(ValueError):
        check(tmp_path, fastq(one), fastq(two), genome_size_mb=value)


# --------------------------------------------------------------------------- lines, CR, TAB, empty lines

def test_cr_is_data_not_a_line_end(tmp_path):
    """CRLF files: the CR stays on the name, so ``/1`` is not at its end and every pair differs."""
    one, two = simple_pair(5, 20)
    result = check(tmp_path, fastq(one, b"\r\n"), fastq(two, b"\r\n"))
    assert (line(result, "pairing").verdict, result.stats.name_mismatches) == ("FAIL", 5)
    assert result.stats.first_mismatch == "r0/1\r"
    assert b"5 pairs whose mate names differ (first: r0/1\r); the files" in result.data
    assert result.stats.max_length == 21 and result.stats.bases == 5 * 2 * 21      # the CR counts as a base
    assert line(result, "format").verdict == "OK"                                   # "+\r" still starts with "+"


def test_cr_after_a_comment_passes_but_lengths_are_one_too_long(tmp_path):
    one = [("r%d 1:N:0:ACGT" % i, "ACGTACGTAC") for i in range(4)]
    two = [("r%d 2:N:0:ACGT" % i, "ACGTACGTAC") for i in range(4)]
    result = check(tmp_path, fastq(one, b"\r\n"), fastq(two, b"\r\n"))
    assert result.fails == 0
    assert line(result, "read length").message.startswith("max 11 bp, mean 11.0 bp")


def test_cr_at_a_sampled_quality_position_is_code_0(tmp_path):
    """7 bases: the CR is the 8th quality character, which is one of those looked at."""
    one = [("r%d 1" % i, "ACGTACG") for i in range(3)]
    two = [("r%d 2" % i, "ACGTACG") for i in range(3)]
    result = check(tmp_path, fastq(one, b"\r\n"), fastq(two))
    assert [entry.text for entry in result.lines if entry.name == "quality"] == [
        "  FAIL   quality                -1 file: quality characters below '!' (code 0): not FASTQ Phred+33"]
    assert result.stats.quality_codes == (0, 73, 73, 73)


def test_cr_only_line_ends_make_one_malformed_row(tmp_path):
    one, two = simple_pair(3)
    result = check(tmp_path, fastq(one, b"\r"), fastq(two, b"\r"))
    assert line(result, "format").message == "1 malformed records (first at pair 1); is this FASTQ?"
    assert line(result, "quality").message == "Phred+33 (codes 999-0 / 999-0)"
    assert line(result, "read length").message.startswith("max 0 bp, mean 0.0 bp")


@pytest.mark.parametrize("extra1, extra2", [(b"\n", b""), (b"", b"\n"), (b"\n", b"\n")])
def test_one_trailing_empty_line_counts_as_a_read_missing_from_the_second_file(tmp_path, extra1, extra2):
    """Whichever file has the empty line, the script says the -2 file has 1 fewer reads."""
    one, two = simple_pair(6)
    result = check(tmp_path, fastq(one) + extra1, fastq(two) + extra2)
    assert verdicts(result)[:3] == [("OK", "format"), ("FAIL", "pairing"), ("OK", "pairing")]
    assert result.lines[0].message == "7 read pairs, every record well-formed"
    assert result.lines[1].message == ("the -2 file has 1 fewer reads than the -1 file (truncated, or not the mate file)")
    assert (result.stats.short_read1, result.stats.short_read2) == (0, 1)
    assert (result.stats.blank_rows_read1, result.stats.blank_rows_read2) == (int(bool(extra1)), int(bool(extra2)))


def test_no_final_line_end_is_still_a_line(tmp_path):
    one, two = simple_pair(4)
    result = check(tmp_path, fastq(one)[:-1], fastq(two)[:-1])
    assert result.fails == 0 and result.stats.pairs == 4 and result.stats.bases == 160


def test_an_empty_line_in_the_middle_shifts_every_later_record(tmp_path):
    one, two = simple_pair(10)
    result = check(tmp_path, fastq(one[:4]) + b"\n" + fastq(one[4:]), fastq(two))
    assert line(result, "format").message == "6 malformed records (first at pair 5); is this FASTQ?"
    assert line(result, "pairing").message.startswith("the -2 file has 1 fewer reads")
    assert result.stats.pairs == 11 and result.stats.sampled_pairs == 4


def test_a_file_that_stops_inside_a_record_is_malformed_not_short(tmp_path):
    one, two = simple_pair(5)
    cut = b"\n".join(fastq(two).split(b"\n")[:-3]) + b"\n"         # the last record keeps header and sequence
    result = check(tmp_path, fastq(one), cut)
    assert line(result, "format").message == "1 malformed records (first at pair 5); is this FASTQ?"
    assert [entry.verdict for entry in result.lines if entry.name == "pairing"] == ["OK"]


def test_whole_records_missing(tmp_path):
    one, two = simple_pair(9)
    short2 = check(tmp_path, fastq(one), fastq(two[:-4]))
    assert short2.lines[1].text == ("  FAIL   pairing                the -2 file has 4 fewer reads than the -1 file "
                                    "(truncated, or not the mate file)")
    assert short2.stats.pairs == 9 and short2.stats.bases == 5 * 40
    assert (short2.stats.rows_read1, short2.stats.rows_read2) == (9, 5)
    short1 = check(tmp_path, fastq(one[:-2]), fastq(two))
    assert short1.lines[1].message.startswith("the -1 file has 2 fewer reads than the -2 file")
    assert verdicts(short1)[:3] == [("OK", "format"), ("FAIL", "pairing"), ("OK", "pairing")]


def test_a_tab_in_a_header_moves_the_fields(tmp_path):
    one, two = simple_pair(5)
    tabbed = [(name + "\tBC:Z:ACGT", seq) for name, seq in two]
    result = check(tmp_path, fastq(one), fastq(tabbed))
    assert line(result, "format").message == "5 malformed records (first at pair 1); is this FASTQ?"
    only_third = [two[0], two[1], (two[2][0] + "\tx", two[2][1]), two[3], two[4]]
    result = check(tmp_path, fastq(one), fastq(only_third))
    assert line(result, "format").message == "1 malformed records (first at pair 3); is this FASTQ?"
    assert result.stats.sampled_pairs == 4


def test_first_at_pair_is_the_first_malformed_or_length_mismatch(tmp_path):
    one, two = simple_pair(8)
    one[2] = (one[2][0], one[2][1], "I" * 19)                       # pair 3: quality one short
    data = fastq(one[:5]) + b"r5/1 no at-sign\n" + fastq(one[5:6]).split(b"\n", 1)[1] + fastq(one[6:])
    result = check(tmp_path, data, fastq(two))
    assert [entry.message for entry in result.lines if entry.name == "format"] == [
        "1 malformed records (first at pair 3); is this FASTQ?",
        "1 records whose sequence and quality lengths differ"]
    assert (result.stats.first_malformed, result.stats.first_length_mismatch, result.stats.first_bad_pair) == (6, 3, 3)


def test_names_are_compared_up_to_the_first_space_without_slash_suffix(tmp_path):
    seq = "ACGTACGTAC"
    cases = [
        ("r1/1", "r1/2", True), ("r1 1:N:0", "r1 2:N:0", True), ("r1", "r1", True), ("r1/1 x", "r1/2 y", True),
        ("SRR1.7.1", "SRR1.7.2", False), ("r1/3", "r1/4", False), ("r1/1", "r2/2", False), ("r1", "r1/2", True),
        ("/1", "/2", True), ("r1/1/1", "r1/1/2", True), ("r1_1", "r1_2", False),
    ]
    for name1, name2, agree in cases:
        result = check(tmp_path, fastq([(name1, seq)] * 2), fastq([(name2, seq)] * 2))
        assert (result.stats.name_mismatches == 0) == agree, (name1, name2)
        slow = check(tmp_path, fastq([(name1, seq)] * 2), fastq([(name2, seq)] * 2), _fast=False)
        assert slow.text == result.text
    result = check(tmp_path, fastq([("a/1", seq), ("b/1", seq), ("c/1", seq)]), fastq([("a/2", seq), ("x/2", seq), ("y/2", seq)]))
    assert line(result, "pairing").message == ("2 pairs whose mate names differ (first: b); the files are not in the "
                                               "same order, or one is truncated")


def test_read_name_bytes_are_kept_unchanged(tmp_path):
    odd = b"r\xff\xfe\xe9"
    result = check(tmp_path, fastq([(odd + b"/1", "ACGT")]), fastq([(b"other/2", "ACGT")]))
    assert b"(first: r\xff\xfe\xe9);" in result.data
    assert readcheck.encode_text(result.stats.first_mismatch) == odd


def test_quality_rules(tmp_path):
    def quality_lines(q1: str, q2: str):
        result = check(tmp_path, fastq([("a/1", "A" * len(q1), q1)]), fastq([("a/2", "A" * len(q2), q2)]))
        return [(entry.verdict, entry.message) for entry in result.lines if entry.name == "quality"]

    assert quality_lines("J" * 20, "J" * 20) == [("OK", "Phred+33 (codes 74-74 / 74-74)")]
    assert quality_lines("K" * 20, "I" * 20) == [
        ("FAIL", "-1 file: qualities look Phred+64 (codes 75-75); convert to Phred+33 first (e.g. seqtk seq -VQ64)")]
    assert quality_lines("I" * 20, "hhhhhhhB" + "h" * 12) == [
        ("FAIL", "-2 file: qualities look Phred+64 (codes 66-104); convert to Phred+33 first (e.g. seqtk seq -VQ64)")]
    # only characters 1, 8, 15, ... are looked at
    assert quality_lines("I" + "h" * 6 + "I" + "h" * 6 + "I", "I" * 15) == [("OK", "Phred+33 (codes 73-73 / 73-73)")]
    assert quality_lines(" " + "I" * 9, "I" * 10) == [
        ("FAIL", "-1 file: quality characters below '!' (code 0): not FASTQ Phred+33")]
    assert quality_lines("I" * 7 + "\x7f", "#" * 8 + "~") == [
        ("FAIL", "-1 file: quality characters below '!' (code 0): not FASTQ Phred+33")]
    assert quality_lines("!" * 8, "#" + "x" * 6 + "~") == [("OK", "Phred+33 (codes 33-33 / 35-126)")]
    assert quality_lines("", "") == [("OK", "Phred+33 (codes 999-0 / 999-0)")]


def test_library_text_comes_from_the_longest_read(tmp_path):
    one = [("a/1", "A" * 100), ("b/1", "A" * 240)]
    two = [("a/2", "A" * 100), ("b/2", "A" * 100)]
    assert line(check(tmp_path, fastq(one), fastq(two)), "read length").message == (
        "max 240 bp, mean 135.0 bp (long-read MiSeq-type library: 2x250/2x300)")
    one[1] = ("b/1", "A" * 239)
    assert line(check(tmp_path, fastq(one), fastq(two)), "read length").message == (
        "max 239 bp, mean 134.8 bp (short-read library: 2x150 or shorter)")


def test_only_upper_case_n_is_counted(tmp_path):
    one = [("a/1", "N" * 10 + "n" * 10)]
    two = [("a/2", "ACGTRYKMacgt" + "A" * 8)]
    result = check(tmp_path, fastq(one), fastq(two))
    assert result.stats.n_fraction == 0.25
    assert line(result, "N bases").message == "25.0% of bases are N"


def test_sampled_statistics_stop_at_the_row_limit_but_counts_do_not(tmp_path, monkeypatch):
    """The first rows are the sample; the longest read and the bases come from every row."""
    monkeypatch.setattr(readcheck, "SAMPLE_ROWS", 10)
    early = [("s%d/1" % i, "ACGT" * 5) for i in range(10)]
    late = [("s%d/1" % i, "AGATCGGAAGAGC" + "N" * 17 + "G" * 10, "h" * 40) for i in range(10, 15)]
    mates = [("s%d/2" % i, "ACGT" * 5) for i in range(15)]
    for fast in (True, False):
        result = check(tmp_path, fastq(early + late), fastq(mates), _fast=fast)
        stats = result.stats
        assert (stats.pairs, stats.sampled_pairs, stats.max_length, stats.mean_length) == (15, 10, 40, 20.0)
        assert stats.bases == 10 * 40 + 5 * 60
        assert (stats.adapter_fraction, stats.polyg_fraction, stats.n_fraction) == (0.0, 0.0, 0.0)
        assert stats.quality_codes == (73, 73, 73, 73)
    # a malformed row among the first ten still uses up one place of the sample
    broken = fastq(early[:3]) + b"not\na\nrecord\nat all\n" + fastq(early[4:] + late)
    for fast in (True, False):
        stats = check(tmp_path, broken, fastq(mates), _fast=fast).stats
        assert (stats.malformed, stats.sampled_pairs) == (1, 9)


# --------------------------------------------------------------------------- the file checks

def test_missing_empty_and_folder(tmp_path):
    one, two = simple_pair(3)
    good = put(tmp_path, "g_1.fq.gz", gz(fastq(one)))
    empty = put(tmp_path, "e_2.fq.gz", b"")
    missing = str(tmp_path / "nope_2.fq")
    result = check_reads(good, empty, wording="upstream", shown_read1="g_1.fq.gz", shown_read2="in/e_2.fq.gz")
    assert result.text == ("TesserACT 1.6.2 preflight: g_1.fq.gz + in/e_2.fq.gz\n"
                           "  OK     gzip                   'g_1.fq.gz' is complete\n"
                           "  FAIL   file                   'in/e_2.fq.gz' is empty\n"
                           "preflight: 1 failed, 0 warnings\n")
    assert not result.stats.ran and not result.has_statistics and result.retry_genome_size is None
    result = check_reads(missing, good, wording="upstream", shown_read1="nope", shown_read2="g")
    assert [entry.text for entry in result.lines] == ["  FAIL   file                   cannot read 'nope'",
                                                      "  OK     gzip                   'g' is complete"]
    result = check_reads(str(tmp_path), good, wording="upstream", shown_read1="a folder", shown_read2="g")   # F7
    assert result.lines[0].text == "  FAIL   file                   cannot read 'a folder'"
    result = check_reads(missing, missing, wording="upstream")
    assert verdicts(result) == [("FAIL", "file"), ("FAIL", "file")]


def test_gzip_is_recognised_by_its_first_bytes_not_by_its_name(tmp_path):
    one, two = simple_pair(3)
    result = check(tmp_path, fastq(one), gz(fastq(two)), names=("lies_1.fq.gz", "lies_2.fq"))
    assert verdicts(result)[0] == ("OK", "gzip") and result.lines[0].message == "'lies_2.fq' is complete"
    assert result.fails == 0 and result.stats.pairs == 3


def test_gzip_of_nothing_is_complete_but_holds_no_records(tmp_path):
    result = check(tmp_path, gz(b""), gz(b""), names=("n_1.fq.gz", "n_2.fq.gz"))
    assert [entry.text for entry in result.lines] == [
        "  OK     gzip                   'n_1.fq.gz' is complete",
        "  OK     gzip                   'n_2.fq.gz' is complete",
        "  FAIL   format                 no FASTQ records read"]
    assert result.stats.ran and result.stats.pairs == 0 and result.stats.depth is None
    assert not result.has_statistics and result.retry_genome_size is None


def test_multi_member_gzip_is_read_across_its_members(tmp_path, small_blocks):
    one, two = simple_pair(40)
    data1 = gz(fastq(one[:13])) + gz(fastq(one[13:14])) + gz(b"") + gz(fastq(one[14:]))
    cut = fastq(two)
    data2 = gz(cut[:777]) + gz(cut[777:])                           # a member boundary inside a record
    result = check(tmp_path, data1, data2, names=("m_1.fq.gz", "m_2.fq.gz"))
    assert verdicts(result)[:4] == [("OK", "gzip"), ("OK", "gzip"), ("OK", "format"), ("OK", "pairing")]
    assert result.stats.pairs == 40 and result.stats.bases == 1600


@pytest.mark.parametrize("damage", ["cut 1", "cut 8", "cut 300", "crc", "length", "garbage", "zeros then garbage",
                                    "second member cut", "stray byte"])
def test_damaged_gzip_fails_and_stops_the_statistics(tmp_path, damage, small_blocks):
    one, two = simple_pair(60)
    whole = gz(fastq(two))
    data = {
        "cut 1": whole[:-1], "cut 8": whole[:-8], "cut 300": whole[:-300],
        "crc": whole[:-8] + bytes([whole[-8] ^ 1]) + whole[-7:],
        "length": whole[:-4] + bytes([whole[-4] ^ 1]) + whole[-3:],
        "garbage": whole + b"GARBAGE\n", "zeros then garbage": whole + b"\0" * 300 + b"x",
        "second member cut": whole + whole[:-20], "stray byte": whole + b"\x1f",
    }[damage]
    result = check(tmp_path, gz(fastq(one)), data, names=("t_1.fq.gz", "sub/t_2.fq.gz".replace("sub/", "")))
    assert [entry.text for entry in result.lines] == [
        "  OK     gzip                   't_1.fq.gz' is complete",
        "  FAIL   gzip                   't_2.fq.gz' is truncated or corrupt (gzip -t failed)"]
    assert not result.stats.ran and result.stats.pairs == 0
    assert no_reader_threads()


def test_zero_bytes_after_the_last_member_are_ignored(tmp_path, small_blocks):
    one, two = simple_pair(30)
    result = check(tmp_path, gz(fastq(one)) + b"\0", gz(fastq(two)) + b"\0" * 5000, names=("z_1.fq.gz", "z_2.fq.gz"))
    assert result.fails == 0 and result.stats.pairs == 30


def test_a_damaged_first_file_still_gets_the_second_one_tested(tmp_path):
    one, two = simple_pair(50)
    result = check(tmp_path, gz(fastq(one))[:40], gz(fastq(two)), names=("d_1.fq.gz", "d_2.fq.gz"))
    assert verdicts(result) == [("FAIL", "gzip"), ("OK", "gzip")]
    both = check(tmp_path, gz(fastq(one))[:40], gz(fastq(two)) + b"x", names=("d_1.fq.gz", "d_2.fq.gz"))
    assert verdicts(both) == [("FAIL", "gzip"), ("FAIL", "gzip")]
    empty_then_bad = check(tmp_path, b"", gz(fastq(two))[:-5], names=("d_1.fq.gz", "d_2.fq.gz"))
    assert verdicts(empty_then_bad) == [("FAIL", "file"), ("FAIL", "gzip")]


def _gzip_member(data: bytes, flags: int = 0, method: int = 8, extra: bytes = b"", header_crc=None) -> bytes:
    head = struct.pack("<BBBBIBB", 0x1f, 0x8b, method, flags, 0, 0, 255) + extra
    if flags & 2:
        head += struct.pack("<H", (zlib.crc32(head) & 0xffff) if header_crc is None else header_crc)
    deflater = zlib.compressobj(6, zlib.DEFLATED, -15)
    body = deflater.compress(data) + deflater.flush()
    return head + body + struct.pack("<II", zlib.crc32(data) & 0xffffffff, len(data) & 0xffffffff)


def _gzip_variants():
    text = fastq(simple_pair(50)[0])
    whole = gz(text)
    return {
        "one member": whole, "two members": whole + whole, "three members": whole * 3, "of nothing": gz(b""),
        "one zero byte": whole + b"\0", "512 zero bytes": whole + b"\0" * 512,
        "zeros then a member": whole + b"\0" * 16 + whole, "zeros then garbage": whole + b"\0\0x",
        "garbage": whole + b"GARBAGE\n", "newline after": whole + b"\n",
        "cut 1": whole[:-1], "cut 4": whole[:-4], "cut 8": whole[:-8], "cut 9": whole[:-9], "cut half": whole[:len(whole) // 2],
        "header only": whole[:10], "magic only": whole[:2] + b"\x08",
        "crc wrong": whole[:-8] + bytes([whole[-8] ^ 0x40]) + whole[-7:],
        "length wrong": whole[:-1] + bytes([whole[-1] ^ 0x01]),
        "second member cut": whole + whole[:-30], "second magic only": whole + b"\x1f\x8b", "stray 1f": whole + b"\x1f",
        "with a file name": _gzip_member(text, 8, extra=b"reads.fq\0"),
        "with a comment": _gzip_member(text, 16, extra=b"made by a test\0"),
        "with an extra field": _gzip_member(text, 4, extra=struct.pack("<H", 6) + b"BC\x02\x00\x10\x00"),
        "with a header checksum": _gzip_member(text, 2),
        "with a wrong header checksum": _gzip_member(text, 2, header_crc=0x1234),
        "reserved flag": _gzip_member(text, 0x20), "reserved flags": _gzip_member(text, 0xc0),
        "unknown method": _gzip_member(text, 0, method=7),
        "bit flipped in the data": whole[:60] + bytes([whole[60] ^ 0x10]) + whole[61:],
        "blocked like bgzip": b"".join(gz(text[i:i + 500]) for i in range(0, len(text), 500)) + gz(b""),
    }


@pytest.mark.skipif(shutil.which("gzip") is None, reason="needs gzip")
@pytest.mark.parametrize("variant", sorted(_gzip_variants()))
def test_complete_means_what_gzip_t_means(tmp_path, variant, small_blocks):
    path = put(tmp_path, "v.gz", _gzip_variants()[variant])
    said = subprocess.run(["gzip", "-t", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    assert readcheck.gzip_complete(path) == (said == 0), "gzip -t exits with %d" % said


def test_gzip_verdicts_do_not_depend_on_a_gzip_program(tmp_path):
    """The same table without gzip installed (Windows): what gzip 1.13 answered when it was asked."""
    complete = {"one member", "two members", "three members", "of nothing", "one zero byte", "512 zero bytes",
                "with a file name", "with a comment", "with an extra field", "with a header checksum",
                "blocked like bgzip"}
    for variant, data in _gzip_variants().items():
        assert readcheck.gzip_complete(put(tmp_path, "v.gz", data)) == (variant in complete), variant


def test_same_file(tmp_path):
    one, _two = simple_pair(4)
    path = put(tmp_path, "s_1.fq.gz", gz(fastq(one)))
    copy = put(tmp_path, "copy.fq.gz", gz(fastq(one)))
    again = check_reads(path, path, wording="upstream", shown_read1="a", shown_read2="a")
    assert [entry.text for entry in again.lines] == [
        "  OK     gzip                   'a' is complete", "  OK     gzip                   'a' is complete",
        "  FAIL   pairing                -1 and -2 are the same file"]
    spelled = check_reads(path, os.path.join(str(tmp_path), ".", "s_1.fq.gz"), wording="upstream")
    assert verdicts(spelled)[-1] == ("FAIL", "pairing") and not spelled.stats.ran
    assert check_reads(path, copy, wording="upstream").fails == 0          # the same bytes are not the same file
    for make in (os.link, os.symlink):
        other = str(tmp_path / ("via_" + make.__name__))
        try:
            make(path, other)
        except (OSError, NotImplementedError, AttributeError):
            continue
        assert verdicts(check_reads(path, other, wording="upstream"))[-1] == ("FAIL", "pairing")
    damaged = put(tmp_path, "bad.fq.gz", gz(fastq(one))[:-3])
    assert verdicts(check_reads(damaged, damaged, wording="upstream")) == [
        ("FAIL", "gzip"), ("FAIL", "gzip"), ("FAIL", "pairing")]


def test_a_file_that_fails_while_it_is_read_raises(tmp_path, monkeypatch):
    one, two = simple_pair(5)
    a, b = put(tmp_path, "a_1.fq", fastq(one)), put(tmp_path, "a_2.fq", fastq(two))

    def unplugged(self, handle):
        raise OSError(5, "the drive is gone")

    monkeypatch.setattr(readcheck._Source, "_copy", unplugged)
    with pytest.raises(OSError, match="the drive is gone"):
        check_reads(a, b)
    assert no_reader_threads()


# --------------------------------------------------------------------------- disk and memory

GB = 1 << 30


def test_run_mode_adds_disk_and_memory_and_writes_the_file(tmp_path):
    one, two = simple_pair(3)
    a, b = put(tmp_path, "a_1.fq", fastq(one)), put(tmp_path, "a_2.fq", fastq(two))
    out = tmp_path / "new" / "assembly"
    result = check_reads(a, b, out_dir=str(out), wording="upstream", shown_read1="a_1.fq", shown_read2="a_2.fq",
                         shown_out="assembly", free_bytes=7 * GB + 5, available_memory_bytes=9 * GB + 1000)
    assert [entry.text for entry in result.lines[-2:]] == ["  OK     disk                   7 GB free in assembly",
                                                           "  OK     memory                 9 GB available"]
    assert result.preflight_path == str(out / "preflight.txt")
    with open(result.preflight_path, "rb") as handle:
        written = handle.read()
    assert written == result.data and b"\r" not in written and written.endswith(b"preflight: 0 failed, 1 warnings\n")
    assert sorted(os.listdir(str(out))) == ["preflight.txt"]                 # no temporary file is left
    no_out = check_reads(a, b, wording="upstream")
    assert not [entry for entry in no_out.lines if entry.name in ("disk", "memory")]


def test_disk_and_memory_limits(tmp_path):
    one, two = simple_pair(3)
    filler = (1 << 20) + 1023 - (len(fastq(one)) + len(fastq(two))) % 1024
    a = put(tmp_path, "a_1.fq", fastq(one) + b"A" * filler)            # about 1 MB; 1023 bytes over a whole kB
    b = put(tmp_path, "a_2.fq", fastq(two))
    size = os.path.getsize(a) + os.path.getsize(b)
    assert size % 1024 == 1023
    need_kb = size // 1024 * 3 + 2 + 2097152                            # (size * 3) / 1024, rounded down

    def last(free, memory):
        result = check_reads(a, b, out_dir=str(tmp_path / "o"), wording="upstream", shown_out="o",
                             free_bytes=free, available_memory_bytes=memory)
        return [(entry.verdict, entry.name, entry.message) for entry in result.lines
                if entry.name in ("disk", "memory")], result

    lines, result = last(need_kb * 1024, 4 * GB)
    assert lines == [("OK", "disk", "2 GB free in o"), ("OK", "memory", "4 GB available")]
    lines, result = last(need_kb * 1024 - 1, 4 * GB - 1)
    assert lines == [("FAIL", "disk", "only 2 GB free in o; need about 2 GB"),      # one kB short of the need
                     ("WARN", "memory", "only 3 GB available; a bacterial assembly needs 2-6 GB")]
    assert result.closing == "preflight: 2 failed, 2 warnings"            # format FAIL, disk FAIL; depth, memory
    lines, _ = last(GB - 1, GB)
    assert lines == [("FAIL", "disk", "only 0 GB free in o; need about 2 GB"),
                     ("WARN", "memory", "only 1 GB available; a bacterial assembly needs 2-6 GB")]
    lines, _ = last(0, None)
    assert lines == [("FAIL", "disk", "only 0 GB free in o; need about 2 GB")]      # no memory line
    lines, _ = last(100 * GB, 0)
    assert lines == [("OK", "disk", "100 GB free in o")]
    lines, _ = last(100 * GB, 1023)
    assert lines == [("OK", "disk", "100 GB free in o")]                   # under one kB: nothing known


def test_free_space_is_asked_from_the_system_when_not_given(tmp_path, monkeypatch):
    one, two = simple_pair(3)
    a, b = put(tmp_path, "a_1.fq", fastq(one)), put(tmp_path, "a_2.fq", fastq(two))
    asked = []

    def free(path):
        asked.append(path)
        return 33 * GB

    monkeypatch.setattr(readcheck.fsutil, "free_bytes", free)
    result = check_reads(a, b, out_dir=str(tmp_path / "o"), wording="upstream", shown_out="o")
    assert asked == [str(tmp_path / "o")]
    assert result.lines[-1].text == "  OK     disk                   33 GB free in o"
    monkeypatch.setattr(readcheck.fsutil, "free_bytes", lambda path: None)
    result = check_reads(a, b, out_dir=str(tmp_path / "o"), wording="upstream", shown_out="o")
    assert result.lines[-1].text == "  FAIL   disk                   only 0 GB free in o; need about 2 GB"


def test_disk_and_memory_are_checked_after_failures_but_not_when_a_file_cannot_be_opened(tmp_path):
    one, two = simple_pair(3)
    a = put(tmp_path, "a_1.fq.gz", gz(fastq(one))[:-2])
    b = put(tmp_path, "a_2.fq", b"")
    out = str(tmp_path / "o")
    failed = check_reads(a, b, out_dir=out, wording="upstream", free_bytes=9 * GB, available_memory_bytes=9 * GB)
    assert verdicts(failed) == [("FAIL", "gzip"), ("FAIL", "file"), ("OK", "disk"), ("OK", "memory")]
    gone = check_reads(a, str(tmp_path / "gone.fq"), out_dir=out, wording="upstream", free_bytes=9 * GB,
                       available_memory_bytes=9 * GB)
    assert verdicts(gone) == [("FAIL", "gzip"), ("FAIL", "file")]
    with open(os.path.join(out, "preflight.txt"), "rb") as handle:
        assert handle.read() == gone.data


# --------------------------------------------------------------------------- wording

def test_windows_wording_changes_three_sentences_and_nothing_else(tmp_path):
    one, two = [], []
    for i in range(40):
        seq1 = "ACGTAC" + "AGATCGGAAGAGC" + "A" if i < 8 else "ACGT" * 5
        seq2 = "ACGTACGTAC" + "G" * 10 if i >= 30 else "ACGT" * 5
        one.append(("r%d/1" % i, seq1, "h" * 12 + "ZZZZBBBB"))
        two.append(("r%d/2" % i, seq2))
    data1, data2 = fastq(one), fastq(two)
    upstream = check(tmp_path, data1, data2, wording="upstream")
    windows = check(tmp_path, data1, data2, wording="windows")
    default = check_reads(put(tmp_path, "a_1.fq", data1), put(tmp_path, "a_2.fq", data2),
                          shown_read1="a_1.fq", shown_read2="a_2.fq")
    assert default.text == windows.text
    assert verdicts(upstream) == verdicts(windows) and upstream.stats == windows.stats
    changed = [(u.name, u.message, w.message) for u, w in zip(upstream.lines, windows.lines) if u != w]
    assert changed == [
        ("quality", "-1 file: qualities look Phred+64 (codes 90-104); convert to Phred+33 first (e.g. seqtk seq -VQ64)",
         "-1 file: qualities look Phred+64 (codes 90-104); trim this sample first (Trimming page): trimming rewrites "
         "the qualities as Phred+33"),
        ("adapters", "20.0% of read pairs carry Illumina adapter sequence: trim them first (fastp, cutadapt)",
         "20.0% of read pairs carry Illumina adapter sequence: trim them first (Remove adapters on the Trimming page)"),
        ("poly-G", "25.0% of read pairs end in a poly-G run (two-colour chemistry): consider fastp --trim_poly_g",
         "25.0% of read pairs end in a poly-G run (two-colour chemistry): consider Trim poly-G tails on the "
         "Trimming page"),
    ]
    for word in ("fastp", "cutadapt", "seqtk"):
        assert word not in windows.text
    with pytest.raises(ValueError):
        check(tmp_path, data1, data2, wording="linux")


# --------------------------------------------------------------------------- cancel and progress

def _medium_pair(tmp_path, n=3000):
    one, two = simple_pair(n, 40)
    return put(tmp_path, "m_1.fq.gz", gz(fastq(one))), put(tmp_path, "m_2.fq.gz", gz(fastq(two)))


def test_progress_rises_from_0_to_1(tmp_path, monkeypatch):
    monkeypatch.setattr(readcheck, "_IN_BLOCK", 2048)
    a, b = _medium_pair(tmp_path)
    seen = []
    result = check_reads(a, b, progress=seen.append)
    assert result.stats.pairs == 3000
    blocks = -(-os.path.getsize(a) // 2048) + -(-os.path.getsize(b) // 2048)
    assert len(seen) >= blocks, "progress follows the blocks the check has worked through, one step each"
    assert all(0.0 <= value <= 1.0 for value in seen)
    assert seen == sorted(seen), "progress must never go back"
    assert seen[-1] == 1.0 and seen.count(1.0) == 1
    assert seen[0] < 0.2
    main = threading.current_thread()
    threads = []
    check_reads(a, b, progress=lambda fraction: threads.append(threading.current_thread()))
    assert set(threads) == {main}, "progress is reported in the thread that asked for the check"


def test_progress_of_plain_files_and_of_a_check_that_fails_early(tmp_path, monkeypatch):
    monkeypatch.setattr(readcheck, "_PLAIN_BLOCK", 4096)
    one, two = simple_pair(2000, 40)
    a, b = put(tmp_path, "p_1.fq", fastq(one)), put(tmp_path, "p_2.fq", fastq(two))
    seen = []
    check_reads(a, b, progress=seen.append)
    blocks = -(-os.path.getsize(a) // 4096) + -(-os.path.getsize(b) // 4096)
    assert len(seen) >= blocks and seen == sorted(seen) and seen[-1] == 1.0
    steps = [later - earlier for earlier, later in zip(seen, seen[1:])]
    assert max(steps) < 0.03, "no jumps: each block moves the figure by about one hundredth"
    seen = []
    check_reads(a, str(tmp_path / "missing.fq"), progress=seen.append)
    assert seen == [1.0]
    monkeypatch.setattr(readcheck, "_IN_BLOCK", 1024)
    seen = []
    c = put(tmp_path, "g_1.fq.gz", gz(fastq(one)))
    check_reads(c, put(tmp_path, "e.fq", b""), progress=seen.append)       # only gzip -t is left to do
    assert len(seen) >= 2 and seen == sorted(seen) and seen[-1] == 1.0
    seen = []
    assert check_input_pair(a, b, progress=seen.append).ok
    assert len(seen) > 20 and seen == sorted(seen) and seen[-1] == 1.0


def test_cancel_before_the_start(tmp_path):
    a, b = _medium_pair(tmp_path, 50)
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        check_reads(a, b, cancel=stop, out_dir=str(tmp_path / "o"), free_bytes=GB, available_memory_bytes=GB)
    assert not os.path.exists(str(tmp_path / "o" / "preflight.txt"))
    with pytest.raises(Cancelled):
        check_input_pair(a, b, cancel=stop)
    with pytest.raises(Cancelled):
        check_reads(a, str(tmp_path / "missing"), cancel=stop)
    assert no_reader_threads()


def test_cancel_at_the_very_end_writes_nothing(tmp_path):
    """Stop pressed while the last block is being looked at: still stopped, and no file appears."""
    a, b = _medium_pair(tmp_path, 200)
    stop = threading.Event()
    seen = []

    def progress(fraction):
        seen.append(fraction)
        if fraction >= 0.999:
            stop.set()

    with pytest.raises(Cancelled):
        check_reads(a, b, cancel=stop, progress=progress, out_dir=str(tmp_path / "o"), free_bytes=GB)
    assert seen[-1] == 0.999
    assert not os.path.exists(str(tmp_path / "o" / "preflight.txt"))
    with pytest.raises(Cancelled):
        check_input_pair(a, b, cancel=stop, progress=progress)


def test_cancel_after_the_last_block_writes_nothing(tmp_path, monkeypatch):
    """Stop pressed (in another thread) after the files were read to the end, before the result is out."""
    a, b = _medium_pair(tmp_path, 200)
    stop = threading.Event()
    statistics = readcheck._stats

    def stop_now(*args):
        stop.set()                           # the files have been read; the lines are being made
        return statistics(*args)

    monkeypatch.setattr(readcheck, "_stats", stop_now)
    seen = []
    with pytest.raises(Cancelled):
        check_reads(a, b, cancel=stop, progress=seen.append, out_dir=str(tmp_path / "o"), free_bytes=GB)
    assert not os.path.exists(str(tmp_path / "o" / "preflight.txt"))
    assert 1.0 not in seen
    stop.clear()
    seen = []
    with pytest.raises(Cancelled):
        check_input_pair(a, b, cancel=stop, progress=seen.append)
    assert 1.0 not in seen


@pytest.mark.parametrize("at", [0.05, 0.5, 0.9])
def test_cancel_in_the_middle(tmp_path, monkeypatch, at):
    monkeypatch.setattr(readcheck, "_IN_BLOCK", 1024)
    a, b = _medium_pair(tmp_path)
    stop = threading.Event()
    seen = []

    def progress(fraction):
        seen.append(fraction)
        if fraction >= at:
            stop.set()

    started = time.time()
    with pytest.raises(Cancelled):
        check_reads(a, b, cancel=stop, progress=progress, out_dir=str(tmp_path / "o"), free_bytes=GB)
    assert time.time() - started < 5
    assert seen and seen[-1] < 1.0, "a cancelled check never reports that it finished"
    assert at <= seen[-1] < at + 0.05, "the check must stop at once, not read on to the end"
    assert not os.path.exists(str(tmp_path / "o" / "preflight.txt"))
    assert no_reader_threads(), "the reader threads must end when the check is cancelled"
    os.remove(a)                                                           # and have let go of the files
    os.remove(b)


def test_cancel_while_only_gzip_is_tested(tmp_path, monkeypatch):
    """The other file is empty, so nothing is left but to test this one to its end; that too stops.

    The reader is slowed down (about a second for the whole file) so that "at once" can be
    told from "at the end" without depending on how fast the machine is.
    """
    emit = readcheck._Source._emit

    def slow_emit(self, block):
        time.sleep(0.005)
        emit(self, block)

    monkeypatch.setattr(readcheck._Source, "_emit", slow_emit)
    monkeypatch.setattr(readcheck, "_IN_BLOCK", 256)
    monkeypatch.setattr(readcheck, "_POLL", 0.005)
    a, _b = _medium_pair(tmp_path)
    assert os.path.getsize(a) > 150 * 256
    stop = threading.Event()
    seen = []

    def progress(fraction):
        seen.append(fraction)
        if fraction > 0.3:
            stop.set()

    with pytest.raises(Cancelled):
        check_reads(a, put(tmp_path, "empty.fq", b""), cancel=stop, progress=progress)
    assert 0.3 < seen[-1] < 0.6, "the test of the file must stop at once, not run on to its end"
    assert no_reader_threads()


def test_cancel_from_another_thread(tmp_path, monkeypatch):
    """As the window does it: the check runs in one thread and Stop is pressed in another."""
    monkeypatch.setattr(readcheck, "_IN_BLOCK", 1024)
    a, b = _medium_pair(tmp_path)
    stop, started, pressed = threading.Event(), threading.Event(), threading.Event()
    outcome = []

    def progress(fraction):
        if fraction > 0.2:
            started.set()
            pressed.wait(5)                  # hold the check here until the other thread has pressed Stop

    def work():
        try:
            outcome.append(check_reads(a, b, cancel=stop, progress=progress))
        except Cancelled as exc:
            outcome.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    assert started.wait(10)
    stop.set()
    pressed.set()
    worker.join(10)
    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], Cancelled)
    assert no_reader_threads()


# --------------------------------------------------------------------------- one code path against the other

def _noisy_records(rng: random.Random, n: int, mate: int, style: int, noise: float):
    """Records of one file as a list of line lists; ``noise`` is the chance of each kind of damage."""
    out = []
    for i in range(n):
        length = rng.choice((0, 1, 6, 7, 8, 13, 14, 15, 30))
        seq = "".join(rng.choice("ACGTN" if rng.random() < 0.2 else "ACGT") for _ in range(length))
        qual = "".join(rng.choice("II#5F~!hZB") for _ in range(length))
        if style == 0:
            name = "r%d/%d" % (i, mate)
        elif style == 1:
            name = "M01:%d:%d %d:N:0:ACGT" % (i, i * 7, mate)
        elif style == 2:
            name = "read%d" % i
        else:
            name = "SRR1.%d.%d" % (i, mate)
        lines = ["@" + name, seq, "+", qual]
        if rng.random() < noise:
            lines[0] = "@" + rng.choice(["other", "r%d/%d" % (i, 3 - mate), "", "/1", "x y", "x\ty", "r%d/1/2" % i])
        if rng.random() < noise:
            lines[1] = rng.choice([seq + "A", seq[:-1], "AGATCGGAAGAGC" + seq, seq + "G" * 10, seq.lower(), "A\tC", ""])
        if rng.random() < noise:
            lines[2] = rng.choice(["+" + name, "", "-", "+\r", "\t+"])
        if rng.random() < noise:
            lines[3] = rng.choice([qual + "I", qual[1:], " " + qual, "\x7f" * length, "\xff" * length, "K" * length, ""])
        if rng.random() < noise:
            lines[0] = rng.choice([name, ">" + name, "\xef\xbb\xbf@" + name])
        if rng.random() < noise / 2:
            lines = lines[:rng.randint(0, 3)]                       # a record that lost lines
        if rng.random() < noise / 2:
            lines.insert(rng.randint(0, len(lines)), "")            # an empty line
        if rng.random() < noise / 2:
            lines = [text + "\r" for text in lines]
        out.append(lines)
    return out


def _noisy_file(rng: random.Random, records) -> bytes:
    data = "".join(text + "\n" for lines in records for text in lines).encode("latin-1")
    roll = rng.random()
    if roll < 0.15:
        data = data[:-1]                                            # no final line end
    elif roll < 0.3:
        data += b"\n" * rng.randint(1, 5)                           # empty lines at the end
    elif roll < 0.4 and data:
        data = data[:rng.randint(0, len(data))]                     # cut anywhere
    roll = rng.random()
    if roll < 0.35:
        return gz(data) if data else data
    if roll < 0.5 and len(data) > 4:
        cut = rng.randint(1, len(data) - 1)
        return gz(data[:cut]) + gz(data[cut:])
    return data


def _same_as_model(result, expected) -> None:
    stats = result.stats
    assert stats.ran
    got = dict(pairs=stats.pairs, bad=stats.malformed, badlen=stats.length_mismatches, badrec=stats.first_bad_pair,
               mism=stats.name_mismatches, bases=stats.bases, maxlen=stats.max_length,
               meanlen="%.1f" % stats.mean_length, adapt=stats.adapter_fraction, polyg=stats.polyg_fraction,
               nfrac=stats.n_fraction, qmin=stats.quality_codes[0], qmax=stats.quality_codes[1],
               qmin2=stats.quality_codes[2], qmax2=stats.quality_codes[3], short1=stats.short_read1,
               short2=stats.short_read2,
               firstm=readcheck.encode_text(stats.first_mismatch) if stats.name_mismatches else b"-")
    assert got == expected


@pytest.mark.parametrize("seed", range(40))
def test_block_code_row_code_and_the_model_agree_on_noisy_input(tmp_path, monkeypatch, seed):
    """Random pairs with every kind of damage, read in tiny blocks and with a tiny sample."""
    rng = random.Random(seed)
    for round_number in range(8):
        n = rng.choice((0, 1, 2, 3, 5, 17, 60))
        style = rng.randrange(4)
        noise = rng.choice((0.0, 0.0, 0.02, 0.15))
        one = _noisy_records(rng, n, 1, style, noise)
        two = _noisy_records(rng, max(0, n - rng.choice((0, 0, 0, 1, 3))), 2, style, noise)
        if rng.random() < 0.15:
            one, two = two, one
        a = put(tmp_path, "f_1.fq", _noisy_file(rng, one))
        b = put(tmp_path, "f_2.fq", _noisy_file(rng, two))
        if os.path.getsize(a) == 0 or os.path.getsize(b) == 0:
            continue
        sample = rng.choice((200000, 200000, 1, 4, 9, 33))
        monkeypatch.setattr(readcheck, "SAMPLE_ROWS", sample)
        monkeypatch.setattr(MODEL, "SAMPLE_ROWS", sample)
        for name in ("_IN_BLOCK", "_PLAIN_BLOCK", "_OUT_BLOCK"):
            monkeypatch.setattr(readcheck, name, rng.choice((1, 2, 3, 7, 64, 255, 4096, 1 << 20)))
        fast = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2")
        slow = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2", _fast=False)
        assert fast.data == slow.data and fast.stats == slow.stats, "seed %d round %d" % (seed, round_number)
        _same_as_model(fast, MODEL.read_stats(a, b))
        # the check before trimming decides as the file, gzip, format and pairing lines do
        failed = any(entry.verdict == "FAIL" and entry.name in ("file", "gzip", "format", "pairing")
                     for entry in fast.lines)
        assert check_input_pair(a, b).ok == (not failed)


def _lines_of(records):
    return [[b"@" + name.encode(), seq.encode(), b"+", b"I" * len(seq)] for name, seq in records]


def _join(records, final_line_end=True) -> bytes:
    data = b"".join(text + b"\n" for lines in records for text in lines)
    return data if final_line_end else data[:-1]


@pytest.mark.parametrize("which, odd, malformed", [
    (0, b"", True), (0, b">r", True), (0, b"?r", True), (0, b"Ar", True), (0, b"r", True), (0, b" @r", True),
    (0, b"\x00@r", True), (0, b"\xff", True), (0, b"@", False), (0, b"@@", False),
    (2, b"", True), (2, b"*", True), (2, b",", True), (2, b"-", True), (2, b" +", True), (2, b"\xff+", True),
    (2, b"+r", False), (2, b"++", False), (2, b"+ ", False),
])
@pytest.mark.parametrize("mate", [1, 2])
def test_one_odd_line_among_good_rows(tmp_path, which, odd, malformed, mate):
    """The block code must notice a single header or third line that starts with the wrong byte,
    whether that byte sorts before or after the right one, wherever the row is in the block."""
    one, two = simple_pair(9, 12)
    for position in (0, 4, 8):
        files = [_lines_of(one), _lines_of(two)]
        files[mate - 1][position][which] = odd
        a, b = put(tmp_path, "o_1.fq", _join(files[0])), put(tmp_path, "o_2.fq", _join(files[1]))
        fast = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2")
        slow = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2", _fast=False)
        assert fast.data == slow.data and fast.stats == slow.stats
        assert fast.stats.pairs == 9
        if malformed:
            assert (fast.stats.malformed, fast.stats.first_malformed, fast.stats.sampled_pairs) == (1, position + 1, 8)
        else:
            assert (fast.stats.malformed, fast.stats.sampled_pairs) == (0, 9)
        _same_as_model(fast, MODEL.read_stats(a, b))


@pytest.mark.parametrize("mate", [1, 2])
@pytest.mark.parametrize("which", [0, 1, 2, 3])
@pytest.mark.parametrize("record, final_line_end", [(2, True), (5, True), (5, False)])
def test_a_tab_is_noticed_wherever_the_block_boundaries_fall(tmp_path, monkeypatch, mate, which, record, final_line_end):
    """One TAB in one line of one record, read in blocks of every size from 1 byte up."""
    one, two = simple_pair(6, 9)
    files = [_lines_of(one), _lines_of(two)]
    text = files[mate - 1][record][which]
    files[mate - 1][record][which] = text[:2] + b"\t" + text[2:]
    a = put(tmp_path, "t_1.fq", _join(files[0], final_line_end))
    b = put(tmp_path, "t_2.fq", _join(files[1], final_line_end))
    expected = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2", _fast=False)
    # the fields move: the row is malformed, or (TAB after the "+" or in the quality of the second
    # file, where nothing follows that could move) its quality is cut short
    assert expected.stats.malformed + expected.stats.length_mismatches == 1
    assert expected.stats.malformed == (0 if mate == 2 and which >= 2 else 1)
    assert expected.stats.first_bad_pair == record + 1
    _same_as_model(expected, MODEL.read_stats(a, b))
    for size in list(range(1, 60)) + [64, 97, 128, 200]:
        monkeypatch.setattr(readcheck, "_PLAIN_BLOCK", size)
        found = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2")
        assert found.data == expected.data and found.stats == expected.stats, "blocks of %d bytes" % size


@pytest.mark.parametrize("mate", [1, 2])
def test_a_tab_far_down_a_file_that_was_read_ahead(tmp_path, monkeypatch, mate):
    """One file arrives whole, the other in crumbs: the TAB waits in memory for many rounds."""
    monkeypatch.setattr(readcheck, "_PLAIN_BLOCK", 1 << 20)
    monkeypatch.setattr(readcheck, "_IN_BLOCK", 16)
    monkeypatch.setattr(readcheck, "_OUT_BLOCK", 48)
    one, two = simple_pair(40, 9)
    files = [_lines_of(one), _lines_of(two)]
    files[mate - 1][31][0] = files[mate - 1][31][0] + b"\tBC:Z:ACGT"
    whole, crumbs = _join(files[mate - 1]), gz(_join(files[2 - mate]))
    a = put(tmp_path, "f_1.fq", whole if mate == 1 else crumbs)
    b = put(tmp_path, "f_2.fq", crumbs if mate == 1 else whole)
    fast = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2")
    slow = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2", _fast=False)
    assert fast.data == slow.data and fast.stats == slow.stats
    assert (fast.stats.malformed, fast.stats.first_malformed, fast.stats.sampled_pairs) == (1, 32, 39)


def test_one_file_ends_while_the_other_is_still_arriving(tmp_path, monkeypatch):
    """Rows of the longer file that are not complete yet must wait for their last lines."""
    one, two = simple_pair(30, 9)
    for blocks in ((7, 1 << 20), (1 << 20, 7), (5, 11), (33, 3)):
        monkeypatch.setattr(readcheck, "_PLAIN_BLOCK", blocks[0])
        monkeypatch.setattr(readcheck, "_IN_BLOCK", blocks[1])
        monkeypatch.setattr(readcheck, "_OUT_BLOCK", blocks[1])
        for short, long_ in ((fastq(one[:3]), gz(fastq(two))), (gz(fastq(one[:3])), fastq(two)),
                             (fastq(one), gz(fastq(two[:1]))), (gz(fastq(one)), fastq(two[:1]) + b"@x\nAC")):
            a, b = put(tmp_path, "e_1.fq", short), put(tmp_path, "e_2.fq", long_)
            fast = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2")
            slow = check_reads(a, b, wording="upstream", shown_read1="1", shown_read2="2", _fast=False)
            assert fast.data == slow.data and fast.stats == slow.stats
            _same_as_model(fast, MODEL.read_stats(a, b))
            assert fast.stats.short_read1 + fast.stats.short_read2 >= 26


def test_a_very_long_line_without_a_line_end(tmp_path, monkeypatch):
    monkeypatch.setattr(readcheck, "_PLAIN_BLOCK", 1000)
    monkeypatch.setattr(readcheck, "_IN_BLOCK", 100)
    blob = b"@" + b"A" * 300000                                    # one line, read in 300 pieces
    a, b = put(tmp_path, "l_1.fq", blob + b"\tB"), put(tmp_path, "l_2.fq.gz", gz(blob))
    result = check_reads(a, b, wording="upstream")
    assert line(result, "format").message == "1 malformed records (first at pair 1); is this FASTQ?"
    _same_as_model(result, MODEL.read_stats(a, b))
    one, two = simple_pair(3)
    c = put(tmp_path, "l_3.fq", fastq(one) + blob)
    d = put(tmp_path, "l_4.fq", fastq(two) + blob + b"\n" + b"C" * 5000 + b"\tx")
    result = check_reads(c, d, wording="upstream")
    assert result.stats.pairs == 4 and result.stats.sampled_pairs == 3
    _same_as_model(result, MODEL.read_stats(c, d))


def test_utf16_and_byte_order_mark_are_read_as_bytes(tmp_path):
    one, two = simple_pair(5)
    bom = check(tmp_path, b"\xef\xbb\xbf" + fastq(one), b"\xef\xbb\xbf" + fastq(two))
    assert line(bom, "format").message == "1 malformed records (first at pair 1); is this FASTQ?"
    wide1 = b"\xff\xfe" + fastq(one, b"\r\n").decode("ascii").encode("utf-16-le")
    wide2 = b"\xff\xfe" + fastq(two, b"\r\n").decode("ascii").encode("utf-16-le")
    wide = check(tmp_path, wide1, wide2)
    assert line(wide, "format").verdict == "FAIL" and wide.stats.sampled_pairs == 0
    a, b = put(tmp_path, "w_1.fq", wide1), put(tmp_path, "w_2.fq", wide2)
    _same_as_model(wide, MODEL.read_stats(a, b))


# --------------------------------------------------------------------------- the check before trimming

_JARGON = ("mate", "phred", "k-mer", "kmer", "gzip", "fastp", "cutadapt", "seqtk", "stdin", "errno", "exception",
           "traceback", "-1 file", "-2 file", "preflight")


def _plain(result) -> None:
    for problem in result.problems:
        assert problem.code and problem.message and problem.next_step
        for text in (problem.message, problem.next_step):
            assert text[0].isupper() or text[0].isdigit() or text[0] == '"', text
            assert text.rstrip().endswith("."), text
            assert "\r" not in text and "\n" not in text and "\t" not in text
            for word in _JARGON:
                assert word not in text.lower(), "%r in %r" % (word, text)


def test_input_pair_that_is_fine(tmp_path):
    one, two = simple_pair(12)
    result = check_input_pair(put(tmp_path, "KP_R1.fastq.gz", gz(fastq(one))), put(tmp_path, "KP_R2.fastq.gz", gz(fastq(two))))
    assert result.ok and result.problems == [] and result.message == "" and result.next_step == ""
    assert result.stats.pairs == 12 and result.stats.ran


def test_input_pair_does_not_judge_quality_depth_or_adapters(tmp_path):
    """Those belong to the reads that will be assembled, after trimming."""
    one = [("r%d/1" % i, "AGATCGGAAGAGC" + "N" * 7 + "G" * 10, "h" * 30) for i in range(5)]
    two = [("r%d/2" % i, "G" * 30, " " * 30) for i in range(5)]
    assert check_input_pair(put(tmp_path, "a_1.fq", fastq(one)), put(tmp_path, "a_2.fq", fastq(two))).ok


def test_input_pair_file_problems(tmp_path):
    one, two = simple_pair(12)
    good = put(tmp_path, "S1_R1.fastq.gz", gz(fastq(one)))
    result = check_input_pair(good, str(tmp_path / "S1_R2.fastq.gz"))
    assert [p.code for p in result.problems] == ["unreadable"] and not result.ok
    assert result.message == 'The file "S1_R2.fastq.gz" could not be opened, so this sample cannot be processed.'
    assert result.problems[0].file == str(tmp_path / "S1_R2.fastq.gz")
    _plain(result)
    result = check_input_pair(good, put(tmp_path, "S1_R2.fastq.gz", b""))
    assert [p.code for p in result.problems] == ["empty"]
    assert result.message == 'The file "S1_R2.fastq.gz" is empty (0 bytes): it holds no reads.'
    _plain(result)
    result = check_input_pair(good, put(tmp_path, "S1_R2.fastq.gz", gz(fastq(two))[:-9]))
    assert [p.code for p in result.problems] == ["damaged"]
    assert result.message.startswith('The file "S1_R2.fastq.gz" is incomplete or damaged')
    assert "again" in result.next_step
    _plain(result)
    result = check_input_pair(good, good)
    assert [p.code for p in result.problems] == ["same_file"]
    _plain(result)
    result = check_input_pair(str(tmp_path), put(tmp_path, "S1_R2.fastq.gz", b""))
    assert [p.code for p in result.problems] == ["unreadable", "empty"]
    result = check_input_pair(put(tmp_path, "n_1.fq.gz", gz(b"")), put(tmp_path, "n_2.fq.gz", gz(b"")))
    assert [p.code for p in result.problems] == ["no_reads"]
    assert result.message == 'No reads were found in "n_1.fq.gz" and "n_2.fq.gz".'
    _plain(result)


def test_input_pair_record_problems(tmp_path):
    one, two = simple_pair(12)

    def problems(data1, data2):
        result = check_input_pair(put(tmp_path, "KP_R1.fastq", data1), put(tmp_path, "KP_R2.fastq", data2))
        _plain(result)
        assert not result.ok
        return [(p.code, p.message) for p in result.problems]

    assert problems(fastq(one), fastq(two[:-3])) == [(
        "fewer_reads", '"KP_R2.fastq" holds 3 reads fewer than "KP_R1.fastq". The two files of a sample must hold the '
                       "same reads: one of them is incomplete, or they do not belong together.")]
    assert problems(fastq(one[:-1]), fastq(two))[0][1].startswith('"KP_R1.fastq" holds 1 read fewer than "KP_R2.fastq".')
    text = b"lorem ipsum dolor\n" * 9
    found = problems(text, text)
    assert found[0][0] == "not_fastq" and found[0][1].startswith("3 read pairs are not in FASTQ format (the first is pair number 1).")
    bad = list(one)
    bad[4] = (bad[4][0], bad[4][1], "I" * 5)
    assert problems(fastq(bad), fastq(two)) == [(
        "length_mismatch", "1 read pair has a different number of bases and of quality values (the first is pair "
                           "number 5). The files are damaged.")]
    # each kind of damage is reported with its own first pair (the script has one number for both)
    data = fastq(bad[:6]) + b"r6/1 no at-sign\n" + fastq(bad[6:7]).split(b"\n", 1)[1] + fastq(bad[7:])
    assert [(code, text.split(".")[0]) for code, text in problems(data, fastq(two))] == [
        ("not_fastq", "1 read pair is not in FASTQ format (the first is pair number 7)"),
        ("length_mismatch", "1 read pair has a different number of bases and of quality values (the first is pair "
                            "number 5)")]
    dotted1 = [("SRR9.%d.1" % i, seq) for i, (_n, seq) in enumerate(one)]
    dotted2 = [("SRR9.%d.2" % i, seq) for i, (_n, seq) in enumerate(two)]
    assert problems(fastq(dotted1), fastq(dotted2)) == [(
        "names_differ", '12 read pairs have different names in the two files (the first is "SRR9.0.1"). The files '
                        "are not in the same order, or they do not belong to the same sample.")]
    shuffled = two[1:] + two[:1]
    assert problems(fastq(one), fastq(shuffled))[0][0] == "names_differ"
    # Windows line ends are named as what they are; a name is never shown with a control character in it
    assert problems(fastq(one, b"\r\n"), fastq(two, b"\r\n")) == [(
        "line_ends", "The read files have Windows line ends, which read files must not have (they may have been "
                     "opened and saved in a text editor). Because of them 12 read pairs cannot be matched.")]
    odd = [(name.replace("/1", "\x07\x1b[31m/1"), seq) for name, seq in one]
    assert '(the first is "r0??[31m")' in problems(fastq(odd), fastq(two))[0][1]


def test_input_pair_says_only_what_is_true_about_empty_lines(tmp_path):
    """The script blames the second file whichever file has the empty line; the screen must not."""
    one, two = simple_pair(6)

    def problems(data1, data2):
        result = check_input_pair(put(tmp_path, "KP_R1.fastq", data1), put(tmp_path, "KP_R2.fastq", data2))
        _plain(result)
        assert not result.ok
        return [(p.code, os.path.basename(p.file)) for p in result.problems]

    assert problems(fastq(one) + b"\n", fastq(two)) == [("blank_lines", "KP_R1.fastq")]
    assert problems(fastq(one), fastq(two) + b"\n") == [("blank_lines", "KP_R2.fastq")]
    assert problems(fastq(one) + b"\n", fastq(two) + b"\n") == [("blank_lines", "KP_R1.fastq"), ("blank_lines", "KP_R2.fastq")]
    assert problems(fastq(one) + b"\n", fastq(two[:-2])) == [("fewer_reads", ""), ("blank_lines", "KP_R1.fastq")]
    result = check_input_pair(put(tmp_path, "KP_R1.fastq", fastq(one) + b"\n"), put(tmp_path, "KP_R2.fastq", fastq(two[:-2])))
    assert result.message.startswith('"KP_R2.fastq" holds 2 reads fewer than "KP_R1.fastq".')
    # the second file ends inside a record after the first has run out: it is the first that is short
    longer = fastq(two) + b"@extra/2\nACGT\n+\n"
    result = check_input_pair(put(tmp_path, "KP_R1.fastq", fastq(one)), put(tmp_path, "KP_R2.fastq", longer))
    assert result.stats.short_read2 == 1, "the script counts this row against the second file"
    assert result.message.startswith('"KP_R1.fastq" holds 1 read fewer than "KP_R2.fastq".')


@pytest.mark.parity
def test_input_pair_decides_like_the_read_check_on_every_parity_fixture():
    H.ensure_fixtures()
    seen = 0
    for case in H.load_cases():
        if case.kind != "check" or case.genome or case.name.startswith("_args"):
            continue
        read1, read2 = os.path.join(H.HERE, case.read1), os.path.join(H.HERE, case.read2)
        if not (os.path.exists(read1) and os.path.exists(read2)):
            continue
        golden = case.golden("stdout.txt").decode("utf-8", "replace").split("\n")
        failed = any(text[2:6] == "FAIL" and text[9:31].strip() in ("file", "gzip", "format", "pairing")
                     for text in golden)
        result = check_input_pair(read1, read2)
        assert result.ok == (not failed), case.name
        _plain(result)
        seen += 1
    assert seen >= 55


# --------------------------------------------------------------------------- speed

def _big_pair(folder, pairs: int):
    rng = random.Random(5)
    bases = bytes(b"ACGT"[i & 3] for i in range(256))
    quals = bytes(b"FFFFFFFFFFFFFFFFFFFFFFFF::::,,#"[i % 31] for i in range(256))
    paths = [os.path.join(str(folder), "big_R%d.fastq.gz" % mate) for mate in (1, 2)]
    with gzip.open(paths[0], "wb", compresslevel=1) as out1, gzip.open(paths[1], "wb", compresslevel=1) as out2:
        for start in range(0, pairs, 20000):
            n = min(20000, pairs - start)
            seq, qual = rng.randbytes(n * 300).translate(bases), rng.randbytes(n * 300).translate(quals)
            block1, block2 = [], []
            for i in range(n):
                name = b"@M01234:55:000000000-ABCDE:1:1101:%d:%d " % (start + i, (start + i) * 7 % 30000)
                at = i * 300
                block1.append(name + b"1:N:0:ACGT\n" + seq[at:at + 150] + b"\n+\n" + qual[at:at + 150] + b"\n")
                block2.append(name + b"2:N:0:ACGT\n" + seq[at + 150:at + 300] + b"\n+\n" + qual[at + 150:at + 300] + b"\n")
            out1.write(b"".join(block1))
            out2.write(b"".join(block2))
    return paths


@pytest.mark.slow
def test_speed_on_200000_pairs_of_2x150(tmp_path):
    """A fifth of the target set (one million pairs in well under 30 seconds on 4 cores).

    Measured on the development machine, limited to 4 cores: one million pairs in 1.7 s,
    so 200,000 take about a third of a second. The limit here is the target itself scaled
    down (6 s), which leaves room for a slow or busy test machine.
    """
    a, b = _big_pair(tmp_path, 200000)
    started = time.perf_counter()
    result = check_reads(a, b, wording="upstream")
    seconds = time.perf_counter() - started
    assert result.fails == 0
    assert (result.stats.pairs, result.stats.bases, result.stats.max_length) == (200000, 60000000, 150)
    assert line(result, "depth").message.startswith("about 12x for a 5 Mb genome")
    assert seconds < 6.0, "the read check took %.1f s for 200,000 pairs" % seconds
