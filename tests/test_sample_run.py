# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""sample_run: one sample through decision D8, with the real Linux engines and with stand-ins.

The real engines work on a made-up 100 kb genome (about two seconds a sample, 4 threads).
The stand-in engines are two small scripts that behave as ``plan.json`` beside them says,
so that crashes, thin data and odd endings can be produced on demand. The helpers at the
top are shared with ``test_batch.py``.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import shutil
import stat
import sys
import threading

import pytest

from tesseract_win import appdirs, osapi, postflight, results, sample_run
from tesseract_win.model import (AssemblySettings, EnginePaths, LogLine, RunConfig, RunStatus, Sample,
                                 SampleStatusChanged, Stage, StageProgress, TrimSettings, Working)

posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in engines are script files")

GENOME = 100_000
GENOME_MB = 0.1
THREADS = 4
_COMP = str.maketrans("ACGT", "TGCA")


# ============================================================================ shared helpers

def make_reads(folder, stem, *, genome=GENOME, depth=40, seed=1, quality="I", gz=True, tags=("_R1", "_R2")):
    """Error-free 2x150 reads of a random genome. Returns ``(read1, read2)`` as text paths."""
    rng = random.Random(seed)
    sequence = "".join(rng.choices("ACGT", k=genome))
    one, two = [], []
    for index in range(int(depth * genome / 300)):
        size = max(170, int(rng.gauss(350, 30)))
        start = rng.randrange(0, genome - size)
        fragment = sequence[start:start + size]
        if rng.random() < 0.5:
            fragment = fragment.translate(_COMP)[::-1]
        name = "@SIM:1:FC:1:1101:%d:%d" % (1000 + index, 2000 + index)
        one.append("%s 1:N:0:1\n%s\n+\n%s\n" % (name, fragment[:150], quality * 150))
        two.append("%s 2:N:0:1\n%s\n+\n%s\n" % (name, fragment[-150:].translate(_COMP)[::-1], quality * 150))
    os.makedirs(str(folder), exist_ok=True)
    paths = []
    for tag, lines in zip(tags, (one, two)):
        path = os.path.join(str(folder), stem + tag + (".fastq.gz" if gz else ".fastq"))
        data = "".join(lines).encode("ascii")
        with open(path, "wb") as handle:
            handle.write(gzip.compress(data, 1, mtime=0) if gz else data)
        paths.append(path)
    return paths[0], paths[1]


def tiny_reads(folder, stem, pairs=40, seed=3):
    """A handful of read pairs: enough for the stand-in engines."""
    return make_reads(folder, stem, genome=2_000, depth=pairs * 300 / 2_000, seed=seed)


def real_engines() -> EnginePaths:
    try:
        return appdirs.find_engines()
    except appdirs.EngineMissing as error:
        pytest.skip("no built engines: %s" % error)


def config_for(results_dir, samples, **assembly):
    settings = dict(threads=THREADS, know_genome_size=True, genome_size_mb=GENOME_MB)
    settings.update(assembly)
    trim = settings.pop("trim", None) or TrimSettings()
    existing = settings.pop("existing", None)
    config = RunConfig(str(results_dir), list(samples), trim=trim, assembly=AssemblySettings(**settings))
    if existing is not None:
        config.existing = existing
    return config


def snapshot(folder):
    """Relative path -> bytes of every file under ``folder``."""
    found = {}
    for root, _dirs, files in os.walk(str(folder)):
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, str(folder)).replace(os.sep, "/")] = handle.read()
    return found


_STAND_IN_ASM = '''\
#!{python}
import json, os, random, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
if "--version" in args:
    print("TesserACT 1.6.2")
    sys.exit(0)
try:
    plan = json.load(open(os.path.join(here, "plan.json")))
except (OSError, ValueError):
    plan = {{}}
sample = os.path.basename(os.path.dirname(os.getcwd()))
mode = plan.get(sample, plan.get("*", "ok"))
out = args[args.index("-o") + 1]
os.makedirs(out, exist_ok=True)
with open(os.path.join(here, "calls.txt"), "a") as calls:
    calls.write("asm %s %s\\n" % (sample, " ".join(args)))
for name in ("-1", "-2"):
    if not os.path.isfile(args[args.index(name) + 1]):
        print("error: input file not found: %s" % args[args.index(name) + 1], file=sys.stderr)
        sys.exit(1)
print("[1/7] loading reads", flush=True)
if mode == "crash":
    os.kill(os.getpid(), signal.SIGSEGV)
if mode == "error":
    print("error: k must be odd to avoid palindromic k-mers (got 22)", file=sys.stderr, flush=True)
    sys.exit(1)
if mode == "memory":
    print("terminate called after throwing an instance of 'std::bad_alloc'", file=sys.stderr, flush=True)
    os.kill(os.getpid(), signal.SIGABRT)
if mode == "hang":
    time.sleep(600)
if isinstance(mode, (int, float)):
    time.sleep(mode)
print("[2/7] read error correction (k=21, trust cutoff auto, graph cutoff auto)", flush=True)
print("[3/7] de Bruijn iterations")
print("  k=21  count 0.1s  distinct=1 solid=1 cutoff=2 peak=30.0", flush=True)
thin = mode == "thin" and "-k" not in args
rng = random.Random(sample)
length = 300 if thin else 5000
if mode != "nothing":
    with open(os.path.join(out, "contigs.fasta"), "w") as fasta:
        fasta.write(">NODE_1_length_%d_cov_30.0000\\n" % length)
        text = "".join(rng.choices("ACGT", k=length))
        for start in range(0, length, 70):
            fasta.write(text[start:start + 70] + "\\n")
with open(os.path.join(out, "report.json"), "w") as report:
    report.write('{{"version": "1.6.2", "written_circular": 0, "written_linear": 0}}\\n')
with open(os.path.join(out, "report.html"), "w") as page:
    page.write("<html><body>stand-in</body></html>\\n")
with open(os.path.join(out, "assembly_graph.gfa"), "w") as graph:
    graph.write("H\\tVN:Z:1.0\\nS\\t1\\t*\\n")
print("[7/7] writing output")
print("  k-mer depth  %s" % ("1.2x (k=21)" if thin else "30.0x (k=99)"), flush=True)
'''

