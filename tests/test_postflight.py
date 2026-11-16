# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Unit tests of ``tesseract_win.postflight``.

The byte-for-byte comparison with the bash script is in ``test_wrapper_parity.py``. Here
each rule is tested on an output folder small enough to check by hand.
"""

from __future__ import annotations

import os

import pytest

from tesseract_win import postflight
from tesseract_win.postflight import (contig_statistics, detected_organism, keep_first_attempt, summarise,
                                      thin_retry_needed)

LOG = ("[1/7] loading reads\n      600 reads, 90,000 bases, max length 150, paired  [mode: standard]\n\n"
       "  contigs      %d\n  total length %s bp\n  k-mer depth  %sx (k=%d)\n  elapsed      0.2s   peak memory 100.00 MB\n")
SELF_QA = ("k\t31\ncompleteness\t%s\nqv_read0\t%s\nmissing_bp_est_gt10x\t%s\nlost_replicon_alarm\t%s\n")


def report(call="none", circular=1, linear=0, spike=0, verdict="pass", version=True, detect=True) -> str:
    p2 = ('"r2": {"enabled": 1, "pairs": 300, "verdict": "%s", "max_outward_frac": 0.5}, '
          '"r3": {"candidates": %d, "written_circular": %d, "written_linear": %d}, '
          '"f5": {"screened": 1, "spikein": %d, "spikein_bp": %d}' % (verdict, circular + linear, circular, linear,
                                                                     spike, spike * 5386))
    if detect:
        p2 += (', "organism_detect": {"mode": "report_only", "models": [{"name": "ecoli", "score": 0.98}], '
               '"best": "ecoli", "accepted": true, "call": "%s", "mixture": false}' % call)
    return ('{\n    "tool": "TesserACT",\n%s    "mode": "standard",\n    "p2": {%s},\n    "assembly": {\n'
            '        "contigs": 3\n    }\n}\n' % ('    "version": "1.6.2",\n' if version else "", p2))


def fasta(records, width=60, eol="\n") -> str:
    out = []
    for name, seq in records:
        out.append(">" + name + eol)
        out.extend(seq[i:i + width] + eol for i in range(0, len(seq), width))
    return "".join(out)


CONTIGS = [("NODE_1_length_500_cov_23.5", "ACGT" * 125), ("NODE_2_length_300_cov_22.1", "GGCA" * 75),
           ("NODE_3_length_200_cov_24.8", "ttga" * 50)]


def make(folder, **files) -> str:
    """An output folder as the engine leaves it; a value of None leaves that file out."""
    content = {"contigs.fasta": fasta(CONTIGS), "report.json": report(), "assembly_graph.gfa": "H\tVN:Z:1.0\n",
               "p2_self_qa.tsv": SELF_QA % ("0.99871", "47.13", "0", "0"),
               "assembly.log": LOG % (3, "1,000", "23.9", 99),
               "preflight.txt": "TesserACT 1.6.2 preflight: a + b\n  WARN   depth                  thin\n"
                                "preflight: 0 failed, 1 warnings\n"}
    content.update({name.replace("__", "."): value for name, value in files.items()})
    os.makedirs(str(folder), exist_ok=True)
    for name, value in content.items():
        if value is not None:
            with open(os.path.join(str(folder), name), "wb") as handle:
                handle.write(value if isinstance(value, bytes) else value.encode("utf-8"))
    return str(folder)


def run(folder, wording="upstream", **options):
    options.setdefault("read1", "reads/s_R1.fastq.gz")
    options.setdefault("read2", "reads/s_R2.fastq.gz")
    options.setdefault("exit_code", 0)
    options.setdefault("engine_version", "TesserACT 1.6.2")
    options.setdefault("shown_out", "assembly")
    return summarise(folder, wording=wording, **options)


def line(result, name: str):
    found = [entry for entry in result.lines if entry.name == name]
    assert len(found) == 1, "%d lines named %s" % (len(found), name)
    return found[0]


def names(result):
    return [(entry.verdict, entry.name) for entry in result.lines]


# --------------------------------------------------------------------------- the profile

def test_profile_values():
    assert dict(postflight.PROFILE_ENV) == {
        "TESSERACT_P2_LIBGUARD": "1", "TESSERACT_P2_CIRC": "verify", "TESSERACT_P2_SPIKEIN": "1",
        "TESSERACT_P2_ENDS": "1", "TESSERACT_P2_TIPS_LOWERCASE": "1", "TESSERACT_P2_SELF_QA": "1",
        "TESSERACT_P2_PROVENANCE": "1", "TESSERACT_DEEP_NORM": "30", "TESSERACT_DEEP_NORM_MATE": "both"}
    assert len(postflight.PROFILE_ENV) == 9
    assert (postflight.DETECT_REPORT_ENV, postflight.DETECT_SKETCH_ENV) == (
        "TESSERACT_P2_DETECT_REPORT", "TESSERACT_P2_DETECT_SKETCH")
    assert dict(postflight.detect_env("/opt/x/om2detect.sketch")) == {
        "TESSERACT_P2_DETECT_REPORT": "1", "TESSERACT_P2_DETECT_SKETCH": "/opt/x/om2detect.sketch"}
    assert postflight.RETRY_ARGS == ("-k", "21,33,55") and postflight.FIRST_ATTEMPT_DIR == "attempt1_default_k"


def test_engine_version_line():
    assert postflight.engine_version_line("TesserACT 1.6.2\nGiovanni Lorenzin, IOWA-BioTech\n") == "TesserACT 1.6.2"
    assert postflight.engine_version_line(b"build info\r\nTesserACT 1.7.0 (windows)\r\nmore\r\n") == "TesserACT 1.7.0 (windows)"
    assert postflight.engine_version_line("no such word\n") == ""
    assert postflight.engine_version_line(b"") == ""


# --------------------------------------------------------------------------- the retry rule

def empty_first_attempt(folder, depth="0.0", contigs=""):
    return make(folder, contigs__fasta=contigs, assembly__log=LOG % (0, "0", depth, 99))


def test_retry_when_almost_nothing_was_assembled_and_the_log_shows_no_kmer_depth(tmp_path):
    decision = thin_retry_needed(empty_first_attempt(tmp_path))
    assert decision and decision.needed
    assert (decision.assembled_bp, decision.wanted_bp, decision.kmer_depth) == (0, 1000000, "0.0")
    assert decision.message == "the assembly holds only 0 bp: retrying with a short k ladder (21,33,55) for thin data ..."


@pytest.mark.parametrize("depth, retry", [("0.0", True), ("1.9", True), ("1.99", True), ("2", False), ("2.0", False),
                                          ("23.9", False), ("0", True), (".5", True), ("1.", True)])
def test_retry_needs_a_kmer_depth_below_2(tmp_path, depth, retry):
    decision = thin_retry_needed(empty_first_attempt(tmp_path, depth, fasta([("c", "ACGT" * 100)])))
    assert bool(decision) is retry
    assert decision.assembled_bp == 400 and decision.kmer_depth == depth
    assert (decision.message != "") is retry
    if retry:
        assert decision.message.startswith("the assembly holds only 400 bp: ")


def test_retry_looks_at_the_last_kmer_depth_line(tmp_path):
    first_low = "  k-mer depth  0.0x (k=77)\n" + LOG % (1, "400", "2.0", 99)
    first_high = "  k-mer depth  7.5x (k=77)\n" + LOG % (1, "400", "1.9", 99)
    assert not thin_retry_needed(make(tmp_path / "a", contigs__fasta="", assembly__log=first_low))
    assert thin_retry_needed(make(tmp_path / "b", contigs__fasta="", assembly__log=first_high))
    same_line = "k-mer depth 9.9x then k-mer depth 0.5x\n"
    assert thin_retry_needed(make(tmp_path / "c", contigs__fasta="", assembly__log=same_line)).kmer_depth == "0.5"


@pytest.mark.parametrize("log", ["", "no such line\n", "k-mer depth  x (k=99)\n", "kmer depth 0.0x\n",
                                 "k-mer depth: 0.0x\n", "K-MER DEPTH 0.0x\n"])
def test_no_kmer_depth_in_the_log_counts_as_99(tmp_path, log):
    decision = thin_retry_needed(make(tmp_path, contigs__fasta="", assembly__log=log))
    assert not decision and decision.kmer_depth is None
    assert not thin_retry_needed(make(tmp_path, contigs__fasta="", assembly__log=None))


def test_a_kmer_depth_that_is_not_a_number_is_compared_as_text(tmp_path):
    """``1.2.3`` is not a number for awk, which then compares the texts: "1.2.3" < "2"."""
    assert thin_retry_needed(make(tmp_path / "a", contigs__fasta="", assembly__log="k-mer depth 1.2.3x\n"))
    assert not thin_retry_needed(make(tmp_path / "b", contigs__fasta="", assembly__log="k-mer depth 9.9.9x\n"))
    assert thin_retry_needed(make(tmp_path / "c", contigs__fasta="", assembly__log="k-mer depth .x\n"))


def test_retry_needs_less_than_a_fifth_of_the_genome(tmp_path):
    folder = make(tmp_path, contigs__fasta=fasta([("c", "A" * 400)]), assembly__log=LOG % (1, "400", "0.0", 99))
    assert thin_retry_needed(folder)                                       # 400 < 1,000,000 (5 Mb assumed)
    assert thin_retry_needed(folder, genome_size_mb="0.00201")             # 400 < int(402.0)
    assert not thin_retry_needed(folder, genome_size_mb="0.002")           # 400 < 400 is false
    assert thin_retry_needed(folder, genome_size_mb="0.002").wanted_bp == 400
    assert thin_retry_needed(folder, genome_size_mb=0.3).wanted_bp == 60000
    assert thin_retry_needed(folder, genome_size_mb="0.000004").wanted_bp == 0       # int(0.8) is 0
    assert not thin_retry_needed(folder, genome_size_mb="0.000004")


def test_retry_assumes_5_mb_when_the_read_statistics_did_not_run(tmp_path):
    """The script takes the genome size from its depth check; without that check it is 5 Mb."""
    folder = make(tmp_path, contigs__fasta=fasta([("c", "A" * 500)]), assembly__log=LOG % (1, "500", "1.9", 99))
    assert not thin_retry_needed(folder, genome_size_mb="0.002", stats_ran=True)
    decision = thin_retry_needed(folder, genome_size_mb="0.002", stats_ran=False)
    assert decision and decision.wanted_bp == 1000000


@pytest.mark.parametrize("args, retry", [
    ((), True), (("--mode", "careful"), True), (("-k", "21,33"), False), (("--kmers", "21,33"), False),
    (("--min-contig", "500", "-k", "21"), False), (("-k21,33",), True), (("--kmers=21,33",), True),
    (("--no-gfa",), True), (("-K",), True),
])
def test_no_retry_when_the_kmer_sizes_were_given(tmp_path, args, retry):
    assert bool(thin_retry_needed(empty_first_attempt(tmp_path), engine_args=args)) is retry


@pytest.mark.parametrize("status", [1, 2, 137, -9, 3221225477])
def test_no_retry_after_a_failed_engine(tmp_path, status):
    assert not thin_retry_needed(empty_first_attempt(tmp_path), exit_code=status)


def test_assembled_bases_are_counted_like_grep_tr_wc(tmp_path):
    text = ">c1 x\nACGT\nAC\n\n>c2\n>c3\nNNNNN\r\nlast line without end"
    decision = thin_retry_needed(make(tmp_path, contigs__fasta=text, assembly__log=LOG % (3, "x", "0.0", 99)))
    assert decision.assembled_bp == 4 + 2 + 0 + 6 + len("last line without end")       # the CR is counted
    assert thin_retry_needed(make(tmp_path / "none", contigs__fasta=None, assembly__log=LOG % (0, "0", "0.0", 99))).assembled_bp == 0


def test_keep_first_attempt_moves_everything_but_the_read_check(tmp_path):
    folder = empty_first_attempt(tmp_path / "assembly")
    os.makedirs(os.path.join(folder, "sub folder"))
    for name in (".hidden", os.path.join("sub folder", "inner.txt"), "report.html"):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            handle.write(name)
    before = sorted(name for name in os.listdir(folder) if name not in ("preflight.txt", ".hidden"))
    kept = keep_first_attempt(folder)
    assert kept == os.path.join(folder, "attempt1_default_k")
    assert sorted(os.listdir(folder)) == [".hidden", "attempt1_default_k", "preflight.txt"]
    assert sorted(os.listdir(kept)) == before
    with open(os.path.join(kept, "sub folder", "inner.txt"), encoding="utf-8") as handle:
        assert handle.read() == os.path.join("sub folder", "inner.txt")
    # a second engine run writes new files; putting them aside again replaces the old copies
    make(folder, contigs__fasta=">new\nAC\n", preflight__txt=None)
    os.makedirs(os.path.join(folder, "sub folder"))
    keep_first_attempt(folder)
    with open(os.path.join(kept, "contigs.fasta"), encoding="utf-8") as handle:
        assert handle.read() == ">new\nAC\n"
    assert os.listdir(os.path.join(kept, "sub folder")) == []
    assert sorted(os.listdir(folder)) == [".hidden", "attempt1_default_k", "preflight.txt"]


# --------------------------------------------------------------------------- organism

@pytest.mark.parametrize("call", sorted(postflight.ORGANISM_GENOME_MB))
def test_every_organism_the_engine_can_call(tmp_path, call):
    folder = make(tmp_path, report__json=report(call=call))
    assert detected_organism(folder) == call
    result = run(folder, wording="windows")
    assert (result.organism, result.organism_name) == (call, postflight.ORGANISM_NAMES[call])
    assert "  hint     the reads look like %s\n" % postflight.ORGANISM_NAMES[call] in result.text


def test_organism_is_found_the_way_the_script_finds_it(tmp_path):
    def found(text):
        return detected_organism(make(tmp_path, report__json=text))

    assert found(report(call="none")) == ""
    assert found(report(call="martian")) == ""
    assert found(report(detect=False)) == ""
    assert found("") == ""
    assert found('{"organism_detect":\n   {\n "best": "ecoli", "second": "senterica",\n "call" :\n "saureus" } }') == "saureus"
    assert found('{"call": "ecoli", "organism_detect": {"call": "none"}}') == ""          # only after the key
    assert found('{"organism_detect": {"x": {"call": "efaecium"}}, "other": {"call": "ecoli"}}') == "efaecium"
    assert found('{"organism_detect": {"call": "Ecoli"}}') == ""                           # lower case only
    assert found('{"organism_detect": {"call": ""}, "z": {"call": "ecoli"}}') == ""        # the first call counts
    assert found('{"organism_detect": null, "call": "ecoli"}') == ""
    assert found('this is not JSON "organism_detect": {"call":"ecoli"') == "ecoli"        # searched as text
    assert detected_organism(make(tmp_path / "none", report__json=None)) == ""
    assert detected_organism(str(tmp_path / "no such folder")) == ""


def test_no_organism_after_a_failed_engine(tmp_path):
    folder = make(tmp_path, report__json=report(call="ecoli"))
    assert run(folder, exit_code=1).organism == ""
    assert "hint" not in run(folder, exit_code=1).text
    assert run(folder).organism == "ecoli"


# --------------------------------------------------------------------------- contig statistics

def stats_of(tmp_path, text: str, **options):
    path = os.path.join(str(tmp_path), "c.fasta")
    with open(path, "wb") as handle:
        handle.write(text.encode("latin-1"))
    return contig_statistics(path, **options)


def test_contig_numbers(tmp_path):
    st = stats_of(tmp_path, fasta(CONTIGS))
    assert (st.contigs, st.total_length, st.largest, st.n50, st.l50, st.n90, st.l90) == (3, 1000, 500, 500, 1, 200, 3)
    assert st.well_formed
    assert st.rows() == [("contigs", 3), ("total_bp", 1000), ("largest", 500), ("N50", 500), ("L50", 1),
                         ("N90", 200), ("L90", 3)]


@pytest.mark.parametrize("lengths, n50, l50, n90, l90", [
    ([5000, 5000], 5000, 1, 5000, 2),               # 5000 of 10000 reaches the half: >=, not >
    ([4, 3, 2, 1], 3, 2, 2, 3),                     # 4+3 = 7 >= 5; 4+3+2 = 9 >= 9.0
    ([1, 2, 3, 4], 3, 2, 2, 3),                     # the order in the file does not matter
    ([10], 10, 1, 10, 1),
    ([9, 1], 9, 1, 9, 1),                           # 9 >= 10 * 0.9 exactly
    ([7, 7, 7], 7, 2, 7, 3),
    ([2500, 2500, 2500, 1500, 1000], 2500, 2, 1500, 4),
])
def test_n50_and_n90(tmp_path, lengths, n50, l50, n90, l90):
    st = stats_of(tmp_path, fasta([("c%d" % i, "A" * n) for i, n in enumerate(lengths)], width=7))
    assert (st.n50, st.l50, st.n90, st.l90) == (n50, l50, n90, l90)
    assert st.largest == max(lengths) and st.total_length == sum(lengths)


def test_contig_names_and_odd_records(tmp_path):
    st = stats_of(tmp_path, "ACGTACGT\n>\nAAAA\n>c2 real\nACGTAC\n> spaced name\nCCCCCCC\n>c9\tx\nGG\n")
    # sequence before the first header, a header that is ">" alone and "> name" are not records;
    # the two empty names count as one duplicate
    assert (st.contigs, st.total_length, st.largest) == (2, 8, 6)
    assert (st.duplicate_names, st.empty_records, st.bad_lines) == (1, 0, 0)
    st = stats_of(tmp_path, ">c1 first\nACGT\n>c1 second\nAC\n>c3\n>c4\nACRYAC\nACGT \nacgtn\n\nNNNN\n")
    assert (st.duplicate_names, st.empty_records, st.bad_lines) == (1, 1, 2)
    assert (st.contigs, st.total_length) == (4, 4 + 2 + 0 + 6 + 5 + 5 + 4)
    assert not st.well_formed
    st = stats_of(tmp_path, ">a\n>b\n")
    assert (st.contigs, st.total_length, st.n50, st.l50, st.n90, st.l90, st.empty_records) == (2, 0, 0, 2, 0, 2, 2)
    st = stats_of(tmp_path, "ACGT\n")
    assert (st.contigs, st.total_length, st.largest, st.n50, st.l50) == (0, 0, 0, 0, 0) and st.well_formed
    st = stats_of(tmp_path, ">last\nACGT")                          # no line end at the end
    assert (st.contigs, st.total_length) == (1, 4)


def test_cr_is_data_unless_asked_otherwise(tmp_path):
    text = fasta(CONTIGS, eol="\r\n")
    strict = stats_of(tmp_path, text)
    assert strict.bad_lines == 18 and strict.total_length == 1018 and not strict.well_formed
    lenient = stats_of(tmp_path, text, strip_cr=True)
    assert lenient == stats_of(tmp_path, fasta(CONTIGS))


# --------------------------------------------------------------------------- the summary

def test_summary_of_a_good_run_upstream(tmp_path):
    folder = make(tmp_path / "work" / "assembly")
    result = run(folder)
    assert result.text == (
        "TesserACT 1.6.2 run summary\n"
        "  reads    reads/s_R1.fastq.gz + reads/s_R2.fastq.gz\n"
        "  version  tesseract 1.6.2, engine TesserACT 1.6.2\n"
        "  settings standard (read-verified circles, library check, PhiX label, small-plasmid recovery, self-check)\n"
        "  preflight preflight: 0 failed, 1 warnings\n"
        "[vanilla assembly] assembly\n"
        "  OK     exit status            0\n"
        "  OK     scaffolds.fasta        not written: every scaffold join was closed with sequence, contigs.fasta is complete\n"
        "  OK     contigs.fasta          3 contigs, 1000 bp; largest 500; N50 500 (L50 1); N90 200 (L90 3)\n"
        "  OK     self-check             k-mer completeness 0.99871, QV 47.13; no high-copy sequence lost\n"
        "  OK     circles                1 replicons written circular (read-verified join), 0 circle candidates "
        "failed verification and were written linear\n"
        "  OK     map-back               skipped: install bwa or minimap2 to measure how many reads map back\n"
        "\n"
        "postflight: 0 failed, 0 warnings (details above; this text is in assembly/SUMMARY.txt)\n")
    assert (result.fails, result.warns, result.ok) == (0, 0, True)
    assert (result.organism, result.organism_name) == ("", "")
    assert result.contigs == postflight.ContigStats(3, 1000, 500, 500, 1, 200, 3)
    assert result.summary_path == os.path.join(folder, "SUMMARY.txt")
    assert result.stats_path == os.path.join(folder, "assembly_stats.tsv")
    with open(result.summary_path, "rb") as handle:
        assert handle.read() == result.text.encode("ascii") == result.data
    with open(result.stats_path, "rb") as handle:
        assert handle.read() == b"metric\tvalue\ncontigs\t3\ntotal_bp\t1000\nlargest\t500\nN50\t500\nL50\t1\nN90\t200\nL90\t3\n"
    assert sorted(os.listdir(folder)) == ["SUMMARY.txt", "assembly.log", "assembly_graph.gfa", "assembly_stats.tsv",
                                          "contigs.fasta", "p2_self_qa.tsv", "preflight.txt", "report.json"]


def test_windows_wording_of_a_good_run(tmp_path):
    folder = make(tmp_path, report__json=report(call="kpneumoniae"))
    upstream, windows = run(folder), run(folder, wording="windows")
    assert summarise(folder, read1="reads/s_R1.fastq.gz", read2="reads/s_R2.fastq.gz", exit_code=0,
                     engine_version="TesserACT 1.6.2", shown_out="assembly").text == windows.text     # the default
    theirs, ours = upstream.text.split("\n"), windows.text.split("\n")
    assert len(theirs) == len(ours)
    assert [(a, b) for a, b in zip(theirs, ours) if a != b] == [
        ("  OK     map-back               skipped: install bwa or minimap2 to measure how many reads map back",
         "  OK     map-back               not included in the Windows edition; the k-mer self-check above covers "
         "completeness"),
        ("  hint     the reads look like kpneumoniae, which has an organism model. To use it (your choice): tesseract "
         "models download kpneumoniae, then rerun with --organism kpneumoniae",
         "  hint     the reads look like Klebsiella pneumoniae"),
    ]
    assert names(upstream) == names(windows) and (upstream.fails, upstream.warns) == (windows.fails, windows.warns)
    assert upstream.contigs == windows.contigs
    for word in ("bwa", "minimap2", "model", "--organism", "download"):
        assert word not in windows.text
    with pytest.raises(ValueError):
        run(folder, wording="linux")


def test_windows_map_back_line_without_a_self_check(tmp_path):
    result = run(make(tmp_path, p2_self_qa__tsv=None), wording="windows")
    assert "self-check" not in [entry.name for entry in result.lines]
    assert line(result, "map-back").text == "  OK     map-back               not included in the Windows edition"
    assert line(run(make(tmp_path / "empty", p2_self_qa__tsv=""), wording="windows"), "map-back").message == (
        "not included in the Windows edition")


def test_failed_engine(tmp_path):
    folder = make(tmp_path, report__json=report(call="ecoli"))
    for status in (1, 3, 137):
        upstream = run(folder, exit_code=status)
        assert names(upstream) == [("FAIL", "exit status")]
        assert upstream.lines[0].message == "%d (see assembly.log)" % status          # the script's OUT.log
        assert upstream.text.endswith("\npostflight: 1 failed, 0 warnings (details above; this text is in "
                                      "assembly/SUMMARY.txt)\n")
        assert upstream.contigs is None and upstream.stats_path == "" and not upstream.ok
        windows = run(folder, wording="windows", exit_code=status)
        assert windows.lines[0].message == "%d (see assembly/assembly.log)" % status  # departure F4
    assert not os.path.exists(os.path.join(folder, "assembly_stats.tsv"))
    retried = run(folder, exit_code=1, retried=True)
    assert names(retried) == [("FAIL", "exit status"), ("WARN", "thin data")]


def test_missing_and_empty_files(tmp_path):
    result = run(make(tmp_path / "a", report__json=None, assembly_graph__gfa=None))
    assert [entry.text for entry in result.lines if entry.verdict == "FAIL"] == [
        "  FAIL   file                   report.json missing or empty",
        "  FAIL   file                   assembly_graph.gfa missing or empty",
        "  FAIL   report.json            unreadable"]
    assert result.contigs is not None and result.stats_path                       # the numbers are still written
    assert "circles" not in [entry.name for entry in result.lines]
    result = run(make(tmp_path / "b", assembly_graph__gfa=""), engine_args=("--mode", "fast", "--no-gfa"))
    assert result.fails == 0
    assert run(make(tmp_path / "b2", assembly_graph__gfa=None), engine_args=("--no-gfa=1",)).fails == 1
    for contigs in (None, ""):
        result = run(make(tmp_path / ("c%s" % (contigs is None)), contigs__fasta=contigs))
        assert names(result) == [("OK", "exit status"), ("FAIL", "file"), ("OK", "scaffolds.fasta")]
        assert result.lines[1].message == "contigs.fasta missing or empty"
        assert result.contigs is None and result.stats_path == ""
        assert not os.path.exists(os.path.join(os.path.dirname(result.summary_path), "assembly_stats.tsv"))


def test_report_without_version_is_unreadable(tmp_path):
    assert line(run(make(tmp_path / "a", report__json=report(version=False))), "report.json").text == (
        "  FAIL   report.json            unreadable")
    minimal = run(make(tmp_path / "b", report__json='{"version": "1.6.2"}\n', p2_self_qa__tsv=None))
    assert names(minimal) == [("OK", "exit status"), ("OK", "scaffolds.fasta"), ("OK", "contigs.fasta"),
                              ("OK", "map-back")]
    not_json = run(make(tmp_path / "c", report__json='garbage "version" garbage'))
    assert not_json.fails == 0
    # the key with its quotes is looked for, nothing looser and nothing stricter
    for text in ('{"tool_version": "1.6.2"}', '{"versions": ["1.6.2"]}', "{version: 1}", '{"Version": "1.6.2"}'):
        assert line(run(make(tmp_path / "d", report__json=text)), "report.json").verdict == "FAIL", text
    split = run(make(tmp_path / "e", report__json='{"ver\nsion": 1}'))        # newlines are taken out first
    assert "report.json" not in [entry.name for entry in split.lines]


def test_malformed_contigs_still_get_their_numbers_written(tmp_path):
    result = run(make(tmp_path, contigs__fasta=">c1\nACGT\n>c1\nACXT\n>c3\n"))
    assert line(result, "contigs.fasta").text == ("  FAIL   contigs.fasta          malformed: 1 duplicate names, "
                                                  "1 empty records, 1 lines with non-ACGTN characters")
    with open(result.stats_path, "rb") as handle:
        assert handle.read() == b"metric\tvalue\ncontigs\t3\ntotal_bp\t8\nlargest\t4\nN50\t4\nL50\t1\nN90\t4\nL90\t2\n"


def test_scaffolds_are_counted_by_lines_that_hold_a_greater_than_sign(tmp_path):
    result = run(make(tmp_path, scaffolds__fasta=">s1\nACGTNNNNACGT\n>s2 a>b\nAC\nx>y\n\n"))
    assert line(result, "scaffolds.fasta").message == "3 records (scaffolds with N gaps; scaffolds.agp lists them)"


@pytest.mark.parametrize("genome, warned, shown", [
    ("0.00125", False, ""),                 # ratio 0.8: the limit itself is fine
    ("0.0013", True, "0.0013"),             # ratio 0.769
    ("0.00077", False, ""),                 # ratio 1.2987
    ("0.00076", True, "0.00076"),           # ratio 1.3158
    ("0.001", False, ""), (0.001, False, ""), (None, False, ""), ("5", True, "5"), ("5.0", True, "5.0"),
])
def test_size_check_with_a_genome_size(tmp_path, genome, warned, shown):
    result = run(make(tmp_path), genome_size_mb=genome)
    sizes = [entry for entry in result.lines if entry.name == "size"]
    assert bool(sizes) is warned
    if warned:
        assert sizes[0].text == ("  WARN   size                   total 0.00 Mb vs ~%s Mb expected: contamination, a mixed "
                                 "sample, or the wrong genome size" % shown)
        assert result.warns == 1


def test_size_check_uses_the_called_organism_when_no_size_was_given(tmp_path):
    folder = make(tmp_path, report__json=report(call="saureus"))
    result = run(folder)
    assert line(result, "size").message.startswith("total 0.00 Mb vs ~2.8 Mb expected")
    given = run(folder, genome_size_mb="0.001")
    assert "size" not in [entry.name for entry in given.lines]            # what the user gave wins
    assert given.organism == "saureus"
    for bad in ("abc", "0", "-1", "5,5"):
        with pytest.raises(ValueError):
            run(folder, genome_size_mb=bad)


@pytest.mark.parametrize("lost, kb", [("2250", "2.2"), ("2350", "2.4"), ("2251", "2.3"), ("50", "0.1"), ("49", "0.0"),
                                      ("0", "0.0"), ("", "0.0"), ("12abc", "0.0"), ("1e3", "1.0"), ("150000", "150.0")])
def test_self_check_alarm_and_its_rounding(tmp_path, lost, kb):
    """2250 bases are 2.2 kb, not 2.3: "%.1f" rounds the double, ties to even."""
    result = run(make(tmp_path, p2_self_qa__tsv=SELF_QA % ("0.98765", "41.27", lost, "1")))
    assert line(result, "self-check").text == (
        "  WARN   self-check             k-mer completeness 0.98765, QV 41.27; about %s kb of sequence seen in the "
        "reads at >10x genome depth is not in the assembly (a small high-copy plasmid, rRNA or IS copies); see "
        "p2_self_qa.missing_hi.tsv" % kb)
    assert result.warns == 1 and result.ok


@pytest.mark.parametrize("alarm", ["0", "", "1.0", "01", " 1", "true", "1 "])
def test_only_the_exact_text_1_is_an_alarm(tmp_path, alarm):
    result = run(make(tmp_path, p2_self_qa__tsv=SELF_QA % ("0.9", "40", "2250", alarm)))
    assert line(result, "self-check").text == ("  OK     self-check             k-mer completeness 0.9, QV 40; no "
                                               "high-copy sequence lost")


def test_self_check_table_with_windows_line_ends(tmp_path):
    table = (SELF_QA % ("0.98765", "41.27", "2250", "1")).replace("\n", "\r\n")
    folder = make(tmp_path, p2_self_qa__tsv=table)
    upstream = line(run(folder), "self-check")                             # "1\r" is not "1" for the script
    assert upstream.verdict == "OK" and upstream.message == ("k-mer completeness 0.98765\r, QV 41.27\r; no high-copy "
                                                             "sequence lost")
    windows = line(run(folder, wording="windows"), "self-check")           # departure F5
    assert windows.verdict == "WARN" and "completeness 0.98765, QV 41.27; about 2.2 kb" in windows.message


def test_circles_spike_in_and_library(tmp_path):
    result = run(make(tmp_path / "a", report__json=report(circular=2, linear=1, spike=2, verdict="outward")))
    assert [entry.text for entry in result.lines[-4:]] == [
        "  OK     circles                2 replicons written circular (read-verified join), 1 circle candidates "
        "failed verification and were written linear",
        "  WARN   spike-in               2 PhiX record(s) labelled _spikein: remove them before submission",
        "  WARN   library                pair orientation check 'outward': not a standard FR library; pair-based "
        "joins were disabled",
        "  OK     map-back               skipped: install bwa or minimap2 to measure how many reads map back"]
    assert (result.fails, result.warns) == (0, 2)
    bare = run(make(tmp_path / "b", report__json='{"version": "1", "p2": {"r3": {"written_circular": 3}}}'))
    assert line(bare, "circles").message.startswith("3 replicons written circular (read-verified join), 0 circle")
    none = run(make(tmp_path / "c", report__json='{"version": "1", "written_circular": null, "spikein": 0}'))
    assert [entry.name for entry in none.lines if entry.name in ("circles", "spike-in", "library")] == []
    spaced = run(make(tmp_path / "d", report__json='{"version":"1","spikein":   007,"r2":{"a":1,"verdict":"inward"}}'))
    assert line(spaced, "spike-in").message.startswith("007 PhiX record(s)")
    assert line(spaced, "library").message.startswith("pair orientation check 'inward'")


def test_library_verdict_is_the_one_of_the_r2_block(tmp_path):
    def library(text):
        result = run(make(tmp_path, report__json='{"version": "1.6.2", "p2": {%s}}' % text))
        return [entry.message for entry in result.lines if entry.name == "library"]

    assert library('"x": {"verdict": "outward"}, "r2": {"enabled": 1, "verdict": "pass"}') == []
    assert library('"r2": {"enabled": 1}, "self_qa": {"verdict": "outward"}') == []        # not across a "}"
    assert library('"r2": {"verdict": "pass"}, "x": {"verdict": "inward"}') == []
    assert library('"r2": {"verdict": "no alarm"}') == []                                  # lower-case letters only
    assert library('"r2": {"verdict": ""}') == []
    assert library('"r2": {"verdict": "inward", "verdict": "outward"}') == [
        "pair orientation check 'outward': not a standard FR library; pair-based joins were disabled"]
    # every match is taken, one per line, as grep -o gives them to the script
    assert library('"r2": {"verdict": "pass"}, "again": {"r2": {"verdict": "pass"}}') == [
        "pair orientation check 'pass\npass': not a standard FR library; pair-based joins were disabled"]


def test_facts_are_the_numbers_of_the_lines(tmp_path):
    good = run(make(tmp_path / "a")).facts
    assert good == postflight.RunFacts(completeness="0.99871", qv="47.13", circular=1, circles_failed=0,
                                       library_verdict="pass")
    assert (good.lost_kb, good.scaffold_records, good.expected_genome_mb, good.size_warned) == (None, None, "", False)
    busy = run(make(tmp_path / "b", report__json=report(call="saureus", circular=2, linear=1, spike=3, verdict="outward"),
                    p2_self_qa__tsv=SELF_QA % ("0.98765", "41.27", "2250", "1"), assembly_graph__gfa=None,
                    scaffolds__fasta=">s\nACNNGT\n"), retried=True).facts
    assert busy == postflight.RunFacts(
        retried=True, missing_files=("assembly_graph.gfa",), scaffold_records=1, expected_genome_mb="2.8",
        size_warned=True, completeness="0.98765", qv="41.27", lost_kb=2.25, circular=2, circles_failed=1,
        spikein_records=3, library_verdict="outward")
    given = run(make(tmp_path / "c"), genome_size_mb="0.001").facts
    assert (given.expected_genome_mb, given.size_warned) == ("0.001", False)
    failed = run(make(tmp_path / "d"), exit_code=3).facts
    assert failed == postflight.RunFacts(exit_code=3)
    bare = run(make(tmp_path / "e", report__json="{}", p2_self_qa__tsv=None, contigs__fasta="")).facts
    assert bare == postflight.RunFacts(missing_files=("contigs.fasta",))
    unreadable = run(make(tmp_path / "f", report__json="{}", p2_self_qa__tsv=None)).facts
    assert unreadable == postflight.RunFacts(report_readable=False)


def test_thin_data_warning_and_its_place(tmp_path):
    result = run(make(tmp_path, report__json=report(call="ecoli")), retried=True, wording="windows")
    assert result.lines[-1].text == (
        "  WARN   thin data              the default k ladder assembled almost nothing; this result uses k = 21,33,55 "
        "(the first attempt is in attempt1_default_k/). Expect a fragmented assembly: sequence more deeply if you can")
    tail = result.text.split("\n")[-6:]
    assert tail[0].startswith("  OK     map-back") and tail[1].startswith("  WARN   thin data")
    assert tail[2] == "  hint     the reads look like Escherichia coli" and tail[3] == ""
    assert tail[4] == "postflight: 0 failed, 2 warnings (details above; this text is in assembly/SUMMARY.txt)"


def test_the_preflight_line_is_the_last_line_of_preflight_txt(tmp_path):
    def shown(content, **options):
        result = run(make(tmp_path, preflight__txt=content), **options)
        return result.text.split("\n")[4]

    assert shown("x\npreflight: 2 failed, 3 warnings\n") == "  preflight preflight: 2 failed, 3 warnings"
    assert shown("x\nno line end") == "  preflight no line end"
    assert shown("x\n\n") == "  preflight "
    assert shown("") == "  preflight "
    assert shown("ignored\n", preflight_line="preflight: 0 failed, 0 warnings") == "  preflight preflight: 0 failed, 0 warnings"
    os.remove(os.path.join(str(tmp_path), "preflight.txt"))
    assert run(str(tmp_path)).text.split("\n")[4] == "  preflight "


def test_paths_are_printed_as_given(tmp_path):
    folder = make(tmp_path / "Résultats [1]" / "assembly")
    result = summarise(folder, read1="C:\\Reads é\\s_R1.fastq.gz", read2="C:\\Reads é\\s_R2.fastq.gz", exit_code=0,
                       engine_version="TesserACT 1.6.2", wording="upstream")
    lines = result.text.split("\n")
    assert lines[1] == "  reads    C:\\Reads é\\s_R1.fastq.gz + C:\\Reads é\\s_R2.fastq.gz"
    assert lines[5] == "[vanilla assembly] %s" % folder
    assert lines[-2].endswith("this text is in %s/SUMMARY.txt)" % folder)
    with open(result.summary_path, "rb") as handle:
        data = handle.read()
    assert data == result.text.encode("utf-8") and b"\r" not in data


def test_no_temporary_files_are_left(tmp_path):
    folder = make(tmp_path)
    before = set(os.listdir(folder))
    run(folder)
    run(folder, wording="windows")
    assert set(os.listdir(folder)) == before | {"SUMMARY.txt", "assembly_stats.tsv"}
