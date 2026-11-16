# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""pairing: finding read files, pairing, lanes, names, peeks, the list, disk and paths.

Everything runs on small files under pytest's ``tmp_path``. No engine is started.
"""

from __future__ import annotations

import builtins
import gzip
import ntpath
import os
import random
import types

import pytest

from tesseract_win import branding, fsutil, pairing
from tesseract_win.model import Sample, SampleState
from tesseract_win.pairing import PeekStatus

GB = 1024 ** 3


# --------------------------------------------------------------------------- helpers

def fastq(reads=4, length=60, mate=1, name="M01:7:000-AB:1:1101:%d:1000", crlf=False, start=0) -> bytes:
    end = "\r\n" if crlf else "\n"
    out = []
    for i in range(start, start + reads):
        bases = "ACGT" * (length // 4) + "A" * (length % 4)
        out.append("@%s %d:N:0:1%s%s%s+%s%s%s" % (name % i, mate, end, bases, end, end, "I" * length, end))
    return "".join(out).encode("ascii")


_BASES = bytes(b"ACGT"[i & 3] for i in range(256))


def random_fastq(reads, length=100, seed=1) -> bytes:
    """Reads with random bases: they do not compress away, so a .gz of them has a real size."""
    rng = random.Random(seed)
    out = []
    for i in range(reads):
        bases = rng.randbytes(length).translate(_BASES)
        out.append(b"@M01:7:000-AB:1:1101:%d:1000 1:N:0:1\n%s\n+\n%s\n" % (i, bases, b"I" * length))
    return b"".join(out)


def put(path, data=None, mate=1, compress=None) -> str:
    """Write a read file. Compressed when the name ends in .gz, unless ``compress`` says otherwise."""
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if data is None:
        data = fastq(mate=mate)
    if compress is None:
        compress = path.lower().endswith(".gz")
    with open(path, "wb") as handle:
        handle.write(gzip.compress(data, mtime=0) if compress else data)
    return path


def pair(folder, name, first="_R1.fastq.gz", second="_R2.fastq.gz"):
    return put(os.path.join(str(folder), name + first), mate=1), put(os.path.join(str(folder), name + second), mate=2)


def by_name(result):
    samples = result.samples if hasattr(result, "samples") else result
    names = [s.name for s in samples]
    assert len(set(names)) == len(names), names
    return {s.name: s for s in samples}


def base(path):
    return os.path.basename(path)


# --------------------------------------------------------------------------- naming forms

@pytest.mark.parametrize("first, second", [
    ("KP_001_R1.fastq.gz", "KP_001_R2.fastq.gz"),
    ("KP_001_1.fastq.gz", "KP_001_2.fastq.gz"),
    ("KP_001.1.fastq.gz", "KP_001.2.fastq.gz"),
    ("KP_001.R1.fastq.gz", "KP_001.R2.fastq.gz"),
    ("KP_001_S12_L001_R1_001.fastq.gz", "KP_001_S12_L001_R2_001.fastq.gz"),
    ("KP_001_S12_R1_001.fastq.gz", "KP_001_S12_R2_001.fastq.gz"),        # --no-lane-splitting
    ("KP_001_L003_R1_001.fastq.gz", "KP_001_L003_R2_001.fastq.gz"),      # older, without _S<n>
    ("KP_001_R1_001.fastq.gz", "KP_001_R2_001.fastq.gz"),
    ("KP_001_r1.fq", "KP_001_r2.fq"),
    ("KP_001_1.fq", "KP_001_2.fq"),
    ("KP_001_R1.fq.gz", "KP_001_R2.fq.gz"),
    ("KP_001_R1.fastq", "KP_001_R2.fastq"),
])
def test_every_naming_form_gives_one_ready_pair(tmp_path, first, second):
    a = put(tmp_path / first, mate=1)
    b = put(tmp_path / second, mate=2)
    result = pairing.scan([str(tmp_path)])
    assert [s.name for s in result.samples] == ["KP_001"]
    sample = result.samples[0]
    assert sample.lanes == [(a, b)]
    assert sample.state is SampleState.READY and sample.note == "" and sample.trim is True
    assert sample.size_bytes == os.path.getsize(a) + os.path.getsize(b)
    assert result.ignored == [] and result.fastq_files == 2


def test_the_read_marker_is_matched_at_the_end_only(tmp_path):
    pair(tmp_path, "A_R1_plate_2")                      # "_R1" and "_2" in the middle stay
    pair(tmp_path, "run.1.lib", "_1.fq.gz", "_2.fq.gz")
    pair(tmp_path, "X_S3_final", "_R1.fastq.gz", "_R2.fastq.gz")   # _S3 is kept: not the Illumina form
    assert sorted(by_name(pairing.scan(str(tmp_path)))) == ["A_R1_plate_2", "X_S3_final", "run.1.lib"]


def test_a_name_ending_in_a_number_is_not_a_read_marker(tmp_path):
    put(tmp_path / "KP_12.fastq.gz")
    put(tmp_path / "KP_22.fastq.gz")
    samples = by_name(pairing.scan(str(tmp_path)))
    assert sorted(samples) == ["KP_12", "KP_22"]
    assert all(s.state is SampleState.LEFT_OUT and s.note == pairing.NO_PARTNER for s in samples.values())


def test_forms_are_not_mixed(tmp_path):
    put(tmp_path / "A_1.fastq.gz")
    put(tmp_path / "A_R2.fastq.gz")
    result = pairing.scan(str(tmp_path))
    assert len(result.samples) == 2 and not result.ready
    assert sorted(s.note for s in result.samples) == sorted([pairing.NO_READ1, pairing.NO_READ2])


@pytest.mark.parametrize("first, second", [
    ("S1_R1.FASTQ.GZ", "S1_R2.FASTQ.GZ"),
    ("S1_R1.Fastq.Gz", "S1_R2.fastq.gz"),
    ("S1_R1.FQ", "S1_R2.fq"),
    ("S1_R1.Fq.GZ", "S1_R2.FASTQ.gz"),
    ("S1_1.FASTQ", "S1_2.Fastq"),
])
def test_letter_case_in_extensions(tmp_path, first, second):
    a, b = put(tmp_path / first), put(tmp_path / second, mate=2)
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["S1"]
    assert result.samples[0].lanes == [(a, b)] and result.samples[0].ready
    assert pairing.is_fastq_name(first) and pairing.is_fastq_name(second)


def test_what_is_and_is_not_a_fastq_name():
    for good in ("a.fastq", "a.fq", "a.fastq.gz", "a.fq.gz", "A.FQ.GZ", "/x/y/a b.fastq"):
        assert pairing.is_fastq_name(good), good
    for bad in ("a.fasta", "a.fa.gz", "a.fastq.bz2", "a.gz", "fastq", ".fastq", "a.fastq.gz.part", "a.txt"):
        assert not pairing.is_fastq_name(bad), bad
    assert set(pairing.FASTQ_EXTENSIONS) == {".fastq", ".fq", ".fastq.gz", ".fq.gz"}


# --------------------------------------------------------------------------- awkward names

def test_names_with_spaces_brackets_and_non_latin_letters(tmp_path):
    folder = tmp_path / "données séq [2026] (rerun)"
    a, b = pair(folder, "KP 001 [a]")
    c, d = pair(folder, "Клебсиелла_7", "_1.fq.gz", "_2.fq.gz")
    e, f = pair(folder, "肺炎桿菌[3]", "_S4_L001_R1_001.fastq.gz", "_S4_L001_R2_001.fastq.gz")
    g, h = pair(folder, "Ünïcödé-ñ", ".R1.fastq", ".R2.fastq")
    result = pairing.scan([str(folder)])
    samples = by_name(result)
    assert sorted(samples) == sorted(["KP_001_[a]", "Клебсиелла_7", "肺炎桿菌[3]", "Ünïcödé-ñ"])
    assert samples["KP_001_[a]"].lanes == [(a, b)]
    assert samples["Клебсиелла_7"].lanes == [(c, d)]
    assert samples["肺炎桿菌[3]"].lanes == [(e, f)]
    assert samples["Ünïcödé-ñ"].lanes == [(g, h)]
    assert all(s.ready for s in result.samples)
    # The one name that had to be cleaned says so; the others are plainly Ready.
    cleaned = samples["KP_001_[a]"]
    assert cleaned.state is SampleState.READY_NOTE
    assert cleaned.note == ("Name changed from 'KP 001 [a]' to 'KP_001_[a]' so that Windows and other "
                            "programs accept it.")
    assert samples["肺炎桿菌[3]"].state is SampleState.READY
    for sample in result.samples:
        assert sample.name == fsutil.safe_name(sample.name)


def test_the_same_files_given_as_files_instead_of_a_folder(tmp_path):
    folder = tmp_path / "a b [1]"
    a, b = pair(folder, "KP [x] é")
    result = pairing.scan([b, a])
    assert [s.lanes for s in result.samples] == [[(a, b)]]
    assert result.samples[0].name == "KP_[x]_é"


def test_names_windows_forbids_are_made_safe(tmp_path):
    pair(tmp_path, "CON")
    pair(tmp_path, "x" * 80)
    samples = by_name(pairing.scan(str(tmp_path)))
    assert "CON_" in samples and samples["CON_"].ready
    long_name = [n for n in samples if n.startswith("xxx")][0]
    assert len(long_name) == 60


def test_samples_and_ignored_come_back_in_natural_order(tmp_path):
    for number in (10, 2, 1, 33, 4):
        pair(tmp_path, "KP_%d" % number)
    pair(tmp_path, "kp_3")
    for number in (10, 9):
        put(tmp_path / ("X%d_S1_L001_I1_001.fastq.gz" % number))
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP_1", "KP_2", "kp_3", "KP_4", "KP_10", "KP_33"]
    assert [base(i.path) for i in result.ignored] == ["X9_S1_L001_I1_001.fastq.gz",
                                                     "X10_S1_L001_I1_001.fastq.gz"]


# --------------------------------------------------------------------------- lanes

def lane_files(folder, name, lanes, s=1, ext=".fastq.gz"):
    out = []
    for lane in lanes:
        out.append(tuple(put(os.path.join(str(folder), "%s_S%d_L%03d_R%d_001%s" % (name, s, lane, read, ext)),
                             mate=read) for read in (1, 2)))
    return out


def test_lanes_are_grouped_in_lane_order(tmp_path):
    files = lane_files(tmp_path, "KP_002", [2, 10, 1, 4, 3])
    pair(tmp_path, "SA_17", "_1.fq.gz", "_2.fq.gz")
    samples = by_name(pairing.scan(str(tmp_path)))
    assert sorted(samples) == ["KP_002", "SA_17"]
    sample = samples["KP_002"]
    assert [base(a) for a, _ in sample.lanes] == [
        "KP_002_S1_L%03d_R1_001.fastq.gz" % lane for lane in (1, 2, 3, 4, 10)]
    assert [base(b) for _, b in sample.lanes] == [
        "KP_002_S1_L%03d_R2_001.fastq.gz" % lane for lane in (1, 2, 3, 4, 10)]
    assert sample.read1.endswith("L001_R1_001.fastq.gz") and sample.read2.endswith("L001_R2_001.fastq.gz")
    assert sample.size_bytes == sum(os.path.getsize(p) for lane in files for p in lane)
    assert sample.state is SampleState.READY


def test_eight_lanes_are_one_sample(tmp_path):
    lane_files(tmp_path, "NOVA", range(1, 9))
    result = pairing.scan(str(tmp_path))
    assert len(result.samples) == 1 and len(result.samples[0].lanes) == 8 and result.samples[0].ready


def test_a_lane_with_one_file_leaves_the_sample_out(tmp_path):
    files = lane_files(tmp_path, "KP", [1, 2, 3])
    os.remove(files[2][1])                                   # lane 3, read 2
    sample = pairing.scan(str(tmp_path)).samples[0]
    assert len(sample.lanes) == 3 and sample.lanes[2] == (files[2][0], "")
    assert sample.state is SampleState.LEFT_OUT and sample.note == "Lane 3 has no Read 2 file."
    os.remove(files[0][0])
    os.rename(files[2][0], files[2][1])                      # now lane 1 lacks read 1, lane 3 read 1
    sample = pairing.scan(str(tmp_path)).samples[0]
    assert sample.note == "Lane 1 has no Read 1 file."


def test_lanes_that_mix_gzipped_and_plain_files_are_left_out(tmp_path):
    lane_files(tmp_path, "KP", [1, 2])
    lane_files(tmp_path, "KP", [3], ext=".fastq")
    result = pairing.scan(str(tmp_path))
    assert len(result.samples) == 1
    sample = result.samples[0]
    assert len(sample.lanes) == 3
    assert sample.state is SampleState.LEFT_OUT and sample.note == pairing.LANES_COMPRESSION


def test_different_sample_numbers_are_different_samples(tmp_path):
    lane_files(tmp_path, "KP", [1, 2], s=1)
    lane_files(tmp_path, "KP", [1, 2], s=2)
    result = pairing.scan(str(tmp_path))
    assert [(s.name, len(s.lanes)) for s in result.samples] == [("KP", 2), ("KP", 2)]
    marked = pairing.mark_name_clashes(result.samples)
    assert len(marked) == 2 and all(pairing.is_name_clash(s) for s in result.samples)


def test_loose_names_are_never_grouped_as_lanes(tmp_path):
    pair(tmp_path, "KP_L001")                                # KP_L001_R1.fastq.gz: not the strict form
    pair(tmp_path, "KP_L002")
    samples = by_name(pairing.scan(str(tmp_path)))
    assert sorted(samples) == ["KP_L001", "KP_L002"]
    assert all(len(s.lanes) == 1 for s in samples.values())


def test_a_file_without_lane_is_not_joined_to_the_lanes(tmp_path):
    lane_files(tmp_path, "KP", [1, 2])
    pair(tmp_path, "KP", "_S1_R1_001.fastq.gz", "_S1_R2_001.fastq.gz")       # the lanes, joined by hand
    result = pairing.scan(str(tmp_path))
    assert sorted(len(s.lanes) for s in result.samples) == [1, 2]
    assert [s.name for s in result.samples] == ["KP", "KP"]


def test_lanes_in_different_folders_are_different_samples(tmp_path):
    lane_files(tmp_path / "run1", "KP", [1])
    lane_files(tmp_path / "run2", "KP", [2])
    result = pairing.scan(str(tmp_path))
    assert [len(s.lanes) for s in result.samples] == [1, 1]


# --------------------------------------------------------------------------- left out by name

def test_a_read_file_with_no_partner(tmp_path):
    a = put(tmp_path / "EC_09_R1.fastq.gz")
    b = put(tmp_path / "EC_10_2.fq.gz")
    c = put(tmp_path / "lonely.fastq")
    samples = by_name(pairing.scan(str(tmp_path)))
    assert samples["EC_09"].lanes == [(a, "")] and samples["EC_09"].note == pairing.NO_READ2
    assert samples["EC_10"].lanes == [("", b)] and samples["EC_10"].note == pairing.NO_READ1
    assert samples["lonely"].lanes == [(c, "")] and samples["lonely"].note == pairing.NO_PARTNER
    for sample in samples.values():
        assert sample.state is SampleState.LEFT_OUT and not sample.ready
        assert pairing.has_no_partner(sample)
        assert sample.size_bytes > 0
    assert samples["EC_10"].read1 == "" and samples["EC_10"].read2 == b
    assert "Read 2" in pairing.NO_READ2 and "same folder" in pairing.NO_READ2


def test_partners_are_looked_for_in_the_same_folder_only(tmp_path):
    put(tmp_path / "one" / "KP_R1.fastq.gz")
    put(tmp_path / "two" / "KP_R2.fastq.gz")
    result = pairing.scan(str(tmp_path))
    assert len(result.samples) == 2 and not result.ready
    assert result.none_paired
    assert "2 files with no partner" in result.summary()


def test_a_pair_whose_files_differ_in_compression(tmp_path):
    a = put(tmp_path / "KP_R1.fastq.gz")
    b = put(tmp_path / "KP_R2.fastq")
    result = pairing.scan(str(tmp_path))
    assert len(result.samples) == 1
    sample = result.samples[0]
    assert sample.lanes == [(a, b)]
    assert sample.state is SampleState.LEFT_OUT and sample.note == pairing.PAIR_COMPRESSION


def test_a_compressed_and_a_plain_copy_are_two_samples(tmp_path):
    pair(tmp_path, "KP")
    pair(tmp_path, "KP", "_R1.fastq", "_R2.fastq")
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP", "KP"]
    kinds = sorted(tuple(base(p).split("_R1")[-1] for p in [s.read1]) for s in result.samples)
    assert kinds == [(".fastq",), (".fastq.gz",)]
    assert all(s.ready for s in result.samples)              # until the names are compared
    pairing.mark_name_clashes(result.samples)
    assert not any(s.ready for s in result.samples)


def test_the_same_file_in_both_roles(tmp_path):
    a = put(tmp_path / "KP_R1.fastq.gz")
    sample = Sample(name="KP", lanes=[(a, a)])
    assert pairing.assess_sample(sample) == (SampleState.LEFT_OUT, pairing.SAME_FILE)
    assert pairing.check_sample(sample).state is SampleState.LEFT_OUT
    # Two names for one file: a hard link, as a mistaken copy would look to the engines.
    b = str(tmp_path / "KP_R2.fastq.gz")
    try:
        os.link(a, b)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard links are not available here")
    result = pairing.scan(str(tmp_path))
    assert len(result.samples) == 1
    assert result.samples[0].state is SampleState.LEFT_OUT and result.samples[0].note == pairing.SAME_FILE
    assert pairing.check_sample(result.samples[0]).note == pairing.SAME_FILE


def test_undetermined_reads_are_found_but_left_out(tmp_path):
    lane_files(tmp_path, "Undetermined", [1, 2], s=0)
    pair(tmp_path, "KP_1")
    result = pairing.scan(str(tmp_path))
    samples = by_name(result)
    assert sorted(samples) == ["KP_1", "Undetermined"]
    undetermined = samples["Undetermined"]
    assert len(undetermined.lanes) == 2
    assert undetermined.state is SampleState.LEFT_OUT and undetermined.note == pairing.UNDETERMINED
    assert pairing.is_undetermined(undetermined) and not pairing.is_undetermined(samples["KP_1"])
    assert "could not assign" in pairing.UNDETERMINED
    assert "4 Undetermined files" in result.summary()
    # A sample that merely starts with the word is a sample.
    pair(tmp_path / "other", "Undetermined_growth")
    assert pairing.scan(str(tmp_path / "other")).samples[0].ready
    # Looking inside or renaming does not make it an isolate.
    assert pairing.check_sample(undetermined).state is SampleState.LEFT_OUT
    assert pairing.rename(result.samples, undetermined, "Mystery") == ""
    assert undetermined.state is SampleState.LEFT_OUT and undetermined.note == pairing.UNDETERMINED


def test_index_reads_are_ignored_with_a_reason(tmp_path):
    good = lane_files(tmp_path, "KP", [1])
    for read in ("I1", "I2"):
        put(tmp_path / ("KP_S1_L001_%s_001.fastq.gz" % read))
    put(tmp_path / "Undetermined_S0_L001_I1_001.fastq.gz")
    put(tmp_path / "old_I1.fastq.gz")
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP"]
    assert result.samples[0].lanes == good and result.samples[0].ready
    assert sorted(base(i.path) for i in result.ignored) == [
        "KP_S1_L001_I1_001.fastq.gz", "KP_S1_L001_I2_001.fastq.gz",
        "Undetermined_S0_L001_I1_001.fastq.gz", "old_I1.fastq.gz"]
    for item in result.ignored:
        assert item.kind == "index" and "index reads" in item.reason and base(item.path) in item.reason


# --------------------------------------------------------------------------- folders

def make_results_folder(root, sample="KP_1", finished=True):
    """A results folder as decision D1 lays it out, with read-like files inside."""
    root = str(root)
    r1, r2 = (os.path.join(root, sample, "trimming", "%s_trimmed_R%d.fastq.gz" % (sample, n)) for n in (1, 2))
    put(r1)
    put(r2, mate=2)
    put(os.path.join(root, sample, "trimming", "leftover_R1.fastq.gz"))
    put(os.path.join(root, sample, "trimming", "leftover_R2.fastq.gz"), mate=2)
    put(os.path.join(root, sample, "_in_progress", "joined", "%s_R1.fastq.gz" % sample))
    put(os.path.join(root, sample, "_in_progress", "joined", "%s_R2.fastq.gz" % sample), mate=2)
    put(os.path.join(root, "assemblies", "%s.fasta" % sample), b">c1\nACGT\n")
    if finished:
        put(os.path.join(root, sample, "sample_status.json"), b"{}\n")
    return r1, r2


def test_a_folder_that_contains_a_results_folder(tmp_path):
    a, b = pair(tmp_path, "KP_1")
    c, d = pair(tmp_path / "more", "KP_2")
    results = tmp_path / branding.RESULTS_DIR_NAME
    make_results_folder(results)
    result = pairing.scan(str(tmp_path))
    assert [(s.name, s.lanes) for s in result.samples] == [("KP_1", [(a, b)]), ("KP_2", [(c, d)])]
    assert [(i.kind, i.path) for i in result.ignored] == [("results_folder", str(results))]
    assert branding.RESULTS_DIR_NAME in result.ignored[0].reason
    assert "1 results folder" in result.summary()


def test_a_results_folder_under_another_name_is_known_by_its_status_files(tmp_path):
    pair(tmp_path, "KP_9")
    make_results_folder(tmp_path / "my output")
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP_9"]
    assert [i.kind for i in result.ignored] == ["results_folder"]


def test_a_results_folder_with_no_finished_sample_is_known_by_last_run_json(tmp_path):
    pair(tmp_path, "KP_9")
    make_results_folder(tmp_path / "out", finished=False)
    put(tmp_path / "out" / "last_run.json", b"{}\n")
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP_9"]


def test_a_results_folder_given_itself_is_not_searched(tmp_path):
    results = tmp_path / "Results"
    make_results_folder(results)
    result = pairing.scan([str(results)])
    assert result.samples == [] and [i.kind for i in result.ignored] == ["results_folder"]
    # Nor one of its sample folders.
    result = pairing.scan([str(results / "KP_1")])
    assert result.samples == [] and [i.kind for i in result.ignored] == ["results_folder"]


def test_work_folders_of_a_crashed_first_run_are_not_searched(tmp_path):
    pair(tmp_path, "KP_9")
    make_results_folder(tmp_path / "odd", finished=False)       # no status file, no last_run.json
    os.remove(str(tmp_path / "odd" / "KP_1" / "trimming" / "leftover_R1.fastq.gz"))
    os.remove(str(tmp_path / "odd" / "KP_1" / "trimming" / "leftover_R2.fastq.gz"))
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP_9"]         # not the joined or trimmed reads
    assert sorted(i.kind for i in result.ignored) == ["own_trimmed", "own_trimmed"]


def test_own_trimmed_reads_are_taken_only_when_given_as_files(tmp_path):
    a, b = pair(tmp_path, "KP_1")
    t1, t2 = pair(tmp_path / "copies", "KP_1", "_trimmed_R1.fastq.gz", "_trimmed_R2.fastq.gz")
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP_1"]
    assert sorted(i.path for i in result.ignored) == [t1, t2]
    assert all(i.kind == "own_trimmed" and branding.APP_NAME in i.reason for i in result.ignored)

    result = pairing.scan([t1, t2])
    assert result.ignored == []
    sample = result.samples[0]
    assert sample.name == "KP_1_trimmed" and sample.lanes == [(t1, t2)]
    assert sample.trim is False and sample.state is SampleState.READY_NOTE
    assert sample.note == pairing.ALREADY_TRIMMED and branding.APP_NAME in sample.note

    # One of the two given: the other comes along. A folder and the files together: both.
    assert pairing.scan([t2]).samples[0].lanes == [(t1, t2)]
    both = pairing.scan([str(tmp_path), t1])
    assert sorted(s.name for s in both.samples) == ["KP_1", "KP_1_trimmed"] and both.ignored == []
    # Even from inside a results folder, when the user points at the files.
    r1, r2 = make_results_folder(tmp_path / branding.RESULTS_DIR_NAME, sample="KP_7")
    inside = pairing.scan([r1, r2])
    assert [(s.name, s.lanes, s.trim) for s in inside.samples] == [("KP_7_trimmed", [(r1, r2)], False)]


def test_hidden_folders_and_links_to_folders_are_not_searched(tmp_path):
    pair(tmp_path / "data", "KP_1")
    pair(tmp_path / "data" / ".snapshot", "KP_2")
    pair(tmp_path / "data" / "$RECYCLE.BIN", "KP_3")
    try:
        os.symlink(str(tmp_path / "data"), str(tmp_path / "data" / "loop"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP_1"]


class FakeEntry:
    """A folder entry as Windows reports it, for the checks that need its attributes."""

    def __init__(self, name, attributes=0x10, reparse_tag=0, junction=None):
        self.name, self.path = name, "C:\\data\\" + name
        self._info = types.SimpleNamespace(st_file_attributes=attributes, st_reparse_tag=reparse_tag)
        if junction is not None:
            self.is_junction = lambda: junction

    def is_dir(self, follow_symlinks=True):
        return True

    def stat(self, follow_symlinks=True):
        return self._info


def test_windows_links_and_hidden_folders_by_their_attributes():
    assert pairing._real_folder(FakeEntry("reads"))
    assert pairing._real_folder(FakeEntry("OneDrive", attributes=0x410, reparse_tag=0x9000001A))   # cloud folder
    assert not pairing._real_folder(FakeEntry("junction", attributes=0x410, reparse_tag=0xA0000003))
    assert not pairing._real_folder(FakeEntry("symlink", attributes=0x410, reparse_tag=0xA000000C))
    assert not pairing._real_folder(FakeEntry("junction312", junction=True))
    assert pairing._real_folder(FakeEntry("plain312", junction=False))
    assert not pairing._hidden(FakeEntry("reads"))
    assert pairing._hidden(FakeEntry("AppData", attributes=0x12))                 # hidden
    assert pairing._hidden(FakeEntry("System Volume Information", attributes=0x16))
    assert pairing._hidden(FakeEntry("Recovery", attributes=0x14))                # system
    for name in (".git", "$RECYCLE.BIN", "_in_progress", "last_failed_attempt", "new_result"):
        assert pairing._hidden(FakeEntry(name)), name


def test_other_files_are_counted_not_listed(tmp_path):
    pair(tmp_path, "KP_1")
    for name in ("notes.txt", "run.xml", "KP_1.fasta", "archive.zip", "x.bam"):
        put(tmp_path / "sub" / name, b"x")
    put(tmp_path / "old_R1.fastq.bz2", b"BZh9")
    result = pairing.scan(str(tmp_path))
    assert result.other_files == 5 and result.fastq_files == 2
    assert [(i.kind, base(i.path)) for i in result.ignored] == [("compression", "old_R1.fastq.bz2")]
    assert result.summary() == "Found 1 sample (2 files). Not added: 6 other files."


def test_files_chosen_on_purpose_that_cannot_be_used(tmp_path):
    chosen = {
        "report.pdf": ("not_fastq", "report.pdf is not a FASTQ file. FASTQ files end in .fastq, .fq, "
                                    ".fastq.gz or .fq.gz."),
        "runs.zip": ("archive", "runs.zip is an archive. Extract it first, then add the FASTQ files "
                                "that are inside."),
        "reads.tar.gz": ("archive", None),
        "KP_1.fastq.bz2": ("compression", "KP_1.fastq.bz2: this kind of compression is not supported. "
                                          "Convert the file to .fastq.gz first."),
        "KP_2_R1.fq.xz": ("compression", None),
        "KP_3_R1.fastq.gz.crdownload": ("partial_download", None),
        "KP_4.bam": ("not_fastq", None),
        "KP_5.fasta": ("not_fastq", None),
    }
    paths = [put(tmp_path / name, b"x") for name in chosen]
    gone = str(tmp_path / "gone_R1.fastq.gz")
    result = pairing.scan(paths + [gone])
    assert result.samples == []
    found = {base(i.path): i for i in result.ignored}
    assert set(found) == set(chosen) | {"gone_R1.fastq.gz"}
    for name, (kind, reason) in chosen.items():
        assert found[name].kind == kind, name
        assert found[name].reason.startswith(name)
        if reason:
            assert found[name].reason == reason
    assert found["gone_R1.fastq.gz"].kind == "missing"
    assert found["gone_R1.fastq.gz"].reason == "gone_R1.fastq.gz was not found."


def test_one_file_given_brings_its_partner(tmp_path):
    a, b = pair(tmp_path, "KP_1")
    lanes = lane_files(tmp_path, "NS", [1, 2])
    assert pairing.scan([a]).samples[0].lanes == [(a, b)]
    assert pairing.scan([b]).samples[0].lanes == [(a, b)]
    # Only the partner of the lane that was chosen, not the other lanes.
    one = pairing.scan([lanes[1][1]])
    assert one.samples[0].lanes == [lanes[1]] and one.samples[0].ready
    # Both lanes chosen by their Read 1 only.
    two = pairing.scan([lanes[1][0], lanes[0][0]])
    assert two.samples[0].lanes == lanes
    # No partner on the disk: left out.
    lone = put(tmp_path / "solo" / "Q_R1.fastq.gz")
    assert pairing.scan([lone]).samples[0].note == pairing.NO_READ2


def test_the_same_file_given_twice_is_one_file(tmp_path):
    a, b = pair(tmp_path, "KP_1")
    result = pairing.scan([a, b, a, str(tmp_path), os.path.join(str(tmp_path), ".", "KP_1_R2.fastq.gz")])
    assert len(result.samples) == 1 and result.fastq_files == 2


def test_an_empty_path_is_not_the_working_folder(tmp_path, monkeypatch):
    pair(tmp_path, "KP_1")
    monkeypatch.chdir(tmp_path)
    assert pairing.scan([""]).samples == [] and pairing.scan([]).samples == []
    assert len(pairing.scan(["."]).samples) == 1              # asked for by name: searched


def test_scan_can_be_followed_and_stopped(tmp_path):
    for number in range(4):
        pair(tmp_path / ("run%d" % number), "KP_%d" % number)
    seen = []
    result = pairing.scan(str(tmp_path), progress=seen.append)
    assert len(result.samples) == 4 and not result.stopped
    assert seen and seen[-1] == 8 and seen == sorted(seen)
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 2

    stopped = pairing.scan(str(tmp_path), should_stop=stop)
    assert stopped.stopped and len(stopped.samples) < 4
    assert "stopped early" in stopped.summary()


def test_scan_reads_no_file_content(tmp_path, monkeypatch):
    a, b = pair(tmp_path, "KP_1")
    real_open = builtins.open

    def no_open(path, *args, **kwargs):
        if str(path).endswith((".gz", ".fastq", ".fq")):
            raise AssertionError("scan opened %s" % path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", no_open)
    assert pairing.scan([str(tmp_path)]).samples[0].lanes == [(a, b)]


# --------------------------------------------------------------------------- peek

def test_peek_of_a_good_file(tmp_path):
    gz = put(tmp_path / "a_R1.fastq.gz", fastq(reads=10, length=151))
    plain = put(tmp_path / "a_R1.fq", fastq(reads=10, length=151))
    for path, is_gz in ((gz, True), (plain, False)):
        look = pairing.peek(path)
        assert look.ok and look.status is PeekStatus.OK and look.problem == ""
        assert look.gzip_content is is_gz and look.gzip_name is is_gz
        assert look.size == os.path.getsize(path)
        assert look.records == 10 and look.longest_read == 151
        assert look.first_read_name == "M01:7:000-AB:1:1101:0:1000"
        assert look.windows_line_ends is False


def test_peek_reads_the_first_kilobytes_only(tmp_path, monkeypatch):
    data = random_fastq(reads=20000)                                    # about 5 MB of text
    big_plain = put(tmp_path / "big_R1.fastq", data)
    big_gz = put(tmp_path / "big_R1.fastq.gz", data)
    assert os.path.getsize(big_plain) > 4 * 1024 * 1024
    assert os.path.getsize(big_gz) > 8 * pairing.PEEK_BYTES
    taken = {}
    real_open = builtins.open

    class Counting:
        def __init__(self, handle, key):
            self.handle, self.key = handle, key

        def read(self, size=-1):
            if size is None or size < 0:
                raise AssertionError("read() without a limit")
            data = self.handle.read(size)
            taken[self.key] = taken.get(self.key, 0) + len(data)
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

    def counting_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if str(path) in (big_plain, big_gz):
            return Counting(handle, str(path))
        return handle

    monkeypatch.setattr(builtins, "open", counting_open)
    for path in (big_plain, big_gz):
        look = pairing.peek(path)
        assert look.ok and look.records > 0
        assert 0 < taken[path] <= pairing.PEEK_BYTES == 64 * 1024


def test_peek_does_not_notice_damage_further_down(tmp_path):
    # Not its job: the read check reads the whole file. The peek must not pretend otherwise.
    data = gzip.compress(random_fastq(reads=20000), mtime=0)
    cut = put(tmp_path / "cut_R1.fastq.gz", data[:len(data) // 2], compress=False)
    assert len(data) // 2 > pairing.PEEK_BYTES
    with pytest.raises(EOFError):
        gzip.decompress(data[:len(data) // 2])                # it really is cut off
    assert pairing.peek(cut).ok


def test_peek_missing_file(tmp_path):
    look = pairing.peek(str(tmp_path / "nowhere" / "x_R1.fastq.gz"))
    assert look.status is PeekStatus.MISSING and not look.ok
    assert look.problem.startswith("File not found.")
    assert pairing._missing_sentence("E:\\run\\x_R1.fastq.gz", splitdrive=ntpath.splitdrive) == \
        "File not found. Is drive E: still connected?"
    assert pairing._missing_sentence("e:\\x.fq", plural=True, splitdrive=ntpath.splitdrive) == \
        "Files not found. Is drive E: still connected?"
    assert "network folder" in pairing._missing_sentence("\\\\nas\\runs\\x.fq", splitdrive=ntpath.splitdrive)


def test_peek_empty_files(tmp_path):
    zero = put(tmp_path / "zero_R1.fastq.gz", b"", compress=False)       # what scepter writes for no reads
    zero_plain = put(tmp_path / "zero_R1.fastq", b"")
    empty_gzip = put(tmp_path / "empty_R1.fastq.gz", b"")                # a valid gzip of nothing
    assert os.path.getsize(empty_gzip) > 0
    for path in (zero, zero_plain, empty_gzip):
        look = pairing.peek(path)
        assert look.status is PeekStatus.EMPTY and look.problem == "This file is empty.", path


def test_peek_gzip_or_not_versus_the_extension(tmp_path):
    hidden_gzip = put(tmp_path / "a_R1.fastq", fastq(), compress=True)
    look = pairing.peek(hidden_gzip)
    assert look.status is PeekStatus.WRONG_EXTENSION
    assert look.gzip_content is True and look.gzip_name is False
    assert "does not end in .gz" in look.problem and ".fastq.gz" in look.problem

    not_gzip = put(tmp_path / "b_R1.fastq.gz", fastq(), compress=False)
    look = pairing.peek(not_gzip)
    assert look.status is PeekStatus.WRONG_EXTENSION
    assert look.gzip_content is False and look.gzip_name is True
    assert "is not compressed" in look.problem

    upper = put(tmp_path / "c_R1.FASTQ.GZ", fastq())
    assert pairing.peek(upper).ok and pairing.peek(upper).gzip_name is True


def test_peek_damaged_gzip(tmp_path):
    good = gzip.compress(fastq(reads=50), mtime=0)
    broken = put(tmp_path / "x_R1.fastq.gz", good[:10] + b"\x00garbage-not-deflate\xff" * 20, compress=False)
    look = pairing.peek(broken)
    assert look.status is PeekStatus.DAMAGED and "damaged" in look.problem
    header_only = put(tmp_path / "y_R1.fastq.gz", good[:12], compress=False)
    assert pairing.peek(header_only).status is PeekStatus.DAMAGED


def test_peek_other_compression_under_a_fastq_name(tmp_path):
    import bz2
    path = put(tmp_path / "a_R1.fastq", bz2.compress(fastq()), compress=False)
    look = pairing.peek(path)
    assert look.status is PeekStatus.OTHER_COMPRESSION and ".fastq.gz" in look.problem


@pytest.mark.parametrize("content, status", [
    (b">contig1\nACGTACGT\n>contig2\nACGT\n", PeekStatus.FASTA),
    (b"<html><body>404 Not Found</body></html>\n", PeekStatus.NOT_FASTQ),
    (b"@r1\nACGT\n+\nIII\n", PeekStatus.NOT_FASTQ),                 # qualities shorter than bases
    (b"@r1\nACGT\nIIII\n@r2\nACGT\nIIII\n", PeekStatus.NOT_FASTQ),  # no "+" line
    (b"@r1\nACGT\n+\n", PeekStatus.NOT_FASTQ),                      # ends inside the first read
    (b"@r1\n\n+\nIIII\n", PeekStatus.NOT_FASTQ),                    # qualities without bases
    (b"\n\n\n", PeekStatus.NOT_FASTQ),
    (b"@r1\rACGT\r+\rIIII\r", PeekStatus.NOT_FASTQ),                # old Mac line ends: one long line
    (b"\x00\x01\x02\x03" * 100, PeekStatus.NOT_FASTQ),
])
def test_peek_content_that_is_not_fastq(tmp_path, content, status):
    for name in ("x_R1.fastq", "x_R1.fastq.gz"):
        look = pairing.peek(put(tmp_path / name, content))
        assert look.status is status, (name, look)
        assert look.problem and not look.ok
    assert "FASTA" in pairing.peek(put(tmp_path / "f.fq", b">a\nACGT\n")).problem


def test_peek_accepts_a_last_read_without_a_line_end(tmp_path):
    look = pairing.peek(put(tmp_path / "x_R1.fastq", b"@r1\nACGT\n+\nIIII"))
    assert look.ok and look.records == 1


def test_peek_windows_line_ends(tmp_path):
    for name in ("w_R1.fastq", "w_R1.fastq.gz"):
        look = pairing.peek(put(tmp_path / name, fastq(crlf=True)))
        assert look.status is PeekStatus.WINDOWS_LINE_ENDS and look.windows_line_ends is True
        assert "Windows line ends" in look.problem
        assert look.records == 4 and look.longest_read == 60          # the read itself is understood
    marked = pairing.peek(put(tmp_path / "bom_R1.fastq", b"\xef\xbb\xbf" + fastq()))
    assert marked.status is PeekStatus.WINDOWS_LINE_ENDS and "text editor" in marked.problem


def test_peek_long_reads(tmp_path):
    medium = pairing.peek(put(tmp_path / "ont_R1.fastq.gz", fastq(reads=3, length=4000)))
    assert medium.status is PeekStatus.LONG_READS and "long reads" in medium.problem
    # One read larger than everything the peek looks at.
    huge = b"@read1 runid=x\n" + b"ACGT" * 150000 + b"\n+\n" + b"I" * 600000 + b"\n"
    for name in ("huge.fastq", "huge.fastq.gz"):
        assert pairing.peek(put(tmp_path / name, huge)).status is PeekStatus.LONG_READS, name
    assert pairing.peek(put(tmp_path / "miseq_R1.fastq.gz", fastq(length=301))).ok


def test_peek_both_reads_in_one_file(tmp_path):
    inter = b"".join(("@r%d/%d\nACGTACGT\n+\nIIIIIIII\n" % (i, m)).encode() for i in range(5) for m in (1, 2))
    look = pairing.peek(put(tmp_path / "il.fastq.gz", inter))
    assert look.status is PeekStatus.INTERLEAVED and "two files" in look.problem
    casava = b"".join(("@M1:1:FC:1:1:%d:1 %d:N:0:1\nACGTACGT\n+\nIIIIIIII\n" % (i, m)).encode()
                      for i in range(5) for m in (1, 2))
    assert pairing.peek(put(tmp_path / "il2.fastq", casava)).status is PeekStatus.INTERLEAVED
    # The same name twice without "this is read 1, this is read 2" is not judged here.
    twins = b"@read\nACGTACGT\n+\nIIIIIIII\n" * 4
    assert pairing.peek(put(tmp_path / "twins_R1.fastq", twins)).ok
    # Consecutive spots of an SRA file are not mates.
    sra = b"".join(("@SRR1234.%d %d length=8\nACGTACGT\n+\nIIIIIIII\n" % (i, i)).encode() for i in (1, 2, 3))
    assert pairing.peek(put(tmp_path / "sra_1.fastq", sra)).ok


def test_peek_never_opens_a_file_that_is_stored_online_only(tmp_path, monkeypatch):
    path = put(tmp_path / "cloud_R1.fastq.gz", fastq())
    asked = []
    real_open = builtins.open

    def guarded_open(name, *args, **kwargs):
        if str(name) == path:
            raise AssertionError("an online-only file was opened")
        return real_open(name, *args, **kwargs)

    def online(name):
        asked.append(name)
        return True

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(gzip, "open", guarded_open)
    look = pairing.peek(path, is_online_only=online)
    assert asked == [path]
    assert look.status is PeekStatus.ONLINE_ONLY and look.gzip_content is None
    assert "online only" in look.problem and "Always keep on this device" in look.problem
    assert look.size == os.path.getsize(path)
    with pytest.raises(AssertionError):
        pairing.peek(path, is_online_only=lambda name: False)         # the guard itself works
    with pytest.raises(AssertionError):
        pairing.peek(path)                                            # default: nothing is online only


def test_peek_survives_a_broken_online_probe(tmp_path):
    path = put(tmp_path / "x_R1.fastq.gz", fastq())

    def broken(name):
        raise OSError("no such attribute here")

    assert pairing.peek(path, is_online_only=broken).ok


def test_peek_file_that_cannot_be_opened(tmp_path, monkeypatch):
    path = put(tmp_path / "busy_R1.fastq.gz", fastq())
    real_open = builtins.open

    def locked(name, *args, **kwargs):
        if str(name) == path:
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", locked)
    look = pairing.peek(path)
    assert look.status is PeekStatus.UNREADABLE and "cannot be opened" in look.problem


def test_peek_path_too_long_and_folder(tmp_path):
    path = put(tmp_path / "x_R1.fastq.gz", fastq())
    assert pairing.peek(path, max_path=len(path)).ok
    look = pairing.peek(path, max_path=len(path) - 1)
    assert look.status is PeekStatus.PATH_TOO_LONG and "too long" in look.problem
    folder = tmp_path / "folder_R1.fastq"
    folder.mkdir()
    assert pairing.peek(str(folder)).status is PeekStatus.NOT_FASTQ


# --------------------------------------------------------------------------- check_sample

def test_check_sample_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "TINY_PAIR_BYTES", 0)
    a, b = pair(tmp_path, "KP_1")
    sample = pairing.scan(str(tmp_path)).samples[0]
    assert pairing.check_sample(sample) is sample
    assert sample.state is SampleState.READY and sample.note == ""
    assert sample.size_bytes == os.path.getsize(a) + os.path.getsize(b)


def test_check_sample_notes_very_little_data(tmp_path):
    pair(tmp_path, "KP_1")
    sample = pairing.check_sample(pairing.scan(str(tmp_path)).samples[0])
    assert sample.state is SampleState.READY_NOTE and sample.ready
    assert sample.note == pairing.TINY == "Very little data. The assembly will probably be poor or fail."


def test_check_sample_size_notes(tmp_path, monkeypatch):
    a, b = pair(tmp_path, "KP_1")
    sample = pairing.scan(str(tmp_path)).samples[0]
    real_peek = pairing.peek

    def sized(first, second):
        def fake(path, is_online_only=None, **kw):
            look = real_peek(path, is_online_only, **kw)
            look.size = first if path == a else second
            return look
        return fake

    monkeypatch.setattr(pairing, "peek", sized(3 * GB, 3 * GB))
    assert pairing.assess_sample(sample) == (SampleState.READY_NOTE, pairing.HUGE)
    monkeypatch.setattr(pairing, "peek", sized(300 * 1024 ** 2, 310 * 1024 ** 2))
    assert pairing.assess_sample(sample) == (SampleState.READY, "")
    monkeypatch.setattr(pairing, "peek", sized(300 * 1024 ** 2, 150 * 1024 ** 2))
    assert pairing.assess_sample(sample) == (SampleState.READY_NOTE, pairing.LOPSIDED)
    monkeypatch.setattr(pairing, "peek", sized(11 * 1024 ** 2, 5 * 1024 ** 2))
    assert pairing.assess_sample(sample) == (SampleState.READY_NOTE, pairing.TINY + " " + pairing.LOPSIDED)
    # assess_sample changes nothing.
    assert sample.state is SampleState.READY and sample.note == ""


def test_check_sample_left_out_with_the_first_serious_reason(tmp_path):
    a = put(tmp_path / "KP_R1.fastq.gz", fastq())
    b = put(tmp_path / "KP_R2.fastq.gz", b"", compress=False)
    sample = pairing.check_sample(pairing.scan(str(tmp_path)).samples[0])
    assert sample.state is SampleState.LEFT_OUT and sample.note == "Read 2: This file is empty."

    put(a, b">c\nACGT\n")                                     # now Read 1 is wrong too, and comes first
    pairing.check_sample(sample)
    assert sample.note.startswith("Read 1: This is a FASTA file")

    put(a, fastq())
    put(b, fastq(mate=2))                                     # repaired: Ready again, by itself
    assert pairing.check_sample(sample).ready


def test_check_sample_same_trouble_in_every_file_is_said_once(tmp_path):
    a, b = pair(tmp_path, "KP")
    sample = pairing.scan(str(tmp_path)).samples[0]
    pairing.check_sample(sample, is_online_only=lambda path: True)
    assert sample.state is SampleState.LEFT_OUT
    assert sample.note.startswith("These files are stored online only.")
    pairing.check_sample(sample, is_online_only=lambda path: path == b)
    assert sample.note.startswith("Read 2: This file is stored online only.")
    os.remove(a)
    os.remove(b)
    pairing.check_sample(sample)
    assert sample.note.startswith("Files not found.")
    put(a, fastq())
    pairing.check_sample(sample)
    assert sample.note.startswith("Read 2: File not found.")


def test_check_sample_names_the_lane(tmp_path):
    files = lane_files(tmp_path, "KP", [1, 2, 7])
    put(files[2][0], fastq(crlf=True))
    sample = pairing.check_sample(pairing.scan(str(tmp_path)).samples[0])
    assert sample.state is SampleState.LEFT_OUT
    assert sample.note.startswith("Lane 7, Read 1: This file has Windows line ends")


def test_check_sample_files_that_do_not_belong_together(tmp_path):
    a = put(tmp_path / "X_1.fastq.gz", fastq(name="runA:%d"))
    b = put(tmp_path / "X_2.fastq.gz", fastq(name="runB:%d", mate=2))
    sample = pairing.check_sample(pairing.scan(str(tmp_path)).samples[0])
    assert sample.state is SampleState.LEFT_OUT and sample.note == pairing.NAMES_DIFFER
    assert "do not belong together" in sample.note


@pytest.mark.parametrize("first, second", [
    ("@r7/1", "@r7/2"),
    ("@M1:1:FC:1:1:5:9 1:N:0:ACGT", "@M1:1:FC:1:1:5:9 2:N:0:ACGT"),
    ("@SRR99.7 7 length=8", "@SRR99.7 7 length=8"),
    ("@SRR99.7.1 7 length=8", "@SRR99.7.2 7 length=8"),               # fastq-dump --readids
    ("@V3:L1:C1:R1:42_1", "@V3:L1:C1:R1:42_2"),
])
def test_check_sample_mate_names_that_do_belong_together(tmp_path, monkeypatch, first, second):
    monkeypatch.setattr(pairing, "TINY_PAIR_BYTES", 0)
    record = "%s\nACGTACGT\n+\nIIIIIIII\n@other\nACGTACGT\n+\nIIIIIIII\n"
    put(tmp_path / "X_R1.fastq", (record % first).encode())
    put(tmp_path / "X_R2.fastq", (record % second).encode())
    sample = pairing.check_sample(pairing.scan(str(tmp_path)).samples[0])
    assert sample.state is SampleState.READY, sample.note


def test_check_sample_keeps_what_the_names_said(tmp_path):
    lone = put(tmp_path / "solo_R1.fastq.gz", b"not even fastq")
    sample = pairing.check_sample(pairing.scan([lone]).samples[0])
    assert sample.note == pairing.NO_READ2                    # the structural reason comes first
    put(tmp_path / "mix_R1.fastq.gz")
    put(tmp_path / "mix_R2.fastq", mate=2)
    mixed = [s for s in pairing.scan(str(tmp_path)).samples if s.name == "mix"][0]
    assert pairing.check_sample(mixed).note == pairing.PAIR_COMPRESSION


def test_check_sample_keeps_the_harmless_notes_of_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "TINY_PAIR_BYTES", 0)
    pair(tmp_path, "KP 5")
    t1, _ = pair(tmp_path / "t", "KP_6", "_trimmed_R1.fastq.gz", "_trimmed_R2.fastq.gz")
    renamed = pairing.check_sample(pairing.scan(str(tmp_path)).samples[0])
    assert renamed.state is SampleState.READY_NOTE and renamed.note.startswith("Name changed from 'KP 5'")
    trimmed = pairing.check_sample(pairing.scan([t1]).samples[0])
    assert trimmed.state is SampleState.READY_NOTE and trimmed.note == pairing.ALREADY_TRIMMED
    assert trimmed.trim is False


# --------------------------------------------------------------------------- name clashes

def two_runs(tmp_path, name="KP_001"):
    pair(tmp_path / "run1", name)
    pair(tmp_path / "run2", name)
    pair(tmp_path / "run2", "SA_17")
    return pairing.scan(str(tmp_path)).samples


def test_samples_that_share_a_name_are_left_out_with_the_reason(tmp_path):
    samples = two_runs(tmp_path)
    assert [s.name for s in samples] == ["KP_001", "KP_001", "SA_17"]
    assert all(s.ready for s in samples)                      # scan alone does not judge the names
    marked = pairing.mark_name_clashes(samples)
    assert marked == samples[:2]
    for sample in samples[:2]:
        assert sample.state is SampleState.LEFT_OUT and pairing.is_name_clash(sample)
        assert sample.note == "Two samples are called KP_001. Rename one of them (double-click the name)."
    assert samples[2].state is SampleState.READY and not pairing.is_name_clash(samples[2])
    assert pairing.mark_name_clashes(samples) == samples[:2]  # asking again changes nothing
    assert samples[0].note.startswith("Two samples are called KP_001.")


def test_names_that_differ_only_in_capitals_clash(tmp_path):
    pair(tmp_path / "a", "kp_1")
    pair(tmp_path / "b", "KP_1")
    pair(tmp_path / "c", "Kp_1")
    samples = pairing.scan(str(tmp_path)).samples
    assert len(pairing.mark_name_clashes(samples)) == 3
    assert samples[0].note.startswith("3 samples have the same name apart from capital letters (")
    assert "KP_1" in samples[0].note and "kp_1" in samples[0].note and "Kp_1" in samples[0].note
    assert all(pairing.is_name_clash(s) for s in samples)


def test_a_row_left_out_for_its_own_reason_keeps_that_reason(tmp_path):
    pair(tmp_path / "run1", "KP")
    put(tmp_path / "run2" / "KP_R1.fastq.gz")                 # no partner
    samples = pairing.scan(str(tmp_path)).samples
    assert pairing.mark_name_clashes(samples) == []
    assert sorted(s.state.value for s in samples) == ["left_out", "ready"]
    assert [s.note for s in samples if not s.ready] == [pairing.NO_READ2]


def test_rename_ends_the_clash_and_gives_back_the_earlier_state(tmp_path):
    samples = two_runs(tmp_path)
    for sample in samples:
        pairing.check_sample(sample)                          # tiny files: Ready with a note
    pairing.mark_name_clashes(samples)
    assert pairing.is_name_clash(samples[0]) and pairing.is_name_clash(samples[1])
    assert pairing.rename(samples, samples[1], "KP_001_second") == ""
    assert [s.name for s in samples] == ["KP_001", "KP_001_second", "SA_17"]
    for sample in samples:
        assert sample.state is SampleState.READY_NOTE and sample.note == pairing.TINY
    # Renaming back brings the clash back.
    assert pairing.rename(samples, samples[1], "KP_001") == "There is already a sample with this name."
    assert samples[1].name == "KP_001_second" and samples[1].ready


def test_rename_validation_returns_a_sentence_and_changes_nothing(tmp_path):
    samples = two_runs(tmp_path, name="AB")
    pairing.mark_name_clashes(samples)
    target = samples[0]
    cases = {
        "": "Type a name for the sample.",
        "   ": "Type a name for the sample.",
        "x" * 61: "Too long: 60 characters at most.",
        "KP 1": 'A name cannot contain spaces or any of these characters: < > : " / \\ | ? *',
        "KP/1": 'A name cannot contain spaces or any of these characters: < > : " / \\ | ? *',
        "KP:1": 'A name cannot contain spaces or any of these characters: < > : " / \\ | ? *',
        "a\\b": 'A name cannot contain spaces or any of these characters: < > : " / \\ | ? *',
        "what?": 'A name cannot contain spaces or any of these characters: < > : " / \\ | ? *',
        "CON": "Windows does not allow the name CON. Choose another name.",
        "lpt1": "Windows does not allow the name lpt1. Choose another name.",
        "_KP": "A name cannot begin or end with a dot or an underscore.",
        "KP.": "A name cannot begin or end with a dot or an underscore.",
        "KP__1": "This name cannot be used as it is. Try KP_1.",
        "SA_17": "There is already a sample with this name.",
        "sa_17": "There is already a sample with this name.",
    }
    for typed, sentence in cases.items():
        assert pairing.name_problem(samples, target, typed) == sentence, typed
        assert pairing.rename(samples, target, typed) == sentence
        assert target.name == "AB"
    # Its own name, in any capitals, is free; so are brackets, accents and non-Latin letters.
    for typed in ("AB", "ab", "KP-[1].é", "肺炎桿菌_3", "x" * 60, "  padded  "):
        assert pairing.name_problem([target, samples[2]], target, typed) == "", typed
    assert pairing.rename(samples, target, "  KP-[1].é ") == ""
    assert target.name == "KP-[1].é"
    assert all(s.ready for s in samples)


def test_a_name_the_program_needs_for_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "TINY_PAIR_BYTES", 0)
    pair(tmp_path / "run1", "Assemblies")                     # <results>\assemblies is the program's
    pair(tmp_path / "run1", "KP_1")
    samples = pairing.merge([], pairing.scan(str(tmp_path))).samples
    table = by_name(samples)
    taken = table["Assemblies"]
    assert taken.state is SampleState.LEFT_OUT and not pairing.is_name_clash(taken)
    assert taken.note == ("The name Assemblies is used by the program for its own files in the results "
                          "folder. Rename this sample (double-click the name).")
    assert table["KP_1"].state is SampleState.READY
    for typed in ("assemblies", "ASSEMBLIES", "last_run.json", "run_summary.html", "run_summary.tsv",
                  "run_log.txt", "tesseract-win.lock"):
        problem = pairing.rename(samples, table["KP_1"], typed)
        assert problem == "The program uses the name %s for its own files. Choose another name." % typed
    assert pairing.check_sample(taken).ready                  # the files themselves are fine
    pairing.mark_name_clashes(samples)
    assert not taken.ready                                    # the name still is not
    assert pairing.rename(samples, taken, "Assemblies_KP") == ""
    assert taken.state is SampleState.READY and taken.note == ""

    # The button repairs it as well.
    again = pairing.merge([], pairing.scan(str(tmp_path))).samples
    renamed = pairing.make_names_different(again)
    assert [s.name for s in renamed] == ["Assemblies_run1"]
    assert all(s.ready for s in again)


def test_rename_drops_the_note_about_the_cleaned_name(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "TINY_PAIR_BYTES", 0)
    pair(tmp_path, "KP 1")
    samples = pairing.scan(str(tmp_path)).samples
    assert samples[0].state is SampleState.READY_NOTE and samples[0].note.startswith("Name changed")
    assert pairing.rename(samples, samples[0], "Klebsiella_1") == ""
    assert samples[0].state is SampleState.READY and samples[0].note == ""
    assert pairing.check_sample(samples[0]).note == ""


def test_make_the_names_different_adds_the_folder_name(tmp_path):
    samples = two_runs(tmp_path)
    pairing.mark_name_clashes(samples)
    renamed = pairing.make_names_different(samples)
    assert renamed == samples[:2]
    assert sorted(s.name for s in samples) == ["KP_001_run1", "KP_001_run2", "SA_17"]
    assert all(s.ready for s in samples) and pairing.mark_name_clashes(samples) == []
    assert pairing.make_names_different(samples) == []        # nothing left to do


def test_make_the_names_different_goes_up_when_the_folders_share_a_name(tmp_path):
    pair(tmp_path / "March" / "reads", "KP")
    pair(tmp_path / "April" / "reads", "KP")
    samples = pairing.scan(str(tmp_path)).samples
    pairing.make_names_different(samples)
    assert sorted(s.name for s in samples) == ["KP_April_reads", "KP_March_reads"]
    assert all(s.ready for s in samples)


def test_make_the_names_different_numbers_samples_of_one_folder(tmp_path):
    pair(tmp_path, "KP")
    pair(tmp_path, "KP", "_R1.fastq", "_R2.fastq")            # the same reads, decompressed
    pair(tmp_path, "KP_1", "_1.fq", "_2.fq")                  # a name the numbering must not take
    samples = pairing.scan(str(tmp_path)).samples
    pairing.make_names_different(samples)
    names = sorted(s.name for s in samples)
    assert len(set(n.casefold() for n in names)) == 3 and "KP_1" in names
    assert all(s.ready for s in samples)
    assert all(n.startswith("KP_") for n in names)


def test_make_the_names_different_keeps_names_within_sixty_characters(tmp_path):
    long_name = "K" * 60
    pair(tmp_path / ("first_folder_with_quite_a_long_name_" * 2), long_name)
    pair(tmp_path / "second", long_name)
    samples = pairing.scan(str(tmp_path)).samples
    assert [s.name for s in samples] == [long_name, long_name]
    pairing.make_names_different(samples)
    names = [s.name for s in samples]
    assert len(set(names)) == 2 and all(len(n) <= 60 for n in names)
    assert all(n == fsutil.safe_name(n) for n in names)
    assert all(s.ready for s in samples)


def test_make_the_names_different_avoids_names_already_in_the_list(tmp_path):
    pair(tmp_path / "run1", "KP")
    pair(tmp_path / "run2", "KP")
    pair(tmp_path / "other", "KP_run1")                       # already has the name the button would give
    samples = pairing.scan(str(tmp_path)).samples
    pairing.make_names_different(samples)
    names = [s.name.casefold() for s in samples]
    assert len(set(names)) == 3 and all(s.ready for s in samples)


# --------------------------------------------------------------------------- merge

def test_merge_skips_exact_duplicates_and_reports_them(tmp_path):
    pair(tmp_path / "a", "KP_1")
    pair(tmp_path / "a", "KP_2")
    existing = pairing.scan(str(tmp_path / "a")).samples
    existing[0].trim = False                                   # the user's own choice survives
    pair(tmp_path / "a", "KP_3")
    found = pairing.scan(str(tmp_path / "a"))
    merged = pairing.merge(existing, found)
    assert [s.name for s in merged.samples] == ["KP_1", "KP_2", "KP_3"]
    assert merged.samples[0] is existing[0] and merged.samples[0].trim is False
    assert [s.name for s in merged.added] == ["KP_3"]
    assert [s.name for s in merged.duplicates] == ["KP_1", "KP_2"]
    assert merged.duplicate_files == 4 and merged.note == "Already in the list: 4 files."
    assert len(existing) == 2                                  # the caller's list is not touched
    again = pairing.merge(merged.samples, pairing.scan(str(tmp_path / "a")).samples)
    assert again.added == [] and len(again.samples) == 3 and again.duplicate_files == 6
    assert pairing.merge([], []).note == ""


def test_merge_marks_name_clashes(tmp_path):
    pair(tmp_path / "run1", "KP")
    pair(tmp_path / "run2", "KP")
    first = pairing.merge([], pairing.scan(str(tmp_path / "run1")))
    assert [s.state for s in first.samples] == [SampleState.READY]
    second = pairing.merge(first.samples, pairing.scan(str(tmp_path / "run2")))
    assert len(second.samples) == 2 and all(pairing.is_name_clash(s) for s in second.samples)
    # Taking one of them off the list frees the other.
    rest = second.samples[:1]
    assert pairing.mark_name_clashes(rest) == [] and rest[0].state is SampleState.READY


def test_merge_part_of_a_sample_already_listed_is_a_duplicate(tmp_path):
    lanes = lane_files(tmp_path, "KP", [1, 2, 3])
    whole = pairing.scan(str(tmp_path)).samples
    part = pairing.scan(list(lanes[1]))
    assert len(part.samples[0].lanes) == 1
    merged = pairing.merge(whole, part)
    assert len(merged.samples) == 1 and merged.duplicate_files == 2 and merged.added == []


def test_merge_a_completed_pair_takes_the_place_of_the_incomplete_row(tmp_path):
    a = put(tmp_path / "KP_R1.fastq.gz")
    pair(tmp_path, "AA")
    first = pairing.merge([], pairing.scan(str(tmp_path)))
    assert [(s.name, s.ready) for s in first.samples] == [("AA", True), ("KP", False)]
    b = put(tmp_path / "KP_R2.fastq.gz", mate=2)              # the copy has finished
    second = pairing.merge(first.samples, pairing.scan(str(tmp_path)))
    assert [(s.name, s.ready) for s in second.samples] == [("AA", True), ("KP", True)]
    assert second.samples[1].lanes == [(a, b)]
    assert [s.name for s in second.replaced] == ["KP"] and [s.name for s in second.added] == ["KP"]
    assert second.duplicate_files == 2                         # AA was found again


# --------------------------------------------------------------------------- disk

def sample(name, size, lanes=1, trim=True, state=SampleState.READY):
    return Sample(name=name, lanes=[("/r/%s_L%d_R1.fq.gz" % (name, i), "/r/%s_L%d_R2.fq.gz" % (name, i))
                                    for i in range(lanes)],
                  trim=trim, size_bytes=size, state=state)


#: What one finished sample is counted to leave behind besides its trimmed reads.
LEFT = pairing.RESULT_BYTES_PER_SAMPLE


def test_per_sample_rule_of_the_bash_script():
    # need_kb=$(( ( $(wc -c < "$R1") + $(wc -c < "$R2") ) * 3 / 1024 + 2097152 ))
    assert pairing.sample_disk_need(0) == 2 * GB
    assert pairing.sample_disk_need(1024) == 2 * GB + 3 * 1024
    assert pairing.sample_disk_need(400 * 1024 ** 2) == 2 * GB + 1200 * 1024 ** 2
    assert pairing.sample_disk_need(1000) == (1000 * 3 // 1024 + 2097152) * 1024
    assert pairing.sample_disk_need(sample("a", 5 * GB)) == 17 * GB


def test_batch_disk_need():
    assert pairing.estimate_disk_need([], True) == 0
    assert pairing.DISK_MARGIN_BYTES == 2 * GB
    one = [sample("a", GB)]
    # kept trimmed reads (1) + the read check's three times (3) + margin (2), + what one sample leaves
    assert pairing.estimate_disk_need(one, True) == 6 * GB + LEFT
    # temporary trimmed reads (1) + 3 + 2: one sample needs the same either way
    assert pairing.estimate_disk_need(one, False) == 6 * GB + LEFT
    forty = [sample("s%d" % i, GB) for i in range(40)]
    assert pairing.estimate_disk_need(forty, True) == (40 + 3 + 2) * GB + 40 * LEFT
    assert pairing.estimate_disk_need(forty, False) == (1 + 3 + 2) * GB + 40 * LEFT
    # Samples that are not trimmed add nothing that stays, apart from their results.
    untrimmed = [sample("s%d" % i, GB, trim=False) for i in range(40)]
    assert pairing.estimate_disk_need(untrimmed, True) == (3 + 2) * GB + 40 * LEFT
    assert pairing.estimate_disk_need(untrimmed, False) == (3 + 2) * GB + 40 * LEFT


def test_batch_disk_need_counts_joined_lanes_and_the_largest_sample():
    batch = [sample("small", GB), sample("lanes", 2 * GB, lanes=4), sample("big", 5 * GB, trim=False)]
    # kept: 1 + 2. working space: small 3, lanes 2 + 6 = 8, big 15 -> 15. margin 2.
    assert pairing.estimate_disk_need(batch, True) == (3 + 15 + 2) * GB + 3 * LEFT
    # nothing kept. working: small 1 + 3, lanes 2 + 2 + 6 = 10, big 15 -> 15.
    assert pairing.estimate_disk_need(batch, False) == (15 + 2) * GB + 3 * LEFT
    batch.append(sample("lanes2", 4 * GB, lanes=2))
    assert pairing.estimate_disk_need(batch, False) == (4 + 4 + 12 + 2) * GB + 4 * LEFT
    assert pairing.estimate_disk_need(batch, True) == (1 + 2 + 4 + 4 + 12 + 2) * GB + 4 * LEFT


def test_batch_disk_need_leaves_out_the_left_out_and_covers_every_read_check():
    batch = [sample("a", GB), sample("gone", 50 * GB, state=SampleState.LEFT_OUT),
             sample("note", 2 * GB, state=SampleState.READY_NOTE)]
    assert pairing.estimate_disk_need(batch, True) == (1 + 2 + 6 + 2) * GB + 2 * LEFT
    assert pairing.estimate_disk_need(iter(batch), True) == pairing.estimate_disk_need(batch, True)
    for keep in (True, False):
        need = pairing.estimate_disk_need(batch, keep)
        assert all(need >= pairing.sample_disk_need(s) for s in batch if s.ready)


def test_a_batch_that_just_fits_passes_the_disk_line_of_every_read_check():
    """Found in review. The three tests above had the numbers of a formula without the term
    for what each finished sample leaves (contigs, graph, reports, the copy in assemblies).
    Without it a disk with exactly the space asked for failed the read check of the later
    samples, by what the earlier ones had left. Here the batch is played through."""
    import random as rnd
    rng = rnd.Random(7)
    assert LEFT == 32 * 1024 ** 2
    for keep in (True, False):
        for _ in range(60):
            batch = [sample("s%d" % i, rng.randrange(1, 3 * GB), lanes=rng.choice([1, 1, 2, 4]),
                            trim=rng.random() < 0.8) for i in range(rng.randrange(1, 40))]
            free = pairing.estimate_disk_need(batch, keep)          # a disk with exactly that much
            for row in batch:
                size = row.size_bytes
                joined = size if len(row.lanes) > 1 else 0
                trimmed = size if row.trim else 0                   # as large as the inputs: the worst case
                at_read_check = free - joined - trimmed
                assert at_read_check >= pairing.sample_disk_need(size), (keep, row.name)
                # The sample ends: the joined copy goes, the trimmed reads go unless kept, results stay.
                free -= (trimmed if keep else 0) + LEFT
            assert free >= 0


# --------------------------------------------------------------------------- longest path

def test_longest_result_path_uses_the_names_of_decision_d1(tmp_path):
    results = str(tmp_path / "Results")
    name = "K" * 60
    longest = pairing.longest_result_path(results, [sample("KP_1", 1), sample(name, 1)])
    # The earlier result, parked while a new one is swapped in, is the deepest place there is.
    assert longest == os.path.join(results, name, "_in_progress", "replaced", "trimming",
                                   name + "_trimmed_R1.fastq.gz")
    assert len(longest) == len(results) + 1 + 60 + 1 + 21 + 1 + 8 + 1 + 60 + 20
    # A short name: the deepest file of the assembly folder is the longest.
    short = pairing.longest_result_path(results, [sample("A", 1)])
    assert short == os.path.join(results, "A", "_in_progress", "replaced", "assembly",
                                 "attempt1_default_k", "p2_self_qa.missing_hi.tsv")
    # Longer than the same files in the other places a sample folder has.
    for other in ("_in_progress", "last_failed_attempt", "new_result"):
        assert len(short) > len(os.path.join(results, "A", other, "assembly", "attempt1_default_k",
                                             "p2_self_qa.missing_hi.tsv"))
    # No sample that runs: the folder's own files.
    for samples in ([], [sample("gone", 1, state=SampleState.LEFT_OUT)]):
        own = pairing.longest_result_path(results, samples)
        assert os.path.dirname(own) == results and os.path.basename(own).startswith("run_summary.html")


def test_longest_result_path_is_absolute_and_grows_with_the_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    samples = [sample("KP_001", 1)]
    relative = pairing.longest_result_path("out", samples)
    assert os.path.isabs(relative) and relative.startswith(str(tmp_path))
    deep = pairing.longest_result_path(os.path.join(str(tmp_path), "a" * 100, "out"), samples)
    assert len(deep) == len(relative) + 101
    assert pairing.MAX_RESULT_PATH == 240
    assert len(deep) > pairing.MAX_RESULT_PATH > len(pairing.longest_result_path("/r", samples))


def test_longest_result_path_covers_everything_a_real_run_writes():
    """Every file of the golden output trees fits under the path that is reported."""
    golden = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parity", "golden")
    if not os.path.isdir(golden):
        pytest.skip("no golden output trees here")
    deepest = 0
    for folder, _dirs, files in os.walk(golden):
        if "out_tree.txt" in files:
            with open(os.path.join(folder, "out_tree.txt"), encoding="utf-8") as handle:
                for line in handle:
                    deepest = max(deepest, len(line.strip().lstrip("./").rstrip("/")))
    assert deepest > 0
    reported = pairing.longest_result_path("/r", [sample("A", 1)])
    allowed = len(reported) - len(os.path.join("/r", "A", "_in_progress", "replaced", "assembly", ""))
    assert deepest <= allowed


def test_names_of_decision_d1_agree_with_the_other_modules():
    """pairing keeps its own copy of a few names; they must be the ones results.py writes."""
    from tesseract_win import settings_map
    for own in settings_map.trimmed_names("KP_1"):
        stem = own[:-len(".fastq.gz")]
        assert pairing._OWN_TRIMMED_RE.search(stem), own
        assert pairing._parse_name(own).own_trimmed
    results = pytest.importorskip("tesseract_win.results")
    assert pairing._STATUS_FILE == results.STATUS_FILE
    assert set(pairing._RESULTS_MARKERS) == {results.STATUS_FILE, results.LAST_RUN, results.LOCK_FILE}
    assert set(pairing._OWN_WORK_FOLDERS) == {results.IN_PROGRESS, results.LAST_FAILED, results.NEW_RESULT}
    assert pairing._RESERVED_NAMES == {results.ASSEMBLIES, results.SUMMARY_HTML, results.SUMMARY_TSV,
                                       results.RUN_LOG, results.LAST_RUN, results.LOCK_FILE}
    assert results.trimmed_read_names("KP_1") == settings_map.trimmed_names("KP_1")
    assert (results.TRIMMING_REPORT_JSON, results.TRIMMING_REPORT_HTML, results.TRIMMING_LOG) == (
        settings_map.TRIMMING_REPORT_JSON, settings_map.TRIMMING_REPORT_HTML, settings_map.TRIMMING_LOG)
    replaced = getattr(results, "_REPLACED", None)
    if replaced is not None:
        assert pairing._DEEPEST_WORK_FOLDER == (results.IN_PROGRESS, replaced)
        deepest = os.path.join(*pairing._DEEPEST_WORK_FOLDER)
        assert len(deepest) >= max(len(results.LAST_FAILED), len(results.NEW_RESULT))
    name = "K" * 60
    longest = pairing.longest_result_path("/r", [sample(name, 1)])
    for made in (os.path.join(results.in_progress_trimming_dir("/r", name), results.trimmed_read_names(name)[0]),
                 os.path.join(results.last_failed_dir("/r", name), results.TRIMMING,
                              results.trimmed_read_names(name)[1]),
                 results.collected_fasta("/r", name), results.status_path("/r", name)):
        assert len(os.path.abspath(made)) <= len(longest), made


# --------------------------------------------------------------------------- Windows paths

def test_windows_paths_in_the_functions_that_only_look_at_names(monkeypatch):
    """The same code with Windows path rules: backslashes, drive letters, capitals that do not count.

    Only functions that never touch the disk are called while ``os.path`` is swapped.
    """
    monkeypatch.setattr(os, "path", ntpath)

    def row(name, folder, first="_R1.fastq.gz", second="_R2.fastq.gz"):
        return Sample(name=name, lanes=[(folder + "\\" + name + first, folder + "\\" + name + second)],
                      size_bytes=10)

    a = row("KP", "D:\\Séquençage 2026\\run 1")
    b = row("KP", "D:\\Séquençage 2026\\run 2")
    c = row("X", "\\\\nas\\share", "_1.fq", "_2.fq")
    d = row("X", "E:", "_1.fq", "_2.fq")
    rows = [a, b, c, d]
    assert all(pairing._static_problem(r) == "" for r in rows)
    assert len(pairing.mark_name_clashes(rows)) == 4
    assert pairing._folder_parts(a) == ["run 1", "Séquençage 2026"]
    assert pairing._folder_parts(c) == [] and pairing._folder_parts(d) == []
    pairing.make_names_different(rows)
    assert [r.name for r in rows] == ["KP_run_1", "KP_run_2", "X_1", "X_2"]
    assert all(r.ready for r in rows)

    # Capitals and the direction of the slashes do not make two files of one.
    same = Sample(name="S", lanes=[("D:\\Reads\\S_R1.fastq", "d:/reads/s_r1.FASTQ")])
    assert pairing._static_problem(same) == pairing.SAME_FILE
    again = row("KP_run_1", "d:\\séquençage 2026\\RUN 1")
    again.lanes = [(a.lanes[0][0].upper(), a.lanes[0][1].lower())]
    merged = pairing.merge(rows, [again])
    assert merged.duplicates == [again] and len(merged.samples) == 4

    longest = pairing.longest_result_path("D:\\Results", [row("KP_001", "D:\\r")])
    assert longest == ("D:\\Results\\KP_001\\_in_progress\\replaced\\assembly\\attempt1_default_k"
                       "\\p2_self_qa.missing_hi.tsv")
    deep = "D:\\" + "\\".join(["a folder with a long name"] * 7)
    assert len(pairing.longest_result_path(deep, [row("KP_001", "D:\\r")])) > pairing.MAX_RESULT_PATH
    assert pairing._missing_sentence("E:\\run\\x_R1.fastq.gz") == "File not found. Is drive E: still connected?"
    assert pairing._lane_label(Sample("L", [("C:\\r\\L_S1_L004_R1_001.fastq.gz", "")]), 0) == "Lane 4"


# --------------------------------------------------------------------------- the whole way

def test_from_a_folder_to_a_list_ready_to_start(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "TINY_PAIR_BYTES", 0)
    run = tmp_path / "Séquençage 2026 [MiSeq]"
    pair(run, "KP_001")
    lane_files(run, "KP_002", [1, 2, 3, 4], s=2)
    pair(run, "SA 17", "_1.fq.gz", "_2.fq.gz")
    put(run / "EC_09_R1.fastq.gz")
    lane_files(run, "Undetermined", [1], s=0)
    put(run / "KP_002_S2_L001_I1_001.fastq.gz")
    put(run / "SampleSheet.csv", b"x")
    make_results_folder(run / branding.RESULTS_DIR_NAME, sample="KP_001")

    found = pairing.scan([str(run)])
    assert found.summary() == ("Found 3 samples (12 files). Not added: 2 other files, 2 Undetermined "
                               "files, 1 file with no partner, 1 results folder.")
    merged = pairing.merge([], found)
    for row in merged.samples:
        pairing.check_sample(row)
    pairing.mark_name_clashes(merged.samples)
    table = {row.name: (row.state, len(row.lanes), row.trim) for row in merged.samples}
    assert table == {
        "EC_09": (SampleState.LEFT_OUT, 1, True),
        "KP_001": (SampleState.READY, 1, True),
        "KP_002": (SampleState.READY, 4, True),
        "SA_17": (SampleState.READY_NOTE, 1, True),
        "Undetermined": (SampleState.LEFT_OUT, 1, True),
    }
    ready = [row for row in merged.samples if row.ready]
    assert pairing.estimate_disk_need(ready, True) > pairing.DISK_MARGIN_BYTES
    assert len(pairing.longest_result_path(str(run / branding.RESULTS_DIR_NAME), ready)) < 240
    # The rows survive the trip through last_run.json.
    for row in merged.samples:
        assert Sample.from_dict(row.to_dict()) == row


# --------------------------------------------------------------------------- found in review
# Inputs the tests above did not have: odd characters, files a Mac leaves behind, reads with
# no bases, small files that are cut short, rows that share files, a search that is stopped.

def test_a_read_with_no_bases_is_still_a_read(tmp_path):
    """Was listed above as "not FASTQ". It is FASTQ: tools that trim without a length filter
    write such reads, and scepter, tesseract-asm and the read check all take them (tried with
    the real engines in review). Calling the file "not a FASTQ file" left a good sample out."""
    empty_first = b"@r0 1:N:0:1\n\n+\n\n" + fastq(reads=5)
    for name in ("z_R1.fastq", "z_R1.fastq.gz"):
        look = pairing.peek(put(tmp_path / name, empty_first))
        assert look.ok and look.records == 6 and look.first_read_name == "r0", name
        assert look.longest_read == 60
    only = pairing.peek(put(tmp_path / "only_R1.fastq", b"@r0\n\n+\n\n"))
    assert only.ok and only.records == 1 and only.longest_read == 0
    a = put(tmp_path / "pair" / "Z_R1.fastq.gz", b"@r0 1:N:0:1\n\n+\n\n" + fastq(reads=5, mate=1))
    b = put(tmp_path / "pair" / "Z_R2.fastq.gz", b"@r0 2:N:0:1\n\n+\n\n" + fastq(reads=5, mate=2))
    assert pairing.check_sample(Sample("Z", [(a, b)])).ready


def test_peek_small_gzip_that_is_cut_short(tmp_path):
    """A copy that stopped early. scepter takes such a file without a word (exit 0)."""
    reads = [fastq(reads=1, start=i) for i in range(40)]
    whole = gzip.compress(b"".join(reads), mtime=0)
    assert len(whole) < pairing.PEEK_BYTES
    for cut in (len(whole) - 1, len(whole) - 8, len(whole) // 2, 30):
        look = pairing.peek(put(tmp_path / ("cut%d_R1.fastq.gz" % cut), whole[:cut], compress=False))
        assert look.status is PeekStatus.DAMAGED, cut
        assert look.problem.startswith("This file is incomplete") and "copy" in look.problem
    # Cut exactly between two compressed blocks: every block that is there is complete, so
    # nothing can be seen (the read check counts the reads of both files instead).
    blocks = [gzip.compress(read, mtime=0) for read in reads]
    on_boundary = put(tmp_path / "b_R1.fastq.gz", b"".join(blocks[:20]), compress=False)
    assert pairing.peek(on_boundary).ok and pairing.peek(on_boundary).records == 20
    # ...but a cut inside the last of several blocks is seen.
    inside = put(tmp_path / "i_R1.fastq.gz", b"".join(blocks[:20]) + blocks[20][:25], compress=False)
    assert pairing.peek(inside).status is PeekStatus.DAMAGED
    # Whatever follows a complete block and is not gzip is ignored, as zlib and both engines do.
    padded = put(tmp_path / "p_R1.fastq.gz", whole + b"\0" * 512, compress=False)
    assert pairing.peek(padded).ok and pairing.peek(padded).records == 40
    # A text that would be far longer than what is looked at is not "cut short".
    long_text = put(tmp_path / "l_R1.fastq.gz", fastq(reads=20000))
    assert os.path.getsize(long_text) < pairing.PEEK_BYTES
    assert pairing.peek(long_text).ok
    sample = Sample("C", [(put(tmp_path / "C_R1.fastq.gz", whole[:len(whole) // 2], compress=False),
                           put(tmp_path / "C_R2.fastq.gz", whole, compress=False))])
    assert pairing.assess_sample(sample) == (
        SampleState.LEFT_OUT, "Read 1: " + pairing._CUT_SHORT)


def test_peek_lanes_joined_end_to_end_and_blocked_gzip(tmp_path):
    """Several gzip members in a row: joined lanes, and the blocked files of bgzip."""
    joined = gzip.compress(fastq(reads=3), mtime=0) + gzip.compress(fastq(reads=3, start=3), mtime=0)
    look = pairing.peek(put(tmp_path / "j_R1.fastq.gz", joined, compress=False))
    assert look.ok and look.records == 6
    empty_first = gzip.compress(b"", mtime=0) + gzip.compress(fastq(reads=2), mtime=0)
    assert pairing.peek(put(tmp_path / "e_R1.fastq.gz", empty_first, compress=False)).records == 2
    many = b"".join(gzip.compress(fastq(reads=1, start=i), mtime=0) for i in range(3000))
    assert len(many) > pairing.PEEK_BYTES
    look = pairing.peek(put(tmp_path / "m_R1.fastq.gz", many, compress=False))
    assert look.ok and look.records > 100


def test_names_with_characters_that_only_look_like_digits(tmp_path):
    """fsutil.natural_key raises on "²" between two numbers; the search must not."""
    for odd in ("KP1²3", "KP1①2", "x1²"):
        with pytest.raises(ValueError):
            fsutil.natural_key(odd)            # if this is ever mended, _natural can go
    pair(tmp_path / "x1²", "KP1²3")
    pair(tmp_path / "x1²", "KP2")
    pair(tmp_path / "x1²", "KP10")
    put(tmp_path / "x1²" / "old1①2_R1.fastq.bz2", b"BZh9")
    result = pairing.scan(str(tmp_path))
    assert [s.name for s in result.samples] == ["KP1²3", "KP2", "KP10"]
    assert all(s.ready for s in result.samples) and len(result.ignored) == 1
    for text in ("KP_2", "kp_10", "a1b22c", "", "٣", "x٣y"):
        assert pairing._natural(text) == fsutil.natural_key(text)
    assert sorted(["b10", "b9", "B1²", "a"], key=pairing._natural) == ["a", "B1²", "b9", "b10"]
    twins = [Sample("KP1²3", [("/a/x_R1.fq", "/a/x_R2.fq")]), Sample("kp1²3", [("/b/x_R1.fq", "/b/x_R2.fq")])]
    assert len(pairing.mark_name_clashes(twins)) == 2 and "KP1²3" in twins[0].note


def test_files_a_mac_leaves_behind_are_not_samples(tmp_path):
    a, b = pair(tmp_path, "KP")
    for name in ("._KP_R1.fastq.gz", "._KP_R2.fastq.gz", ".DS_Store", ".hidden_R1.fastq.gz"):
        put(tmp_path / name, b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\0" * 64, compress=False)
    result = pairing.scan(str(tmp_path))
    assert [(s.name, s.lanes, s.state) for s in result.samples] == [("KP", [(a, b)], SampleState.READY)]
    assert result.other_files == 4 and result.fastq_files == 2 and result.ignored == []
    assert result.summary() == "Found 1 sample (2 files). Not added: 4 other files."
    merged = pairing.merge([], result)
    assert [s.ready for s in merged.samples] == [True]          # no second "KP" to clash with
    # One file given by itself still finds its real partner, not the Mac's copy.
    assert pairing.scan([a]).samples[0].lanes == [(a, b)]
    # Pointed at on purpose, such a file is looked at like any other and found wanting.
    chosen = pairing.scan([str(tmp_path / "._KP_R1.fastq.gz")])
    assert len(chosen.samples) == 1
    assert pairing.check_sample(chosen.samples[0]).state is SampleState.LEFT_OUT


def test_scan_takes_paths_of_every_kind(tmp_path, monkeypatch):
    a, b = pair(tmp_path / "run [1] é 日本", "KP")
    for given in (str(tmp_path), tmp_path, os.fsencode(str(tmp_path)), [tmp_path], (str(tmp_path),),
                  iter([str(tmp_path)]), [str(tmp_path) + os.sep], [a, os.fsencode(b)]):
        assert pairing.scan(given).samples[0].lanes == [(a, b)], given
    monkeypatch.chdir(tmp_path)
    relative = pairing.scan([os.path.join("run [1] é 日本", "KP_R1.fastq.gz")])
    assert relative.samples[0].lanes == [(a, b)]                # absolute, whatever was given


ODD_STEMS = ["-KP", "--help", "%TEMP%", "KP;rm", "KP&x", "KP'q", "KP,1", "KP=1", "KP~1", "KP#1", "KP@x",
             "KP+1", "KP!", "KP{1}", "KP^1", "KP`x`", "Ωmega", "名前", "é" * 70, "🧬KP", "KP​X", "a" * 200]
if os.name != "nt":
    ODD_STEMS += ["KP\nB", "KP\tB", 'KP"q"', "KP<1>", "KP|x", "KP?", "KP*", "KP:1", "KP\\x", "COM1", "nul"]


def test_no_file_name_stops_the_search_or_gives_an_unsafe_sample_name(tmp_path):
    made = 0
    for number, stem in enumerate(ODD_STEMS):
        folder = tmp_path / ("f%d" % number)
        try:
            pair(folder, stem)
            made += 1
        except OSError:
            continue                                            # this file system refuses the name
    assert made > 15
    result = pairing.scan(str(tmp_path))
    assert len(result.samples) == made and result.ignored == []
    from tesseract_win import settings_map
    for sample in result.samples:
        assert sample.ready, (sample.name, sample.note)
        assert fsutil.safe_name(sample.name) == sample.name and 0 < len(sample.name) <= 60
        assert not set(sample.name) & set('<>:"/\\|?* \t\n')
        assert pairing.name_problem([], sample, sample.name) == "", sample.name
        for trimmed in settings_map.trimmed_names(sample.name):
            assert os.path.basename(trimmed) == trimmed and pairing._parse_name(trimmed).own_trimmed
        assert Sample.from_dict(sample.to_dict()) == sample
    merged = pairing.merge([], result)
    for sample in merged.samples:
        pairing.check_sample(sample)
    pairing.mark_name_clashes(merged.samples)
    renamed = pairing.make_names_different(merged.samples)
    names = [s.name.casefold() for s in merged.samples]
    assert len(set(names)) == len(names), renamed
    if os.name != "nt":    # Windows itself refuses or alters a few of these names; such a sample is Left out with a reason
        assert all(s.ready for s in merged.samples), renamed
    assert len(pairing.longest_result_path(str(tmp_path / "out"), merged.samples)) > 0


def test_a_search_stopped_at_any_moment_never_shows_half_a_sample(tmp_path):
    """Stop looking, pressed after any number of folders: what is shown is whole or absent."""
    for number in range(6):
        pair(tmp_path / ("run%d" % number) / "deep", "KP_%d" % number)
        lane_files(tmp_path / ("run%d" % number), "NS_%d" % number, [1, 2, 3])
    full = pairing.scan(str(tmp_path))
    assert len(full.samples) == 12 and all(s.ready for s in full.samples)
    whole = {s.name: s.lanes for s in full.samples}
    seen_counts = set()
    for allowed in range(0, 30):
        calls = []

        def stop(calls=calls, allowed=allowed):
            calls.append(1)
            return len(calls) > allowed

        part = pairing.scan([str(tmp_path), str(tmp_path / "run0")], should_stop=stop)
        seen_counts.add(len(part.samples))
        for sample in part.samples:
            assert sample.ready and sample.lanes == whole[sample.name]      # never a lane or a partner short
        if part.stopped:
            assert "stopped early" in part.summary()
        else:
            assert len(part.samples) == 12
    assert 0 in seen_counts and 12 in seen_counts and len(seen_counts) > 4


def test_files_that_change_between_the_search_and_the_look_inside(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "TINY_PAIR_BYTES", 0)
    a, b = pair(tmp_path, "KP")
    c, d = pair(tmp_path, "GONE")
    e, f = pair(tmp_path, "GROWS")
    rows = by_name(pairing.merge([], pairing.scan(str(tmp_path))))
    assert all(row.ready for row in rows.values())
    os.remove(d)                                               # the stick was pulled, the file deleted
    with open(b, "wb"):
        pass                                                   # a copy that has just begun again
    with open(e, "ab") as handle:
        handle.write(gzip.compress(fastq(reads=50, start=4), mtime=0))
    before = rows["GROWS"].size_bytes
    for row in rows.values():
        pairing.check_sample(row)
    assert rows["KP"].state is SampleState.LEFT_OUT and rows["KP"].note == "Read 2: This file is empty."
    assert rows["GONE"].state is SampleState.LEFT_OUT and rows["GONE"].note.startswith("Read 2: File not found.")
    assert rows["GROWS"].ready and rows["GROWS"].size_bytes == os.path.getsize(e) + os.path.getsize(f) > before
    # The file comes back: the same check makes the row Ready again, by itself.
    put(d, mate=2)
    put(b, mate=2)
    for row in rows.values():
        pairing.check_sample(row)
    assert all(row.state is SampleState.READY for row in rows.values())
    # A folder that vanishes while it is searched is passed over, not an error.
    real_scandir = os.scandir

    def vanishing(path):
        if os.path.basename(str(path)) == "sub":
            raise FileNotFoundError(2, "gone", str(path))
        return real_scandir(path)

    pair(tmp_path / "sub", "LATE")
    monkeypatch.setattr(os, "scandir", vanishing)
    result = pairing.scan(str(tmp_path))
    assert sorted(s.name for s in result.samples) == ["GONE", "GROWS", "KP"]
    assert [i.kind for i in result.ignored] == ["unreadable"]


def test_check_sample_never_opens_files_that_are_stored_online_only(tmp_path, monkeypatch):
    lanes = lane_files(tmp_path, "CLOUD", [1, 2, 3])
    sample = pairing.scan(str(tmp_path)).samples[0]
    cloud = {path for lane in lanes for path in lane}
    real_open = builtins.open

    def guarded_open(name, *args, **kwargs):
        if str(name) in cloud:
            raise AssertionError("an online-only file was opened: %s" % name)
        return real_open(name, *args, **kwargs)

    asked = []
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(gzip, "open", guarded_open)
    state, note = pairing.assess_sample(sample, lambda path: asked.append(path) or True)
    assert state is SampleState.LEFT_OUT and note.startswith("These files are stored online only.")
    assert sorted(asked) == sorted(cloud)                      # each file asked about once, none opened
    # Only one of the six is online only: it is named, and it alone is not opened.
    cloud = {lanes[1][1]}
    state, note = pairing.assess_sample(sample, lambda path: path in cloud)
    assert (state, note) == (SampleState.LEFT_OUT, "Lane 2, Read 2: " + pairing._PEEK_SENTENCES[
        PeekStatus.ONLINE_ONLY])


def test_merge_a_sample_found_with_more_lanes_takes_the_place_of_its_part(tmp_path):
    """One lane added by hand, then the folder: one row with all the lanes, not two rows that
    share files (which "Make the names different" would then have sent to be assembled twice)."""
    lanes = lane_files(tmp_path, "KP", [1, 2, 3])
    pair(tmp_path, "AA")
    first = pairing.merge([], pairing.scan(list(lanes[0])))
    assert [(s.name, len(s.lanes), s.ready) for s in first.samples] == [("KP", 1, True)]
    assert pairing.rename(first.samples, first.samples[0], "Isolate_7") == ""
    first.samples[0].trim = False
    second = pairing.merge(first.samples, pairing.scan(str(tmp_path)))
    assert [(s.name, len(s.lanes), s.ready) for s in second.samples] == [("Isolate_7", 3, True), ("AA", 1, True)]
    assert second.samples[0].lanes == lanes and second.samples[0].trim is False     # the user's choices stay
    assert [len(s.lanes) for s in second.replaced] == [1] and len(second.added) == 2
    assert pairing.make_names_different(second.samples) == []
    used = [path for s in second.samples for lane in s.lanes for path in lane]
    assert len(used) == len(set(used))                          # no read file in two rows
    # The other way round is a duplicate, as before.
    third = pairing.merge(second.samples, pairing.scan(list(lanes[2])))
    assert third.added == [] and third.duplicate_files == 2 and len(third.samples) == 2


def test_merge_several_parts_become_one_row(tmp_path):
    lanes = lane_files(tmp_path, "KP", [1, 2, 3, 4])
    rows = pairing.merge([], pairing.scan(list(lanes[0]))).samples
    rows = pairing.merge(rows, pairing.scan(list(lanes[2]))).samples
    pair(tmp_path / "other", "ZZ")
    rows = pairing.merge(rows, pairing.scan(str(tmp_path / "other"))).samples
    assert [(s.name, len(s.lanes)) for s in rows] == [("KP", 1), ("KP", 1), ("ZZ", 1)]
    assert all(pairing.is_name_clash(s) for s in rows[:2])      # lane 1 and lane 3, added apart
    merged = pairing.merge(rows, pairing.scan(str(tmp_path)))
    assert [(s.name, len(s.lanes), s.state) for s in merged.samples] == [
        ("KP", 4, SampleState.READY), ("ZZ", 1, SampleState.READY)]
    assert merged.samples[0].lanes == lanes and len(merged.replaced) == 2
    assert len(rows) == 3                                       # the caller's list is as it was
    # A row left out for a reason of its own gives the new row nothing: not its Trim tick.
    lone = put(tmp_path / "solo" / "Q_R1.fastq.gz")
    half = pairing.merge([], pairing.scan([lone])).samples
    half[0].trim = False
    put(tmp_path / "solo" / "Q_R2.fastq.gz", mate=2)
    done = pairing.merge(half, pairing.scan(str(tmp_path / "solo")))
    assert [(s.name, s.ready, s.trim) for s in done.samples] == [("Q", True, True)]


def test_merge_rows_that_share_lanes_become_one_row(tmp_path):
    lanes = lane_files(tmp_path, "KP", [1, 2, 3, 4])
    rows = pairing.merge([], pairing.scan(list(lanes[0]) + list(lanes[1]))).samples      # lanes 1 and 2
    assert [len(s.lanes) for s in rows] == [2]
    rows[0].trim = False
    merged = pairing.merge(rows, pairing.scan(list(lanes[1]) + list(lanes[3])))          # lanes 2 and 4
    assert [(s.name, s.ready, s.trim) for s in merged.samples] == [("KP", True, False)]
    assert merged.samples[0].lanes == [lanes[0], lanes[1], lanes[3]]                     # in lane order
    assert merged.samples[0].size_bytes == sum(os.path.getsize(p) for i in (0, 1, 3) for p in lanes[i])
    assert merged.added == merged.samples and merged.replaced == rows and merged.duplicates == []
    assert rows[0].lanes == [lanes[0], lanes[1]]                 # the caller's row is as it was
    # A lane that was one file short is made whole by a row that has the other file.
    os.rename(lanes[2][1], lanes[2][1] + ".part")
    short = pairing.merge([], pairing.scan([lanes[0][0], lanes[2][0]])).samples
    assert short[0].note == "Lane 3 has no Read 2 file." and len(short[0].lanes) == 2
    os.rename(lanes[2][1] + ".part", lanes[2][1])
    whole = pairing.merge(short, pairing.scan([lanes[2][1], lanes[3][0]]))
    assert [(s.lanes, s.ready) for s in whole.samples] == [([lanes[0], lanes[2], lanes[3]], True)]
    assert pairing.check_sample(whole.samples[0]).ready


def test_merge_two_files_for_one_place_are_not_made_one_row(tmp_path):
    a, b = pair(tmp_path, "KP")
    other = put(tmp_path / "KP_R2.fq.gz", mate=2)                # a second candidate for Read 2
    rows = pairing.merge([], pairing.scan([a, b])).samples
    merged = pairing.merge(rows, pairing.scan([a, other]))
    assert [s.lanes for s in merged.samples] == [[(a, b)], [(a, other)]]
    assert all(pairing.is_name_clash(s) for s in merged.samples)   # the user decides which is meant
    assert merged.replaced == [] and len(merged.added) == 1
    # A row that is wholly part of the found sample gives way even then.
    lone = Sample("KP", [("", other)], state=SampleState.LEFT_OUT, note=pairing.NO_READ1)
    merged = pairing.merge(rows + [lone], pairing.scan([a, other]))
    assert [s.lanes for s in merged.samples] == [[(a, b)], [(a, other)]] and merged.replaced == [lone]


def test_merge_with_windows_paths(monkeypatch):
    """Capital letters and the direction of the slashes do not make two files of one."""
    monkeypatch.setattr(os, "path", ntpath)
    folder = "D:\\Séquençage 2026\\run [1]\\"

    def lane(number, spelling=str):
        return tuple(spelling(folder + "KP_S1_L00%d_R%d_001.fastq.gz" % (number, read)) for read in (1, 2))

    listed = Sample("Isolate", [lane(1), lane(2)], trim=False, size_bytes=10)
    again = Sample("KP", [lane(2, str.upper), lane(3, lambda p: p.replace("\\", "/"))], size_bytes=10)
    merged = pairing.merge([listed], [again])
    assert len(merged.samples) == 1 and merged.replaced == [listed]
    row = merged.samples[0]
    assert (row.name, row.trim, row.ready) == ("Isolate", False, True)
    assert [ntpath.basename(a).upper() for a, _ in row.lanes] == [
        "KP_S1_L00%d_R1_001.FASTQ.GZ" % number for number in (1, 2, 3)]
    assert pairing.merge(merged.samples, [Sample("KP", [lane(3, str.lower)])]).duplicate_files == 2
    assert pairing._natural("D:\\run²\\KP_10_R1.fastq.gz") > pairing._natural("D:\\run²\\KP_9_R1.fastq.gz")


def test_a_path_with_a_damaged_character_is_left_out_not_saved(tmp_path):
    """Such a path cannot be written to last_run.json (UTF-8): it would stop the whole batch."""
    broken = "D:\\run\\KP\udcff_R%d.fastq.gz"                 # as Windows hands over half a character
    row = Sample("KP", [(broken % 1, broken % 2)])
    with pytest.raises(UnicodeError):
        fsutil.atomic_write_json(str(tmp_path / "last_run.json"), row.to_dict())
    assert pairing.assess_sample(row) == (SampleState.LEFT_OUT, pairing.BROKEN_NAME)
    assert pairing.check_sample(row).state is SampleState.LEFT_OUT
    assert "Rename" in pairing.BROKEN_NAME and "plain letters" in pairing.BROKEN_NAME
    good = Sample("OK", [("D:\\run\\é 日本 🧬_R1.fastq.gz", "D:\\run\\é 日本 🧬_R2.fastq.gz")])
    assert pairing._static_problem(good) == ""
    # The same from a real folder, where the file system allows such a name.
    names = [os.fsencode(str(tmp_path)) + b"/KP\xff_R%d.fastq.gz" % read for read in (1, 2)]
    try:
        for read, name in enumerate(names, start=1):
            with open(name, "wb") as handle:
                handle.write(gzip.compress(fastq(mate=read), mtime=0))
    except (OSError, UnicodeError):
        return                                                    # not possible here (Windows, macOS)
    pair(tmp_path, "GOOD")
    result = pairing.scan(str(tmp_path))
    states = {s.name: (s.state, s.note) for s in result.samples}
    assert states["GOOD"] == (SampleState.READY, "") and len(states) == 2
    assert (SampleState.LEFT_OUT, pairing.BROKEN_NAME) in states.values()
    merged = pairing.merge([], result)
    for row in merged.samples:
        pairing.check_sample(row)
    pairing.mark_name_clashes(merged.samples)
    ready = [row for row in merged.samples if row.ready]
    assert [row.name for row in ready] == ["GOOD"]
    fsutil.atomic_write_json(str(tmp_path / "last_run.json"), [row.to_dict() for row in ready])


def test_peek_opens_regular_files_only(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no named pipes here")
    pipe = str(tmp_path / "pipe_R1.fastq")
    os.mkfifo(pipe)                                               # opening it would wait for ever
    look = pairing.peek(pipe)
    assert look.status is PeekStatus.NOT_FASTQ and look.gzip_content is None
    assert pairing.assess_sample(Sample("P", [(pipe, pipe)]))[0] is SampleState.LEFT_OUT


def test_rename_never_raises_whatever_is_typed(tmp_path):
    pair(tmp_path, "A")
    pair(tmp_path, "B")
    rows = pairing.scan(str(tmp_path)).samples
    for typed in (None, 7, b"X", ["X"], "", " ", "\t", "\x00", "A" * 61, "B", "b", "CON", "aux.txt",
                  "a/b", "a\\b", "a:b", "a b", ".x", "x.", "_x", "x_", "assemblies", "LAST_RUN.JSON",
                  "é" * 61, " ", "x\ny"):
        answer = pairing.rename(rows, rows[0], typed)
        assert isinstance(answer, str) and answer.endswith((".", "*")) and rows[0].name == "A", typed
    for typed in ("C", "é-日本.1", "x" * 60, "a", " D "):
        assert pairing.rename(rows, rows[0], typed) == "" and rows[0].name == typed.strip()
        assert fsutil.safe_name(rows[0].name) == rows[0].name


def test_a_hundred_samples_stay_quick_and_the_list_does_not_grow(tmp_path):
    import time
    for number in range(100):
        lane_files(tmp_path / ("run%d" % (number % 4)), "KP_%03d" % number, [1, 2], s=number + 1)
    started = time.monotonic()
    rows = pairing.merge([], pairing.scan(str(tmp_path))).samples
    for row in rows:
        pairing.check_sample(row)
    pairing.mark_name_clashes(rows)
    assert len(rows) == 100 and all(row.ready for row in rows)
    for _ in range(5):                                          # the same folder dropped again and again
        again = pairing.merge(rows, pairing.scan(str(tmp_path)))
        assert again.added == [] and again.duplicate_files == 400 and len(again.samples) == 100
        rows = again.samples
    for row in rows[:20]:
        assert pairing.name_problem(rows, row, row.name + "_b") == ""
    assert time.monotonic() - started < 20
    assert pairing.estimate_disk_need(rows, True) > pairing.estimate_disk_need(rows, False) > 2 * GB