_STAND_IN_SCEPTER = '''\
#!{python}
import gzip, json, os, shutil, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
if "--version" in args:
    print("scepter 1.1.0")
    sys.exit(0)
try:
    plan = json.load(open(os.path.join(here, "plan.json")))
except (OSError, ValueError):
    plan = {{}}
value = lambda flag: args[args.index(flag) + 1]
sample = value("-R")
mode = plan.get("trim:" + sample, plan.get("trim:*", "ok"))
with open(os.path.join(here, "calls.txt"), "a") as calls:
    calls.write("scepter %s %s\\n" % (sample, " ".join(args)))
print("[detect] whole_file sample: 10 + 10 reads in 0.00s", file=sys.stderr, flush=True)
if mode == "crash":
    os.kill(os.getpid(), signal.SIGSEGV)
if isinstance(mode, (int, float)):
    time.sleep(mode)
reads = 0
for source, target in ((value("-i"), value("-o")), (value("-I"), value("-O"))):
    shutil.copyfile(source, target)
    with gzip.open(source, "rb") as handle:
        reads += sum(1 for _ in handle) // 4
sys.stderr.write("\\r  [============            ]  50.0%  1K reads  1K reads/s")
sys.stderr.write("\\r  [========================] 100.0%  2K reads  2K reads/s\\r" + " " * 78 + "\\r")
sys.stderr.flush()
if mode != "no_report":
    with open(value("-j"), "w") as report:
        json.dump({{"summary": {{"after_filtering": {{"total_reads": 0 if mode == "empty" else reads}}}}}}, report)
with open(value("-h"), "w") as page:
    page.write("<html><body>stand-in</body></html>\\n")
'''


def stand_in_engines(folder, plan=None) -> EnginePaths:
    """Two scripts that play the engines. ``plan`` maps a sample name (or ``*``) to what the assembler
    does: ``ok``, ``crash``, ``error``, ``memory``, ``hang``, ``thin``, ``nothing`` or seconds to wait;
    ``trim:<name>`` does the same for the trimmer (``ok``, ``crash``, ``empty``, ``no_report``, seconds)."""
    folder = str(folder)
    os.makedirs(folder, exist_ok=True)
    paths = []
    for name, body in (("tesseract-asm", _STAND_IN_ASM), ("scepter", _STAND_IN_SCEPTER)):
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body.format(python=sys.executable))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        paths.append(path)
    set_plan(folder, plan or {})
    return EnginePaths(tesseract_asm=paths[0], scepter=paths[1], detect_sketch="")


def set_plan(folder, plan):
    with open(os.path.join(str(folder), "plan.json"), "w", encoding="utf-8") as handle:
        json.dump(plan, handle)


def calls(engines: EnginePaths):
    path = os.path.join(os.path.dirname(engines.tesseract_asm), "calls.txt")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return handle.read().splitlines()


class Run:
    """Runs one sample the way the batch does and keeps what it posted."""

    def __init__(self, config, engines, *, stop_at=None, surroundings=None, idle_timeout=sample_run.IDLE_TIMEOUT):
        self.config, self.engines = config, engines
        self.events = []
        self.stop = threading.Event()
        self.stop_at = stop_at
        self.surroundings = surroundings
        self.idle_timeout = idle_timeout
        self.trace = sample_run.SampleTrace()
        self.log = []

    def post(self, event):
        self.events.append(event)
        if self.stop_at is not None and isinstance(event, SampleStatusChanged) and event.status is self.stop_at:
            self.stop.set()

    def __call__(self, sample):
        group = osapi.ProcessGroup()
        try:
            return sample_run.run_sample(
                sample, self.config, self.engines, group=group, stop_event=self.stop, post=self.post,
                check_surroundings=self.surroundings, trace=self.trace, run_log=self.log.append,
                idle_timeout=self.idle_timeout)
        finally:
            group.close()

    def statuses(self):
        return [e.status for e in self.events if isinstance(e, SampleStatusChanged)]


# ============================================================================ fixtures

@pytest.fixture(scope="module")
def reads(tmp_path_factory):
    return make_reads(tmp_path_factory.mktemp("reads"), "KP 1")


@pytest.fixture(scope="module")
def finished(tmp_path_factory, reads):
    """A results folder holding one finished, trimmed sample made by the real engines."""
    folder = tmp_path_factory.mktemp("finished") / "results"
    sample = Sample("KP_1", [reads], trim=True)
    outcome = Run(config_for(folder, [sample]), real_engines())(sample)
    assert outcome.status is RunStatus.DONE, outcome
    return str(folder)


@pytest.fixture
def earlier(tmp_path, finished):
    """A private copy of the finished results folder."""
    folder = tmp_path / "results"
    shutil.copytree(finished, str(folder))
    return str(folder)


# ============================================================================ the real engines

@pytest.mark.engines
def test_a_trimmed_sample_ends_done_with_every_file_in_place(finished, reads):
    record = results.read_status(finished, "KP_1")
    assert record.assembled and record.status is RunStatus.DONE and record.trimmed
    outcome = record.outcome
    assert outcome.contigs == 1 and 99_000 < outcome.total_length < 101_000 and outcome.n50 == outcome.largest
    assert outcome.depth is not None and outcome.depth > 30 and outcome.note == "" and outcome.notes == []
    assert outcome.seconds > 0 and outcome.finished_at and os.path.isfile(outcome.report_html)
    files = set(snapshot(results.sample_dir(finished, "KP_1")))
    assert {"sample_status.json", "trimming/KP_1_trimmed_R1.fastq.gz", "trimming/KP_1_trimmed_R2.fastq.gz",
            "trimming/trimming_report.html", "trimming/trimming_report.json", "trimming/trimming.log",
            "assembly/contigs.fasta", "assembly/report.json", "assembly/report.html", "assembly/assembly_graph.gfa",
            "assembly/assembly.log", "assembly/preflight.txt", "assembly/SUMMARY.txt",
            "assembly/assembly_stats.tsv"} <= files
    assert not any(name.startswith(("_in_progress", "last_failed_attempt", "new_result")) for name in files)
    with open(results.collected_fasta(finished, "KP_1"), "rb") as copy, \
            open(results.contigs_path(finished, "KP_1"), "rb") as original:
        assert copy.read() == original.read()
    assert record.settings_text.startswith("genome size about 0.1 Mb")
    assert [name for name, _size in record.reads] == [os.path.basename(p) for p in reads]


@pytest.mark.engines
def test_the_files_say_what_the_ubuntu_command_would_say(finished):
    folder = results.assembly_dir(finished, "KP_1")
    with open(os.path.join(folder, "preflight.txt"), encoding="utf-8") as handle:
        preflight = handle.read()
    # The reads are named as the engine was given them: relative, so the lines stay true after the move.
    first = os.path.join("trimming", "KP_1_trimmed_R1.fastq.gz")
    assert preflight.splitlines()[0].startswith("TesserACT 1.6.2 preflight: %s + " % first)
    assert "preflight: 0 failed, 0 warnings" in preflight.splitlines()[-1]
    with open(os.path.join(folder, "SUMMARY.txt"), encoding="utf-8") as handle:
        summary = handle.read()
    assert "[vanilla assembly] assembly" in summary and "postflight: 0 failed, 0 warnings" in summary
    assert "version  tesseract 1.6.2, engine TesserACT 1.6.2" in summary
    with open(os.path.join(folder, "assembly.log"), encoding="utf-8") as handle:
        log_text = handle.read()
    for variable, value in postflight.PROFILE_ENV:
        assert "[config] %s=" % variable in log_text
    assert "TESSERACT_P2_DETECT_SKETCH" in log_text and "[7/7] writing output" in log_text
    with open(os.path.join(finished, "KP_1", "trimming", "trimming.log"), encoding="utf-8") as handle:
        assert "[detect]" in handle.read()          # scepter ran with --verbose


@pytest.mark.engines
def test_events_tell_the_stages_in_order_and_do_not_flood(tmp_path, reads):
    sample = Sample("KP_1", [reads], trim=True)
    run = Run(config_for(tmp_path / "r", [sample]), real_engines())
    outcome = run(sample)
    assert outcome.status is RunStatus.DONE
    assert run.statuses() == [RunStatus.TRIMMING, RunStatus.CHECKING_READS, RunStatus.ASSEMBLING,
                              RunStatus.SUMMARISING]
    assert {type(e) for e in run.events} <= {SampleStatusChanged, StageProgress, Working, LogLine}
    assert all(getattr(e, "name") == "KP_1" for e in run.events)
    texts = [e.text for e in run.events if isinstance(e, StageProgress)]
    assert "Trimming: 100 %" in texts and "Checking reads: 100 %" in texts
    assert "Step 1 of 7: reading the reads" in texts and "Step 7 of 7: writing the results" in texts
    assert any(t.startswith("Step 3 of 7: building the assembly graph (k = ") for t in texts)
    assert all(e.fraction is None or 0.0 <= e.fraction <= 1.0 for e in run.events if isinstance(e, StageProgress))
    assert all(e.fraction is None for e in run.events
               if isinstance(e, StageProgress) and e.stage in (Stage.ASSEMBLE, Stage.SUMMARISE))
    assert len(run.events) < 40 * max(1.0, outcome.seconds)        # a few per second, not one per line
    logged = "\n".join(e.text for e in run.events if isinstance(e, LogLine))
    assert "[1/7] loading reads" in logged and "reads/s" not in logged
    assert run.trace.code == "" and run.trace.reached is RunStatus.SUMMARISING
    assert any("assembled in" in line for line in run.log)


@pytest.mark.engines
@pytest.mark.parametrize("stage", [RunStatus.JOINING_LANES, RunStatus.TRIMMING, RunStatus.CHECKING_READS,
                                   RunStatus.ASSEMBLING])
def test_a_stop_during_any_stage_gives_stopped_and_loses_nothing(earlier, tmp_path, reads, stage):
    before = snapshot(earlier)
    second = make_reads(tmp_path / "lane2", "KP 1", seed=2)
    lanes = [reads, second] if stage is RunStatus.JOINING_LANES else [reads]
    sample = Sample("KP_1", lanes, trim=True)
    run = Run(config_for(earlier, [sample]), real_engines(), stop_at=stage)
    outcome = run(sample)
    assert outcome.status is RunStatus.STOPPED
    assert outcome.note == "Stopped. The earlier assembly was kept." and outcome.kept_earlier_result
    assert run.statuses()[-1] is stage
    assert not os.path.exists(results.in_progress_dir(earlier, "KP_1"))
    assert snapshot(earlier) == before                   # the earlier result, byte for byte, and nothing else
    assert results.is_assembled(earlier, "KP_1")


@pytest.mark.engines
def test_a_stop_of_a_first_attempt_leaves_nothing_that_looks_finished(tmp_path, reads):
    sample = Sample("KP_1", [reads], trim=False)
    run = Run(config_for(tmp_path / "r", [sample]), real_engines(), stop_at=RunStatus.ASSEMBLING)
    outcome = run(sample)
    assert outcome.status is RunStatus.STOPPED and not outcome.kept_earlier_result
    assert outcome.note.startswith("Stopped before it was finished")
    folder = results.sample_dir(str(tmp_path / "r"), "KP_1")
    assert not os.path.exists(os.path.join(folder, "_in_progress"))
    assert not os.path.exists(results.status_path(str(tmp_path / "r"), "KP_1"))
    assert not results.is_assembled(str(tmp_path / "r"), "KP_1")
    assert not os.path.exists(results.collected_fasta(str(tmp_path / "r"), "KP_1"))


@pytest.mark.engines
def test_a_stop_in_the_last_seconds_lets_the_sample_finish(tmp_path, reads):
    sample = Sample("KP_1", [reads], trim=False)
    run = Run(config_for(tmp_path / "r", [sample]), real_engines(), stop_at=RunStatus.SUMMARISING)
    outcome = run(sample)
    assert outcome.status is RunStatus.DONE and run.stop.is_set()
    assert results.is_assembled(str(tmp_path / "r"), "KP_1")
    assert not os.path.exists(os.path.join(results.sample_dir(str(tmp_path / "r"), "KP_1"), "trimming"))


@pytest.mark.engines
def test_reads_that_fail_the_read_check_fail_the_sample_unless_told_to_go_on(tmp_path):
    old_style = make_reads(tmp_path / "reads", "old", quality="h")            # an old quality format
    sample = Sample("old", [old_style], trim=False)
    run = Run(config_for(tmp_path / "r", [sample]), real_engines())
    outcome = run(sample)
    assert outcome.status is RunStatus.FAILED
    assert outcome.note == "The read files are not in the standard FASTQ format the program needs."
    assert outcome.next_step == "Ask whoever produced them for standard Illumina FASTQ files."
    assert run.statuses() == [RunStatus.CHECKING_READS] and run.trace.about_data
    assert outcome.log.endswith(os.path.join("last_failed_attempt", "assembly", "preflight.txt"))
    assert os.path.isfile(outcome.log)
    record = results.read_status(str(tmp_path / "r"), "old")
    assert record.status is RunStatus.FAILED and not record.assembled

    forced = Run(config_for(tmp_path / "r", [sample], force=True), real_engines())
    outcome = forced(sample)
    assert outcome.status is RunStatus.DONE_NOTES and outcome.contigs == 1
    assert outcome.note == "The read files are not in the standard FASTQ format the program needs."
    assert results.is_assembled(str(tmp_path / "r"), "old")


@pytest.mark.engines
def test_reads_that_are_all_removed_by_trimming_fail_with_the_reason(tmp_path):
    poor = make_reads(tmp_path / "reads", "poor", genome=5_000, depth=20, quality="#")
    sample = Sample("poor", [poor], trim=True)
    run = Run(config_for(tmp_path / "r", [sample]), real_engines())
    outcome = run(sample)
    assert outcome.status is RunStatus.FAILED and run.trace.code == "trim_no_reads"
    assert outcome.note.startswith("No reads were left after trimming")
    assert outcome.log.endswith(os.path.join("last_failed_attempt", "trimming", "trimming.log"))
    assert run.statuses() == [RunStatus.TRIMMING]
    assert not os.path.exists(results.in_progress_dir(str(tmp_path / "r"), "poor"))


# ============================================================================ the stand-in engines

@posix_only
def test_an_untrimmed_sample_with_stand_ins_and_what_the_engine_is_given(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    pair = tiny_reads(tmp_path / "reads dir", "S1")
    sample = Sample("S 1", [pair], trim=False)
    config = config_for(tmp_path / "résultats [1]", [sample], genome_size_mb=5.0, know_genome_size=False,
                        mode="careful", scaffold=False)
    outcome = Run(config, engines)(sample)
    assert outcome.status is RunStatus.DONE_NOTES and outcome.note.startswith("Very low depth")
    # The row's note, its advice, and the list of every warning (the first one word for word).
    assert outcome.notes == [outcome.note]
    assert outcome.next_step == "More sequencing of this sample would give a better assembly."
    assert outcome.contigs == 1 and outcome.total_length == 5000
    (call,) = calls(engines)
    assert call == "asm S 1 -1 %s -2 %s -o assembly -t %d --mode careful --no-scaffold" % (pair[0], pair[1], THREADS)
    record = results.read_status(config.results_dir, "S 1")
    assert record.settings_text == "not trimmed; mode Careful; no scaffolding"
    assert not os.path.exists(results.trimming_dir(config.results_dir, "S 1"))


@posix_only
def test_trimmed_reads_are_deleted_when_they_are_not_to_be_kept(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=True)
    config = config_for(tmp_path / "r", [sample], trim=TrimSettings(keep_trimmed_reads=False))
    outcome = Run(config, engines)(sample)
    assert outcome.status in results.DONE_STATUSES
    assert sorted(os.listdir(results.trimming_dir(config.results_dir, "S1"))) == [
        "trimming.log", "trimming_report.html", "trimming_report.json"]
    scepter_call = [c for c in calls(engines) if c.startswith("scepter ")][0]
    assert "-o S1_trimmed_R1.fastq.gz -O S1_trimmed_R2.fastq.gz -j trimming_report.json -h trimming_report.html" \
        in scepter_call and "--no-color --no-summary --verbose" in scepter_call and " -w %d " % THREADS in scepter_call
    asm_call = [c for c in calls(engines) if c.startswith("asm ")][0]
    assert "-1 %s -2 %s -o assembly" % (os.path.join("trimming", "S1_trimmed_R1.fastq.gz"),
                                        os.path.join("trimming", "S1_trimmed_R2.fastq.gz")) in asm_call


@posix_only
def test_lanes_are_joined_in_order(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    lanes = [tiny_reads(tmp_path / "reads", "S1_L00%d" % lane, pairs=10 * lane, seed=lane) for lane in (1, 2, 3)]
    sample = Sample("S1", lanes, trim=True)
    run = Run(config_for(tmp_path / "r", [sample]), engines)
    outcome = run(sample)
    assert outcome.status in results.DONE_STATUSES
    assert run.statuses()[0] is RunStatus.JOINING_LANES
    for index in (0, 1):
        expected = b"".join(gzip.decompress(open(pair[index], "rb").read()) for pair in lanes)
        trimmed = os.path.join(results.trimming_dir(run.config.results_dir, "S1"),
                               "S1_trimmed_R%d.fastq.gz" % (index + 1))
        assert gzip.decompress(open(trimmed, "rb").read()) == expected      # the stand-in trimmer copies
    assert not os.path.exists(os.path.join(results.sample_dir(run.config.results_dir, "S1"), "joined"))
    assert any("joined 3 lanes" in line for line in run.log)


@posix_only
def test_plain_lanes_are_joined_too_and_mixed_ones_are_refused(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    first = make_reads(tmp_path / "reads", "a_L001", genome=2_000, depth=3, gz=False)
    second = make_reads(tmp_path / "reads", "a_L002", genome=2_000, depth=3, seed=5, gz=False)
    with open(first[0], "rb+") as handle:                # a file whose last line has no line end
        handle.seek(-1, os.SEEK_END)
        handle.truncate()
    sample = Sample("a", [first, second], trim=False)
    outcome = Run(config_for(tmp_path / "r", [sample]), engines)(sample)
    assert outcome.status in results.DONE_STATUSES
    with open(os.path.join(results.assembly_dir(str(tmp_path / "r"), "a"), "preflight.txt")) as handle:
        assert "40 read pairs, every record well-formed" in handle.read()

    packed = tiny_reads(tmp_path / "reads", "b_L002")
    mixed = Sample("b", [first, packed], trim=False)
    run = Run(config_for(tmp_path / "r", [mixed]), engines)
    outcome = run(mixed)
    assert outcome.status is RunStatus.FAILED and "mix of compressed and uncompressed" in outcome.note
    assert run.trace.about_data


@posix_only
def test_very_thin_data_gets_the_second_attempt(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"S1": "thin"})
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=False)
    run = Run(config_for(tmp_path / "r", [sample], know_genome_size=False), engines)
    outcome = run(sample)
    assert outcome.status is RunStatus.DONE_NOTES and outcome.thin_retry and outcome.total_length == 5000
    asm_calls = [c for c in calls(engines) if c.startswith("asm ")]
    assert len(asm_calls) == 2 and " -k " not in asm_calls[0]
    assert " -t %d -k 21,33,55" % THREADS in asm_calls[1]
    folder = results.assembly_dir(str(tmp_path / "r"), "S1")
    assert os.path.isfile(os.path.join(folder, "attempt1_default_k", "contigs.fasta"))
    assert os.path.isfile(os.path.join(folder, "attempt1_default_k", "assembly.log"))
    texts = [e.text for e in run.events if isinstance(e, StageProgress)]
    assert "Very little data: trying again with settings for thin data." in texts
    assert any("second attempt" in t for t in texts)
    assert len(outcome.notes) == 2 and outcome.notes[0] == outcome.note
    assert "settings for thin data" in outcome.notes[1] and outcome.notes[1].endswith("a better assembly.")
    with open(os.path.join(folder, "SUMMARY.txt")) as handle:
        summary = handle.read()
    assert "thin data" in summary and "this result uses k = 21,33,55" in summary


@posix_only
@pytest.mark.parametrize("plan, code, words", [
    ({"S1": "crash"}, "engine_crash:assembler", "The assembler stopped unexpectedly. This is a fault in the program"),
    ({"S1": "memory"}, "out_of_memory", "This sample needs more memory than this PC could give."),
    ({"S1": "error"}, "engine_error:assembler", "The assembler could not process this sample"),
    ({"S1": "nothing"}, "too_little_data", "Too little data to assemble: no contigs were produced."),
    ({"trim:S1": "crash"}, "engine_crash:trimmer", "The trimmer stopped unexpectedly."),
    ({"trim:S1": "empty"}, "trim_no_reads", "No reads were left after trimming"),
    ({"trim:S1": "no_report"}, "trim_incomplete", "The trimmer ended without writing all of its files"),
])
def test_every_way_an_engine_fails_gives_failed_with_plain_words_and_a_log(tmp_path, plan, code, words):
    engines = stand_in_engines(tmp_path / "engines", plan)
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=True)
    run = Run(config_for(tmp_path / "r", [sample]), engines)
    outcome = run(sample)
    assert outcome.status is RunStatus.FAILED and run.trace.code == code
    assert outcome.note.startswith(words) and outcome.next_step.endswith(".")
    assert outcome.log and os.path.isfile(outcome.log)
    assert os.path.dirname(os.path.dirname(outcome.log)) == results.last_failed_dir(str(tmp_path / "r"), "S1")
    assert not os.path.exists(results.in_progress_dir(str(tmp_path / "r"), "S1"))
    assert not os.path.exists(results.assembly_dir(str(tmp_path / "r"), "S1"))
    record = results.read_status(str(tmp_path / "r"), "S1")
    assert record.status is RunStatus.FAILED and record.outcome.note == outcome.note
    assert not run.trace.environment


@posix_only
def test_an_engine_that_does_nothing_is_ended_and_the_sample_fails(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"S1": "hang"})
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=False)
    run = Run(config_for(tmp_path / "r", [sample]), engines, idle_timeout=1.0)
    outcome = run(sample)
    assert outcome.status is RunStatus.FAILED and run.trace.code == "engine_hung:assembler"
    assert outcome.note == "The assembler stopped responding and was ended after 1 minute without activity."
    assert outcome.seconds < 30


@posix_only
def test_a_failed_redo_keeps_the_earlier_result_and_says_so(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=True)
    config = config_for(tmp_path / "r", [sample])
    assert Run(config, engines)(sample).status in results.DONE_STATUSES
    folder = results.sample_dir(config.results_dir, "S1")
    before = {k: v for k, v in snapshot(folder).items() if k != "sample_status.json"}
    collected = open(results.collected_fasta(config.results_dir, "S1"), "rb").read()
    when = results.read_status(config.results_dir, "S1").finished_at

    set_plan(tmp_path / "engines", {"S1": "crash"})
    outcome = Run(config, engines)(sample)
    assert outcome.status is RunStatus.FAILED and outcome.kept_earlier_result
    from tesseract_win import messages
    assert outcome.note == ("The assembler stopped unexpectedly. This is a fault in the program, not in your "
                            "data. The other samples continue. The earlier assembly (%s) was kept."
                            % messages.date_words(when))
    after = {k: v for k, v in snapshot(folder).items()
             if k != "sample_status.json" and not k.startswith("last_failed_attempt/")}
    assert after == before
    assert open(results.collected_fasta(config.results_dir, "S1"), "rb").read() == collected
    record = results.read_status(config.results_dir, "S1")
    assert record.assembled and record.last_attempt["status"] == "failed"

    again = Run(config, engines)(sample)                 # the same failure a second time (audit row 9.12)
    assert "This is the second time. Trying again will not help unless something changes." in again.note
    assert again.note.endswith("was kept.") and results.is_assembled(config.results_dir, "S1")


@posix_only
def test_a_good_redo_replaces_the_earlier_result(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=True)
    config = config_for(tmp_path / "r", [sample])
    assert Run(config, engines)(sample).status in results.DONE_STATUSES
    marker = os.path.join(results.assembly_dir(config.results_dir, "S1"), "old_marker.txt")
    open(marker, "w").close()
    untrimmed = Sample("S1", sample.lanes, trim=False)
    outcome = Run(config, engines)(untrimmed)
    assert outcome.status in results.DONE_STATUSES and not outcome.kept_earlier_result
    assert not os.path.exists(marker)
    assert not os.path.exists(results.trimming_dir(config.results_dir, "S1"))       # it described other reads
    assert not results.read_status(config.results_dir, "S1").trimmed


@posix_only
def test_a_result_that_cannot_be_put_in_place_waits_beside_it(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=True)
    config = config_for(tmp_path / "r", [sample])
    theirs = os.path.join(results.sample_dir(config.results_dir, "S1"), "assembly", "their work.txt")
    os.makedirs(os.path.dirname(theirs))
    open(theirs, "w").write("not ours")
    run = Run(config, engines)
    outcome = run(sample)
    assert outcome.status is RunStatus.DONE_NOTES
    assert outcome.note.startswith("Done, but the sample's folder holds an assembly or trimming folder that this "
                                   "program did not make") and outcome.notes[0] == outcome.note
    waiting = results.new_result_dir(config.results_dir, "S1")
    assert outcome.note.endswith("The new result is in %s." % os.path.join("S1", "new_result"))
    assert outcome.folder == waiting and os.path.isfile(os.path.join(waiting, "assembly", "contigs.fasta"))
    assert os.path.isfile(outcome.report_html) and os.path.isfile(outcome.log)
    assert open(theirs).read() == "not ours"
    assert not os.path.exists(results.collected_fasta(config.results_dir, "S1"))
    assert not results.is_assembled(config.results_dir, "S1")        # their folder is never passed off as ours


@posix_only
def test_little_free_memory_is_not_a_note_on_the_sample(tmp_path, monkeypatch):
    engines = stand_in_engines(tmp_path / "engines")
    pair = make_reads(tmp_path / "reads", "S1", genome=20_000, depth=40)
    sample = Sample("S1", [pair], trim=False)
    monkeypatch.setattr(osapi, "available_memory_bytes", lambda: 2 * 1024 ** 3)
    config = config_for(tmp_path / "r", [sample], genome_size_mb=0.02, know_genome_size=True)
    outcome = Run(config, engines)(sample)
    preflight = open(os.path.join(results.assembly_dir(config.results_dir, "S1"), "preflight.txt")).read()
    assert "only 2 GB available" in preflight
    assert not any("memory" in note for note in outcome.notes)


@posix_only
def test_a_first_failure_that_repeats_is_told_as_the_second_time(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"S1": "crash"})
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=False)
    config = config_for(tmp_path / "r", [sample])
    first = Run(config, engines)(sample)
    second = Run(config, engines)(sample)
    assert "second time" not in first.note and second.note.endswith(
        "This is the second time. Trying again will not help unless something changes.")
    assert not second.kept_earlier_result


@posix_only
def test_an_error_inside_the_program_becomes_a_failed_sample_not_an_exception(tmp_path, monkeypatch, caplog):
    engines = stand_in_engines(tmp_path / "engines")
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=False)

    def broken(*_args, **_kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(postflight, "summarise", broken)
    run = Run(config_for(tmp_path / "r", [sample]), engines)
    with caplog.at_level("ERROR"):
        outcome = run(sample)
    assert outcome.status is RunStatus.FAILED and run.trace.code == "internal"
    assert outcome.note.startswith("Something went wrong inside TesserACT-Win") and "a bug" not in outcome.note
    assert "RuntimeError: a bug" in caplog.text          # the traceback is in the log
    assert not os.path.exists(results.in_progress_dir(str(tmp_path / "r"), "S1"))
    assert not results.is_assembled(str(tmp_path / "r"), "S1")


@posix_only
def test_a_listener_that_raises_does_not_break_the_run(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=False)
    group = osapi.ProcessGroup()

    def post(_event):
        raise ValueError("the window is gone")

    try:
        outcome = sample_run.run_sample(sample, config_for(tmp_path / "r", [sample]), engines, group=group,
                                        stop_event=threading.Event(), post=post)
    finally:
        group.close()
    assert outcome.status in results.DONE_STATUSES


@posix_only
def test_the_pc_s_failure_does_not_blame_the_sample(tmp_path):
    from tesseract_win import messages
    engines = stand_in_engines(tmp_path / "engines", {"S1": "crash"})
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=False)
    gone = messages.explain_environment("results_gone", "/somewhere")
    run = Run(config_for(tmp_path / "r", [sample]), engines, surroundings=lambda: gone)
    outcome = run(sample)
    assert outcome.status is RunStatus.STOPPED and run.trace.environment == "results_gone"
    assert outcome.note.startswith("Not finished: the results folder can no longer be reached")
    assert results.read_status(str(tmp_path / "r"), "S1") is None        # no "failed" on the sample's record
    assert not os.path.exists(results.in_progress_dir(str(tmp_path / "r"), "S1"))


@posix_only
def test_an_engine_that_cannot_be_started_is_the_pc_s_failure(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    os.remove(engines.tesseract_asm)
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "S1")], trim=False)
    run = Run(config_for(tmp_path / "r", [sample]), engines)
    outcome = run(sample)
    assert outcome.status is RunStatus.STOPPED and run.trace.environment == "engine_missing"
    assert run.trace.environment_path == engines.tesseract_asm

    engines = stand_in_engines(tmp_path / "engines")
    os.chmod(engines.scepter, 0o644)                     # not allowed to run: what "blocked" looks like here
    trimmed = Sample("S2", [tiny_reads(tmp_path / "reads", "S2")], trim=True)
    run = Run(config_for(tmp_path / "r", [trimmed]), engines)
    outcome = run(trimmed)
    assert outcome.status is RunStatus.STOPPED and run.trace.environment == "engine_blocked"


def test_read_files_that_are_gone_fail_the_sample_with_the_reason(tmp_path):
    engines = EnginePaths(str(tmp_path / "no-asm"), str(tmp_path / "no-scepter"), "")
    sample = Sample("S1", [(str(tmp_path / "gone_R1.fastq.gz"), str(tmp_path / "gone_R2.fastq.gz"))], trim=True)
    run = Run(config_for(tmp_path / "r", [sample]), engines)
    outcome = run(sample)
    assert outcome.status is RunStatus.FAILED and run.trace.code == "reads_gone"
    assert outcome.note.startswith("The read files can no longer be read (") and "gone_R1.fastq.gz" in outcome.note
    assert "Is the drive still connected?" in outcome.next_step

    empty = Sample("S2", [], trim=False)
    assert Run(config_for(tmp_path / "r", [empty]), engines)(empty).status is RunStatus.FAILED


def test_settings_text_for_the_status_file():
    config = config_for("x", [])
    assert sample_run.settings_text(RunConfig("x", []), True) == "standard settings"
    assert sample_run.settings_text(RunConfig("x", []), False) == "not trimmed"
    assert "genome size about 0.1 Mb" in sample_run.settings_text(config, True)


# ============================================================================ added by the review

@posix_only
@pytest.mark.parametrize("where", ["collect", "write_status"])
def test_dying_in_the_last_step_of_a_redo_never_looks_finished_and_keeps_the_earlier_result(
        tmp_path, monkeypatch, where):
    """The program dies (power cut) between the swap and the status file: review focus 1."""
    engines = stand_in_engines(tmp_path / "engines")
    sample = Sample("S1", [tiny_reads(tmp_path / "reads", "a")], trim=True)
    config = config_for(tmp_path / "r", [sample])
    assert Run(config, engines)(sample).status in results.DONE_STATUSES
    before = snapshot(results.sample_dir(config.results_dir, "S1"))

    class Died(BaseException):
        pass

    def die(*_args, **_kwargs):
        raise Died()

    with monkeypatch.context() as patch:
        patch.setattr(sample_run.results, where, die)
        with pytest.raises(Died):
            Run(config, engines)(sample)
    # As the crash left it: nothing claims to be a finished result that is not one.
    assert not results.is_assembled(config.results_dir, "S1")
    # What every new run does first puts the earlier result back, byte for byte.
    assert results.recover_interrupted(config.results_dir) == ["S1"]
    assert results.is_assembled(config.results_dir, "S1")
    after = snapshot(results.sample_dir(config.results_dir, "S1"))
    assert {k: v for k, v in after.items() if not k.startswith("_in_progress/")} == before
    # And the sample can simply be run again.
    assert Run(config, engines)(sample).status in results.DONE_STATUSES
    assert not os.path.exists(results.in_progress_dir(config.results_dir, "S1"))


_NOISY_ASM = '''\
#!{python}
import os, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("TesserACT 1.6.2")
    sys.exit(0)
out = args[args.index("-o") + 1]
os.makedirs(out, exist_ok=True)
print("[1/7] loading reads", flush=True)
end, number = time.time() + 2.0, 0
while time.time() < end:
    for _ in range(2000):
        print("  a line of chatter %d %s" % (number, "x" * 80))
        number += 1
    sys.stdout.flush()
with open(os.path.join(out, "contigs.fasta"), "w") as fasta:
    fasta.write(">NODE_1_length_700_cov_30.0000\\n" + "ACGT" * 175 + "\\n")
with open(os.path.join(out, "report.json"), "w") as report:
    report.write('{{"version": "1.6.2", "written_circular": 0, "written_linear": 0}}\\n')
with open(os.path.join(out, "report.html"), "w") as page:
    page.write("<html><body>stand-in</body></html>\\n")
with open(os.path.join(out, "assembly_graph.gfa"), "w") as graph:
    graph.write("H\\tVN:Z:1.0\\n")
print("[7/7] writing output", flush=True)
'''


@posix_only
def test_an_engine_that_prints_without_end_does_not_flood_the_window(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    with open(engines.tesseract_asm, "w", encoding="utf-8") as handle:
        handle.write(_NOISY_ASM.format(python=sys.executable))
    sample = Sample("N1", [tiny_reads(tmp_path / "reads", "a")], trim=False)
    run = Run(config_for(tmp_path / "r", [sample]), engines)
    outcome = run(sample)
    assert outcome.status in results.DONE_STATUSES, outcome
    lines = [e for e in run.events if isinstance(e, LogLine)]
    assert lines and len(lines) <= outcome.seconds / 0.3 + 8, len(lines)     # a few per second at most
    assert max(len(e.text.splitlines()) for e in lines) <= 201               # and none of them without end
    assert len(run.events) < 200
    log_file = os.path.join(results.assembly_dir(config_for(tmp_path / "r", [sample]).results_dir, "N1"),
                            "assembly.log")
    assert os.path.getsize(log_file) > 100_000                               # the file holds all of it
