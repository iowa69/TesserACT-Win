# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""batch: the runner thread, its bookkeeping and what the window asks of it.

Real Linux engines on a made-up 100 kb genome where the engines matter (results in place,
broken inputs, a batch killed in mid-assembly); stand-in engines (see ``test_sample_run``)
where the bookkeeping matters.
"""

from __future__ import annotations

import gzip
import os
import queue
import shutil
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from tesseract_win import batch, fsutil, osapi, results
from tesseract_win.model import (BatchFinished, BatchProgress, BatchStarted, EndReason, Existing, LogLine,
                                 Notice, RunStatus, Sample, SampleFinished, SampleStatusChanged, StageProgress,
                                 StopMode, Working)

from .test_sample_run import (THREADS, calls, config_for, make_reads, posix_only, real_engines, set_plan,
                              snapshot, stand_in_engines, tiny_reads)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE_ORDER = [RunStatus.JOINING_LANES, RunStatus.TRIMMING, RunStatus.CHECKING_READS, RunStatus.ASSEMBLING,
               RunStatus.SUMMARISING]


# ============================================================================ helpers

def run_batch(config, engines, *, left_out=(), resume=False, on_event=None, timeout=180.0):
    """Run a batch to its end. Returns ``(runner, events)``; ``on_event(runner, event)`` sees each event."""
    events: "queue.Queue" = queue.Queue()
    runner = batch.BatchRunner(config, events, engines=engines, left_out=left_out, resume=resume)
    runner.start()
    seen = []
    deadline = time.monotonic() + timeout
    while True:
        event = events.get(timeout=max(0.1, deadline - time.monotonic()))
        seen.append(event)
        if on_event is not None:
            on_event(runner, event)
        if isinstance(event, BatchFinished):
            break
    runner.join(20)
    assert not runner.is_alive() and events.empty()
    return runner, seen


def finals(events):
    """Sample name -> final status, from the events."""
    return {e.name: e.status for e in events if isinstance(e, SampleStatusChanged) and e.status.is_final}


def outcomes(events):
    return {e.outcome.name: e.outcome for e in events if isinstance(e, SampleFinished)}


def check_events(events, config, left_out=0):
    """Every event is one of the contract's classes, well-formed and in a sensible order."""
    names = [s.name for s in config.samples]
    assert isinstance(events[0], BatchStarted) and isinstance(events[-1], BatchFinished)
    assert sum(isinstance(e, (BatchStarted, BatchFinished)) for e in events) == 2
    assert events[0].names == names and events[0].total == len(names)
    final, finished, running, reached = {}, set(), None, -1
    done = failed = position = 0
    for event in events[1:-1]:
        if isinstance(event, SampleStatusChanged):
            assert event.name in names and event.name not in final, event
            assert isinstance(event.status, RunStatus) and isinstance(event.note, str)
            if event.status.is_running:
                assert running in (None, event.name), "two samples at once: %r" % (event,)
                assert STAGE_ORDER.index(event.status) > reached or running is None
                running, reached = event.name, STAGE_ORDER.index(event.status)
            else:
                assert event.status.is_final
                final[event.name] = event.status
                if event.status in (RunStatus.FAILED, RunStatus.LEFT_OUT, RunStatus.SKIPPED, RunStatus.STOPPED):
                    assert event.note, event
                if running == event.name:
                    running, reached = None, -1
        elif isinstance(event, (StageProgress, Working, LogLine)):
            assert event.name == running, event
            if isinstance(event, StageProgress):
                assert event.text and (event.fraction is None or 0.0 <= event.fraction <= 1.0)
            elif isinstance(event, LogLine):
                assert event.text.strip()
            else:
                assert event.busy_threads >= 0 and event.memory_bytes >= 0
        elif isinstance(event, SampleFinished):
            outcome = event.outcome
            assert outcome is not None and final.get(outcome.name) is outcome.status, outcome
            assert outcome.name not in finished
            finished.add(outcome.name)
            if outcome.status is RunStatus.FAILED:
                assert outcome.note and outcome.next_step
        elif isinstance(event, BatchProgress):
            assert event.total == len(names) and position <= event.position <= event.total
            assert event.done >= done and event.failed >= failed
            assert event.eta_seconds is None or event.eta_seconds >= 0
            done, failed, position = event.done, event.failed, event.position
        else:
            assert isinstance(event, Notice), "an event outside the contract: %r" % (event,)
            assert event.kind in ("battery", "memory", "slept", "info") and event.level in ("info", "warn", "bad")
    assert set(final) == set(names), "a sample without a final state"
    end = events[-1]
    assert isinstance(end.reason, EndReason) and end.message and end.seconds >= 0
    assert sum(end.counts.values()) == len(names) + left_out
    for status in set(final.values()):
        expected = sum(1 for s in final.values() if s is status) + (left_out if status is RunStatus.LEFT_OUT else 0)
        assert end.counts[status.value] == expected
    return final


def split_lanes(pair, folder, parts=2):
    """Cut a read pair into ``parts`` lanes with Illumina names. Returns the list of lane pairs."""
    os.makedirs(str(folder), exist_ok=True)
    lanes = [[None, None] for _ in range(parts)]
    for index, path in enumerate(pair):
        records = gzip.decompress(open(path, "rb").read()).split(b"\n")[:-1]
        per_lane = (len(records) // 4 // parts) * 4
        for lane in range(parts):
            chunk = records[lane * per_lane:(lane + 1) * per_lane if lane + 1 < parts else None]
            target = os.path.join(str(folder), "KP-7_S3_L00%d_R%d_001.fastq.gz" % (lane + 1, index + 1))
            with open(target, "wb") as handle:
                handle.write(gzip.compress(b"\n".join(chunk) + b"\n", 1))
            lanes[lane][index] = target
    return [tuple(lane) for lane in lanes]


@pytest.fixture(scope="module")
def reads(tmp_path_factory):
    return make_reads(tmp_path_factory.mktemp("batch reads"), "KP 1")


def stand_in_samples(tmp_path, count, trim=True):
    pair = tiny_reads(tmp_path / "reads", "shared")
    return [Sample("S%02d" % (i + 1), [pair], trim=trim, size_bytes=1000) for i in range(count)]


def last_states(folder):
    return {name: status for name, status in results.load_last_run(str(folder)).states.items()}


# ============================================================================ the real engines

@pytest.mark.engines
def test_a_trimmed_and_an_untrimmed_sample_end_done_with_every_file_of_decision_d1(tmp_path, reads):
    folder = tmp_path / "Results of today [1]"
    samples = [Sample("KP_trimmed", [reads], trim=True), Sample("KP plain é", [reads], trim=False)]
    config = config_for(folder, samples)
    assert batch.preflight_batch(config, real_engines()) == []
    runner, events = run_batch(config, real_engines())
    final = check_events(events, config)
    assert final == {"KP_trimmed": RunStatus.DONE, "KP plain é": RunStatus.DONE}
    end = events[-1]
    assert end.reason is EndReason.COMPLETED and end.counts == {"done": 2}
    assert end.message.startswith("Finished at ") and end.message.endswith("2 done.")
    files = set(snapshot(folder))
    assert {"run_summary.html", "run_summary.tsv", "run_log.txt", "last_run.json",
            "assemblies/KP_trimmed.fasta", "assemblies/KP plain é.fasta",
            "KP_trimmed/sample_status.json", "KP_trimmed/trimming/KP_trimmed_trimmed_R1.fastq.gz",
            "KP_trimmed/trimming/KP_trimmed_trimmed_R2.fastq.gz", "KP_trimmed/trimming/trimming_report.html",
            "KP_trimmed/trimming/trimming_report.json", "KP_trimmed/trimming/trimming.log",
            "KP_trimmed/assembly/contigs.fasta", "KP_trimmed/assembly/SUMMARY.txt",
            "KP_trimmed/assembly/assembly_stats.tsv", "KP_trimmed/assembly/preflight.txt",
            "KP_trimmed/assembly/report.html", "KP_trimmed/assembly/assembly.log",
            "KP plain é/sample_status.json", "KP plain é/assembly/contigs.fasta",
            "KP plain é/assembly/SUMMARY.txt", "KP plain é/assembly/report.html"} <= files
    assert not any("_in_progress" in name or "last_failed_attempt" in name or name.startswith("KP plain é/trimming")
                   for name in files)
    assert not [name for name in files if ".tmp" in name]
    assert results.count_assemblies(str(folder)) == 2
    for outcome in outcomes(events).values():
        assert outcome.contigs == 1 and outcome.n50 > 99_000 and os.path.isfile(outcome.report_html)
    last = results.load_last_run(str(folder))
    assert last.ended_at and last.end_reason is EndReason.COMPLETED
    assert set(last.states.values()) == {RunStatus.DONE}
    assert batch.interrupted_run(str(folder)) is None and batch.failed_config(str(folder)) is None
    log_text = (folder / "run_log.txt").read_text(encoding="utf-8")
    assert "KP_trimmed: done in" in log_text and "KP plain é: done in" in log_text and "\r" not in log_text
    assert "Settings: standard trimming, genome size about 0.1 Mb, %d threads." % THREADS in log_text
    summary = (folder / "run_summary.tsv").read_text(encoding="utf-8")
    assert "KP_trimmed" in summary and "KP plain é" in summary
    assert osapi.FolderLock.holder_text(str(folder)) == ""               # the lock went with the run
    progress = [e for e in events if isinstance(e, BatchProgress)]
    assert [p.position for p in progress] == [1, 1, 2, 2] and progress[0].eta_seconds is None
    assert progress[1].eta_seconds > 0 and progress[-1].done == 2 and progress[-1].eta_seconds == 0


@pytest.mark.engines
def test_lanes_are_joined_and_give_the_assembly_of_the_whole(tmp_path, reads):
    lanes = split_lanes(reads, tmp_path / "lanes")
    samples = [Sample("KP-7", lanes, trim=False), Sample("whole", [reads], trim=False)]
    config = config_for(tmp_path / "r", samples)
    _runner, events = run_batch(config, real_engines())
    assert check_events(events, config) == {"KP-7": RunStatus.DONE, "whole": RunStatus.DONE}
    statuses = [e.status for e in events if isinstance(e, SampleStatusChanged) and e.name == "KP-7"]
    assert statuses[0] is RunStatus.JOINING_LANES
    joined = open(results.contigs_path(config.results_dir, "KP-7"), "rb").read()
    assert joined == open(results.contigs_path(config.results_dir, "whole"), "rb").read()
    assert len(results.read_status(config.results_dir, "KP-7").reads) == 4


@pytest.mark.engines
def test_broken_inputs_fail_with_plain_notes_and_the_batch_goes_on(tmp_path, reads):
    broken = tmp_path / "broken"
    broken.mkdir()
    data = [gzip.decompress(open(path, "rb").read()) for path in reads]

    def pair(stem, one, two, raw2=None):
        paths = (str(broken / (stem + "_R1.fastq.gz")), str(broken / (stem + "_R2.fastq.gz")))
        open(paths[0], "wb").write(gzip.compress(one, 1))
        open(paths[1], "wb").write(raw2 if raw2 is not None else gzip.compress(two, 1))
        return paths

    whole2 = gzip.compress(data[1], 1)
    records2 = data[1].split(b"\n")[:-1]
    swapped = records2[4:8] + records2[0:4] + records2[8:]
    samples = [
        Sample("cut_short", [pair("cut", data[0], b"", raw2=whole2[:len(whole2) // 2])], trim=True),
        Sample("fewer_reads", [pair("fewer", data[0], b"\n".join(records2[:-400]) + b"\n")], trim=True),
        Sample("other_order", [pair("order", data[0], b"\n".join(swapped) + b"\n")], trim=True),
        Sample("cut_short_untrimmed", [pair("cut2", data[0], b"", raw2=whole2[:len(whole2) // 2])], trim=False),
        Sample("good", [reads], trim=True),
    ]
    config = config_for(tmp_path / "r", samples)
    _runner, events = run_batch(config, real_engines())
    final = check_events(events, config)
    assert final == {"cut_short": RunStatus.FAILED, "fewer_reads": RunStatus.FAILED,
                     "other_order": RunStatus.FAILED, "cut_short_untrimmed": RunStatus.FAILED,
                     "good": RunStatus.DONE}
    assert events[-1].reason is EndReason.COMPLETED and events[-1].counts == {"failed": 4, "done": 1}
    found = outcomes(events)
    damaged = ("A read file is incomplete or damaged (%s). It may have been cut short while copying or "
               "downloading.")
    assert found["cut_short"].note == damaged % "cut_R2.fastq.gz"
    assert found["cut_short"].next_step == "Copy it again from the original, then try again."
    assert found["cut_short_untrimmed"].note == damaged % "cut2_R2.fastq.gz"
    mismatch = "Read 1 and Read 2 do not match: one file has fewer reads or they are in a different order."
    assert found["fewer_reads"].note == mismatch and found["other_order"].note == mismatch
    assert found["other_order"].next_step == "Use the two original files from the sequencer."
    for name in ("cut_short", "fewer_reads", "other_order", "cut_short_untrimmed"):
        assert not results.is_assembled(config.results_dir, name)
        assert not os.path.exists(results.in_progress_dir(config.results_dir, name))
        assert results.read_status(config.results_dir, name).status is RunStatus.FAILED
    retry = batch.failed_config(config.results_dir)
    assert [s.name for s in retry.samples] == ["cut_short", "fewer_reads", "other_order", "cut_short_untrimmed"]
    assert retry.existing is Existing.OVERWRITE
    summary = open(results.summary_html_path(config.results_dir), encoding="utf-8").read()
    assert "A read file is incomplete or damaged (cut_R2.fastq.gz)" in summary


_HELPER = """\
import queue, sys
from tesseract_win import batch
from tesseract_win.model import AssemblySettings, BatchFinished, RunConfig, Sample
folder, read1, read2 = sys.argv[1:4]
samples = [Sample("K%d" % i, [(read1, read2)], trim=False) for i in (1, 2, 3)]
config = RunConfig(folder, samples, assembly=AssemblySettings(threads=4, know_genome_size=True, genome_size_mb=0.1))
events = queue.Queue()
runner = batch.BatchRunner(config, events)
runner.start()
while not isinstance(events.get(), BatchFinished):
    pass
"""


def _engines_working_in(folder):
    """The process ids of engines whose working folder lies inside ``folder``."""
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                where = os.readlink("/proc/%s/cwd" % entry)
                with open("/proc/%s/cmdline" % entry, "rb") as handle:
                    command = handle.read().decode("utf-8", "replace")
            except OSError:
                continue
            if where.startswith(folder) and "tesseract-asm" in command:
                found.append(entry)
    return found


@posix_only
@pytest.mark.engines
@pytest.mark.slow
def test_a_batch_killed_in_mid_assembly_leaves_no_engine_and_can_be_continued(tmp_path, reads):
    real_engines()
    folder = str(tmp_path / "killed run")
    script = tmp_path / "helper.py"
    script.write_text(_HELPER, encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=REPO)
    helper = subprocess.Popen([sys.executable, str(script), folder, reads[0], reads[1]], env=environment,
                              cwd=str(tmp_path), stdin=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            data = fsutil.read_json(results.last_run_path(folder), {}) or {}
            state = (data.get("states") or {}).get("K2", {})
            if state.get("status") == "assembling" and _engines_working_in(os.path.join(folder, "K2")):
                break
            assert helper.poll() is None, "the helper ended before the second sample was being assembled"
            time.sleep(0.02)
        else:
            pytest.fail("the second sample never reached the assembly")
        engines_before = _engines_working_in(folder)
        assert engines_before
        os.kill(helper.pid, signal.SIGKILL)
        helper.wait(30)
    finally:
        if helper.poll() is None:
            helper.kill()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(os.path.exists("/proc/%s" % p) for p in engines_before):
        time.sleep(0.05)
    assert not any(os.path.exists("/proc/%s" % p) for p in engines_before), "an engine outlived the program"

    assert results.is_assembled(folder, "K1")
    first = snapshot(results.sample_dir(folder, "K1"))
    assert not results.is_assembled(folder, "K2") and not os.path.exists(results.collected_fasta(folder, "K2"))
    found = batch.interrupted_run(folder)
    assert found is not None and found.cause == "unknown" and found.missing == []
    assert (found.finished, found.total) == (1, 3) and found.when
    assert [s.name for s in found.config.samples] == ["K2", "K3"]
    assert batch.preflight_batch(found.config) == []     # the lock died with the program

    _runner, events = run_batch(found.config, None, resume=True)
    assert check_events(events, found.config) == {"K2": RunStatus.DONE, "K3": RunStatus.DONE}
    assert snapshot(results.sample_dir(folder, "K1")) == first
    assert not os.path.exists(results.in_progress_dir(folder, "K2"))
    assert last_states(folder) == {"K1": RunStatus.DONE, "K2": RunStatus.DONE, "K3": RunStatus.DONE}
    assert batch.interrupted_run(folder) is None
    assert _engines_working_in(folder) == []


# ============================================================================ bookkeeping, stand-in engines

@posix_only
def test_thirty_samples_every_one_accounted_for_and_every_event_in_order(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"S07": "crash", "trim:S20": "crash", "S13": "nothing"})
    samples = stand_in_samples(tmp_path, 30)
    for sample in samples[::3]:
        sample.trim = False
    left_out = [("lonely", "No Read 2 file was found for this sample."), ("mixed", "Compressed and plain files mixed.")]
    config = config_for(tmp_path / "r", samples)
    runner, events = run_batch(config, engines, left_out=left_out)
    final = check_events(events, config, left_out=2)
    failed = {name for name, status in final.items() if status is RunStatus.FAILED}
    assert failed == {"S07", "S20", "S13"}
    assert all(status in results.DONE_STATUSES for name, status in final.items() if name not in failed)
    end = events[-1]
    assert end.reason is EndReason.COMPLETED and end.counts["failed"] == 3 and end.counts["left_out"] == 2
    assert end.counts.get("done", 0) + end.counts.get("done_notes", 0) == 27
    assert "3 failed" in end.message and "2 left out" in end.message
    assert runner.current() == ("", 0.0) and runner.stop_pending() is None

    folder = config.results_dir
    assert results.count_assemblies(folder) == 27
    assert sorted(os.listdir(results.assemblies_dir(folder))) == sorted(
        "%s.fasta" % s.name for s in samples if s.name not in failed)
    last = results.load_last_run(folder)
    assert last.ended_at and last.end_reason is EndReason.COMPLETED and len(last.states) == 30
    assert {n for n, s in last.states.items() if s is RunStatus.FAILED} == failed
    assert [s.name for s in last.config.samples] == [s.name for s in samples]
    table = open(results.summary_tsv_path(folder), encoding="utf-8").read().splitlines()
    assert len(table) == 1 + 30 + 2                       # the header, every sample, the two left out
    page = open(results.summary_html_path(folder), encoding="utf-8").read()
    assert "lonely" in page and "No Read 2 file was found" in page and "The assembler stopped unexpectedly" in page
    run_log = open(results.run_log_path(folder), encoding="utf-8").read()
    assert all("%s: started" % s.name in run_log for s in samples) and "S07: FAILED" in run_log
    assert not any(os.path.exists(results.in_progress_dir(folder, s.name)) for s in samples)
    progress = [e for e in events if isinstance(e, BatchProgress)]
    assert [p.position for p in progress[::2]] == list(range(1, 31)) and progress[0].eta_seconds is None
    assert all(p.eta_seconds is not None for p in progress[1:])
    asm = [c.split(" ")[1] for c in calls(engines) if c.startswith("asm ")]
    assert asm == [s.name for s in samples if s.name != "S20"]            # one at a time, in order, once each
    assert [s.name for s in batch.failed_config(folder).samples] == ["S07", "S13", "S20"]


@posix_only
def test_existing_results_are_skipped_or_redone_as_asked(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 3)
    config = config_for(tmp_path / "r", samples)
    folder = config.results_dir
    _runner, events = run_batch(config, engines)
    assert all(s in results.DONE_STATUSES for s in check_events(events, config).values())
    before = snapshot(folder)
    first_calls = len(calls(engines))

    # Skip (the default): nothing is run, nothing is touched, the rows say when they were made.
    assert config.existing is Existing.SKIP
    _runner, events = run_batch(config, engines)
    assert set(check_events(events, config).values()) == {RunStatus.SKIPPED}
    assert len(calls(engines)) == first_calls
    skipped = outcomes(events)["S01"]
    assert skipped.note.startswith("Already assembled on ") and skipped.contigs == 1 and skipped.total_length == 5000
    assert events[-1].counts == {"skipped": 3} and "3 skipped (already assembled)" in events[-1].message
    now = snapshot(folder)
    for name in before:
        if name.endswith(("contigs.fasta", "sample_status.json", ".fasta", ".fastq.gz")):
            assert now[name] == before[name], name

    # Skipped results made with other settings say so.
    other = config_for(folder, samples, mode="careful")
    _runner, events = run_batch(other, engines)
    assert outcomes(events)["S02"].note.endswith(", with other settings.")

    # Overwrite: a good redo replaces, a failed one keeps the old result.
    for sample in samples:
        open(os.path.join(results.assembly_dir(folder, sample.name), "old_marker.txt"), "w").close()
    set_plan(tmp_path / "engines", {"S02": "crash"})
    redo = config_for(folder, samples, existing=Existing.OVERWRITE)
    _runner, events = run_batch(redo, engines)
    final = check_events(events, redo)
    assert final["S02"] is RunStatus.FAILED and final["S01"] in results.DONE_STATUSES
    assert not os.path.exists(os.path.join(results.assembly_dir(folder, "S01"), "old_marker.txt"))
    assert os.path.exists(os.path.join(results.assembly_dir(folder, "S02"), "old_marker.txt"))
    kept = outcomes(events)["S02"]
    assert kept.kept_earlier_result and kept.note.endswith("was kept.") and results.is_assembled(folder, "S02")
    assert snapshot(folder)["S02/assembly/contigs.fasta"] == before["S02/assembly/contigs.fasta"]
    assert snapshot(folder)["assemblies/S02.fasta"] == before["assemblies/S02.fasta"]
    assert results.count_assemblies(folder) == 3


@posix_only
def test_the_same_name_from_other_reads_is_left_out_with_the_reason(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 2)
    config = config_for(tmp_path / "r", samples)
    run_batch(config, engines)
    before = snapshot(results.sample_dir(config.results_dir, "S01"))
    stranger = Sample("S01", [tiny_reads(tmp_path / "other", "x", pairs=30, seed=9)], trim=True)
    fresh = Sample("S03", samples[0].lanes, trim=True)
    for existing in (Existing.SKIP, Existing.OVERWRITE):
        again = config_for(config.results_dir, [stranger, fresh], existing=existing)
        _runner, events = run_batch(again, engines)
        final = check_events(events, again)
        assert final["S01"] is RunStatus.LEFT_OUT
        note = [e.note for e in events if isinstance(e, SampleStatusChanged) and e.name == "S01"][0]
        assert note.startswith("A different sample called S01 is already assembled") and "Rename this sample" in note
        assert snapshot(results.sample_dir(config.results_dir, "S01")) == before
        assert events[-1].counts["left_out"] == 1
    assert "A different sample called S01" in open(results.summary_html_path(config.results_dir), encoding="utf-8").read()

    # A folder somebody else put there is not ours to replace either (audit row 3.7).
    os.makedirs(os.path.join(config.results_dir, "S09", "assembly"))
    theirs = config_for(config.results_dir, [Sample("S09", samples[0].lanes, trim=False)])
    _runner, events = run_batch(theirs, engines)
    assert finals(events) == {"S09": RunStatus.LEFT_OUT}
    reserved = config_for(config.results_dir, [Sample("assemblies", samples[0].lanes, trim=False)])
    _runner, events = run_batch(reserved, engines)
    assert finals(events) == {"assemblies": RunStatus.LEFT_OUT}


@posix_only
def test_stop_now_then_continue_runs_only_what_is_left(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"S02": 30})
    samples = stand_in_samples(tmp_path, 4, trim=False)
    config = config_for(tmp_path / "r", samples)
    folder = config.results_dir
    seen_current = []

    def stop_at_second(runner, event):
        if isinstance(event, SampleStatusChanged) and event.name == "S02" and event.status is RunStatus.ASSEMBLING:
            time.sleep(0.3)
            seen_current.append(runner.current())
            assert batch.interrupted_run(folder) is None          # it is running, not interrupted
            assert any("being used by another run" in p for p in batch.preflight_batch(config, engines))
            runner.request_stop(StopMode.NOW)
            assert runner.stop_pending() is StopMode.NOW

    began = time.monotonic()
    _runner, events = run_batch(config, engines, on_event=stop_at_second)
    assert time.monotonic() - began < 20                         # the engine was ended, not waited for
    final = check_events(events, config)
    assert final == {"S01": final["S01"], "S02": RunStatus.STOPPED, "S03": RunStatus.NOT_STARTED,
                     "S04": RunStatus.NOT_STARTED}
    assert final["S01"] in results.DONE_STATUSES
    assert seen_current[0][0] == "S02" and seen_current[0][1] > 0.2
    end = events[-1]
    assert end.reason is EndReason.STOPPED
    assert end.message == "Stopped. 1 finished, 1 stopped, 2 not started. Nothing that was finished has been lost."
    assert outcomes(events)["S02"].note.startswith("Stopped before it was finished")
    assert not os.path.exists(results.in_progress_dir(folder, "S02"))
    last = results.load_last_run(folder)
    assert last.ended_at and last.end_reason is EndReason.STOPPED

    found = batch.interrupted_run(folder)
    assert found.cause == "stopped" and (found.finished, found.total) == (1, 4) and found.missing == []
    assert [s.name for s in found.config.samples] == ["S02", "S03", "S04"]
    assert found.config.assembly == config.assembly and found.config.trim == config.trim

    set_plan(tmp_path / "engines", {})
    _runner, events = run_batch(found.config, engines, resume=True)
    final = check_events(events, found.config)
    assert set(final) == {"S02", "S03", "S04"} and all(s in results.DONE_STATUSES for s in final.values())
    asm = [c.split(" ")[1] for c in calls(engines) if c.startswith("asm ")]
    assert asm == ["S01", "S02", "S02", "S03", "S04"]            # the finished sample was not done again
    assert set(last_states(folder)) == {"S01", "S02", "S03", "S04"}
    assert all(s in results.DONE_STATUSES for s in last_states(folder).values())
    assert batch.interrupted_run(folder) is None
    assert results.count_assemblies(folder) == 4


@posix_only
def test_stop_after_the_current_sample_and_changing_one_s_mind(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"S01": 1.0})
    samples = stand_in_samples(tmp_path, 3, trim=False)

    def stop_after(runner, event):
        if isinstance(event, SampleStatusChanged) and event.name == "S01" and event.status is RunStatus.ASSEMBLING:
            runner.request_stop(StopMode.AFTER_CURRENT)
            assert runner.stop_pending() is StopMode.AFTER_CURRENT

    config = config_for(tmp_path / "r1", samples)
    _runner, events = run_batch(config, engines, on_event=stop_after)
    final = check_events(events, config)
    assert final["S01"] in results.DONE_STATUSES
    assert (final["S02"], final["S03"]) == (RunStatus.NOT_STARTED, RunStatus.NOT_STARTED)
    assert events[-1].reason is EndReason.STOPPED and "1 finished, 2 not started" in events[-1].message

    def stop_then_keep_going(runner, event):
        if isinstance(event, SampleStatusChanged) and event.name == "S01" and event.status is RunStatus.ASSEMBLING:
            runner.request_stop(StopMode.AFTER_CURRENT)
            runner.cancel_stop()
            assert runner.stop_pending() is None

    config = config_for(tmp_path / "r2", samples)
    _runner, events = run_batch(config, engines, on_event=stop_then_keep_going)
    assert all(s in results.DONE_STATUSES for s in check_events(events, config).values())
    assert events[-1].reason is EndReason.COMPLETED


@posix_only
@pytest.mark.parametrize("moment", ["as the assembler starts", "while the assembler works"])
def test_the_results_folder_taken_away_in_mid_batch_ends_it_as_the_pc_s_failure(tmp_path, moment):
    engines = stand_in_engines(tmp_path / "engines", {"S02": 1.5})
    samples = stand_in_samples(tmp_path, 4, trim=False)
    config = config_for(tmp_path / "results", samples)
    folder = config.results_dir
    away = str(tmp_path / "unplugged")

    def unplug(_runner, event):
        if isinstance(event, SampleStatusChanged) and event.name == "S02" and event.status is RunStatus.ASSEMBLING:
            if moment == "while the assembler works":
                deadline = time.monotonic() + 20
                while not any(c.startswith("asm S02 ") for c in calls(engines)) and time.monotonic() < deadline:
                    time.sleep(0.01)
            os.rename(folder, away)

    _runner, events = run_batch(config, engines, on_event=unplug)
    final = check_events(events, config)
    assert final["S01"] in results.DONE_STATUSES
    assert (final["S02"], final["S03"], final["S04"]) == (RunStatus.STOPPED, RunStatus.NOT_STARTED,
                                                          RunStatus.NOT_STARTED)
    end = events[-1]
    assert end.reason is EndReason.ENVIRONMENT
    assert end.message == ("Stopped: the results folder can no longer be reached (%s). Reconnect the drive, then "
                           "press Continue. The 1 finished sample is safe." % folder)
    assert outcomes(events)["S02"].note.startswith("Not finished: the results folder can no longer be reached")
    assert results.is_assembled(away, "S01")                     # what was finished is intact where it went
    assert not os.path.exists(os.path.join(folder, "last_run.json"))     # and nothing was faked in its place

    # The drive comes back: Continue goes on with what is left.
    shutil.rmtree(folder, ignore_errors=True)
    os.rename(away, folder)
    set_plan(tmp_path / "engines", {})
    found = batch.interrupted_run(folder)
    assert found is not None and [s.name for s in found.config.samples] == ["S02", "S03", "S04"]
    _runner, events = run_batch(found.config, engines, resume=True)
    assert all(s in results.DONE_STATUSES for s in check_events(events, found.config).values())
    assert results.count_assemblies(folder) == 4


@posix_only
def test_a_results_folder_gone_between_two_samples(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 3, trim=False)
    config = config_for(tmp_path / "results", samples)

    away = str(tmp_path / "unplugged")

    def unplug(_runner, event):
        if isinstance(event, BatchProgress) and event.done == 1 and not os.path.exists(away):
            os.rename(config.results_dir, away)

    _runner, events = run_batch(config, engines, on_event=unplug)
    final = check_events(events, config)
    # The folder went while the runner was between two samples; whichever moment it noticed, the
    # batch ends as the PC's failure, the last sample never starts and what was finished is safe.
    assert final["S03"] is RunStatus.NOT_STARTED
    assert events[-1].reason is EndReason.ENVIRONMENT and "can no longer be reached" in events[-1].message
    assert results.is_assembled(away, "S01")
    assert not os.path.exists(results.last_run_path(config.results_dir))


@posix_only
def test_three_quick_failures_of_the_same_kind_in_a_row_stop_the_batch(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"*": "crash"})
    samples = stand_in_samples(tmp_path, 6, trim=False)
    config = config_for(tmp_path / "r1", samples)
    _runner, events = run_batch(config, engines)
    final = check_events(events, config)
    assert [final["S0%d" % i] for i in range(1, 7)] == [RunStatus.FAILED] * 3 + [RunStatus.NOT_STARTED] * 3
    end = events[-1]
    assert end.reason is EndReason.ENVIRONMENT
    assert end.message.startswith("Stopped: three samples in a row failed in the same way, straight after starting.")
    assert "press Continue" in end.message
    assert len([c for c in calls(engines) if c.startswith("asm ")]) == 3
    assert [s.name for s in batch.interrupted_run(config.results_dir).config.samples] == ["S04", "S05", "S06"]

    # A good sample in between, or failures of different kinds, are not the PC's failure.
    set_plan(tmp_path / "engines", {"S01": "crash", "S02": "crash", "S04": "crash", "S05": "error", "S06": "crash"})
    config = config_for(tmp_path / "r2", samples)
    _runner, events = run_batch(config, engines)
    assert events[-1].reason is EndReason.COMPLETED and events[-1].counts["failed"] == 5

    # Slow failures are not "within a minute of starting".
    set_plan(tmp_path / "engines", {"*": "crash"})
    config = config_for(tmp_path / "r3", samples[:4])
    old = batch.QUICK_FAILURE_SECONDS
    batch.QUICK_FAILURE_SECONDS = 0.0
    try:
        _runner, events = run_batch(config, engines)
    finally:
        batch.QUICK_FAILURE_SECONDS = old
    assert events[-1].reason is EndReason.COMPLETED and events[-1].counts == {"failed": 4}


@posix_only
def test_failures_that_belong_to_the_samples_never_stop_the_batch(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    good = tiny_reads(tmp_path / "reads", "good")
    cut = str(tmp_path / "reads" / "cut_R2.fastq.gz")
    whole = open(good[1], "rb").read()
    open(cut, "wb").write(whole[:len(whole) // 2])
    samples = [Sample("B%d" % i, [(good[0], cut)], trim=True) for i in range(1, 6)] + [Sample("G", [good], trim=True)]
    config = config_for(tmp_path / "r", samples)
    _runner, events = run_batch(config, engines)
    final = check_events(events, config)
    assert events[-1].reason is EndReason.COMPLETED and events[-1].counts["failed"] == 5
    assert final["G"] in results.DONE_STATUSES


@posix_only
def test_an_engine_that_disappears_in_mid_run_stops_the_batch(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 3, trim=False)
    config = config_for(tmp_path / "r", samples)

    def quarantine(_runner, event):
        if isinstance(event, SampleFinished) and event.outcome.name == "S01":
            os.remove(engines.tesseract_asm)

    _runner, events = run_batch(config, engines, on_event=quarantine)
    final = check_events(events, config)
    assert final["S01"] in results.DONE_STATUSES and final["S02"] is RunStatus.NOT_STARTED
    end = events[-1]
    assert end.reason is EndReason.ENVIRONMENT
    assert end.message.startswith("Stopped: part of the program has disappeared (%s). Antivirus software "
                                  "sometimes removes it by mistake." % engines.tesseract_asm)
    assert end.message.endswith("The 1 finished sample is safe.")
    assert any("part of the program has disappeared" in p or "Part of the program has disappeared" in p
               for p in batch.preflight_batch(config, engines))


@posix_only
def test_read_files_that_are_gone(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    good = stand_in_samples(tmp_path, 4, trim=False)
    gone = (str(tmp_path / "usb" / "x_R1.fastq.gz"), str(tmp_path / "usb" / "x_R2.fastq.gz"))
    # One sample whose files are gone fails; the batch goes on.
    samples = [good[0], Sample("lost", [gone], trim=False), good[1]]
    config = config_for(tmp_path / "r1", samples)
    _runner, events = run_batch(config, engines)
    final = check_events(events, config)
    assert final["lost"] is RunStatus.FAILED and final["S02"] in results.DONE_STATUSES
    lost = outcomes(events)["lost"]
    assert lost.note == "The read files can no longer be read (%s)." % gone[0]
    assert "Is the drive still connected?" in lost.next_step
    assert events[-1].reason is EndReason.COMPLETED
    # Two in a row: the drive of the reads is gone, which is the PC's failure (audit row 9.10).
    samples = [good[0], Sample("lost1", [gone], trim=False), Sample("lost2", [gone], trim=False), good[1]]
    config = config_for(tmp_path / "r2", samples)
    _runner, events = run_batch(config, engines)
    final = check_events(events, config)
    assert final == {"S01": final["S01"], "lost1": RunStatus.FAILED, "lost2": RunStatus.NOT_STARTED,
                     "S02": RunStatus.NOT_STARTED}
    assert events[-1].reason is EndReason.ENVIRONMENT
    assert events[-1].message.startswith("Stopped: the read files can no longer be read (%s)." % gone[0])
    found = batch.interrupted_run(config.results_dir)
    assert found.missing == ["lost2"] and [s.name for s in found.config.samples] == ["S02"]


@posix_only
def test_a_folder_in_use_and_a_folder_of_a_newer_version_are_refused(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 2, trim=False)
    config = config_for(tmp_path / "busy", samples)
    os.makedirs(config.results_dir)
    lock = osapi.FolderLock()
    assert lock.acquire(config.results_dir, "Ada on LAB-PC, since 2026-11-16T09:00:00")
    try:
        problems = batch.preflight_batch(config, engines)
        assert len(problems) == 1 and problems[0].startswith(
            "The results folder is being used by another run (Ada on LAB-PC, since 2026-11-16T09:00:00).")
        helper = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""\
                import queue, sys
                from tesseract_win import batch
                from tesseract_win.model import BatchFinished, EnginePaths, RunConfig, Sample
                engines = EnginePaths(sys.argv[2], sys.argv[3], "")
                config = RunConfig(sys.argv[1], [Sample("S01", [(sys.argv[4], sys.argv[5])], trim=False)])
                events = queue.Queue()
                batch.BatchRunner(config, events, engines=engines).start()
                while True:
                    event = events.get(timeout=60)
                    if isinstance(event, BatchFinished):
                        print(event.reason.value, "|", event.message, "|", event.counts)
                        break
                """), config.results_dir, engines.tesseract_asm, engines.scepter, *samples[0].lanes[0]],
            env=dict(os.environ, PYTHONPATH=REPO), capture_output=True, text=True, timeout=120)
        assert helper.returncode == 0, helper.stderr
        assert helper.stdout.startswith("environment | Stopped: the results folder is being used by another run "
                                        "(Ada on LAB-PC, since 2026-11-16T09:00:00). Wait for that run to finish")
        assert "{'not_started': 1}" in helper.stdout
        assert not os.path.exists(results.last_run_path(config.results_dir))
    finally:
        lock.release()

    newer = config_for(tmp_path / "newer", samples)
    os.makedirs(newer.results_dir)
    fsutil.atomic_write_json(results.last_run_path(newer.results_dir), {"format": 99, "config": {}})
    assert any("made by a newer version of TesserACT-Win" in p for p in batch.preflight_batch(newer, engines))
    _runner, events = run_batch(newer, engines)
    assert events[-1].reason is EndReason.ENVIRONMENT and "newer version" in events[-1].message
    assert set(check_events(events, newer).values()) == {RunStatus.NOT_STARTED}
    assert fsutil.read_json(results.last_run_path(newer.results_dir))["format"] == 99      # left alone
    assert batch.interrupted_run(newer.results_dir) is None and batch.failed_config(newer.results_dir) is None


@posix_only
def test_what_prevents_a_start_is_said_in_sentences(tmp_path, monkeypatch):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 2)
    config = config_for(tmp_path / "r", samples)
    assert batch.preflight_batch(config, engines) == []
    assert os.path.isdir(config.results_dir) and os.listdir(config.results_dir) == []

    assert batch.preflight_batch(config_for(tmp_path / "r", []), engines) == [
        "No sample is ready to run. Add samples on the Samples page."]

    blocker = tmp_path / "a file"
    blocker.write_text("x")
    (problem,) = batch.preflight_batch(config_for(blocker / "results", samples), engines)
    assert problem.startswith("The results folder cannot be written to (%s)." % (blocker / "results"))

    monkeypatch.setattr(batch, "DISK_BASE_BYTES", 10 ** 18)
    (problem,) = batch.preflight_batch(config, engines)
    assert problem.startswith("There is not enough free space on") and "Free some space" in problem
    _runner, events = run_batch(config, engines)
    assert events[-1].reason is EndReason.ENVIRONMENT and "not enough free space" in events[-1].message
    assert "needed" in events[-1].message and set(finals(events).values()) == {RunStatus.NOT_STARTED}
    monkeypatch.undo()

    os.chmod(engines.scepter, 0o644)
    (problem,) = batch.preflight_batch(config, engines)
    assert problem.startswith("Windows or your antivirus stopped part of the program from starting (%s)."
                              % engines.scepter)
    untrimmed = config_for(tmp_path / "r", [Sample("U", samples[0].lanes, trim=False)])
    assert batch.preflight_batch(untrimmed, engines) == []       # the trimmer is not needed for this run
    os.remove(engines.tesseract_asm)
    assert any(p.startswith("Part of the program has disappeared (%s)." % engines.tesseract_asm)
               for p in batch.preflight_batch(config, engines))
    _runner, events = run_batch(untrimmed, engines)
    assert events[-1].reason is EndReason.ENVIRONMENT and "has disappeared" in events[-1].message

    monkeypatch.setenv("TESSERACT_WIN_ENGINES", str(tmp_path / "nowhere"))
    monkeypatch.setattr("tesseract_win.appdirs.is_frozen", lambda: True)
    monkeypatch.setattr("tesseract_win.appdirs.app_dir", lambda: str(tmp_path / "app"))
    assert any("Part of the program has disappeared" in p for p in batch.preflight_batch(config))
    _runner, events = run_batch(config, None)
    assert events[-1].reason is EndReason.ENVIRONMENT and "Extract the zip again" in events[-1].message


@posix_only
def test_notices_battery_memory_and_a_pc_that_slept(tmp_path, monkeypatch):
    engines = stand_in_engines(tmp_path / "engines", {"S01": 1.0})
    samples = stand_in_samples(tmp_path, 1, trim=False)
    power = [(True, 80, True)]
    monkeypatch.setattr(osapi, "power_status", lambda: power[0])
    monkeypatch.setattr(osapi, "available_memory_bytes", lambda: int(2.9 * 1024 ** 3))
    monkeypatch.setattr(batch, "_CLOCK_POLL", 0.05)
    monkeypatch.setattr(batch, "_BATTERY_EVERY", 1)
    jump = [0.0]
    monkeypatch.setattr(batch, "_wall", lambda: time.time() + jump[0])

    def on_event(_runner, event):
        if isinstance(event, SampleStatusChanged) and event.status is RunStatus.ASSEMBLING:
            jump[0] = 9 * 3600.0                                   # the PC slept for nine hours
            power[0] = (True, 15, True)

    config = config_for(tmp_path / "r", samples)
    _runner, events = run_batch(config, engines, on_event=on_event)
    check_events(events, config)
    notices = [e for e in events if isinstance(e, Notice)]
    by_kind = {}
    for notice in notices:
        by_kind.setdefault(notice.kind, []).append(notice)
    assert by_kind["memory"][0].text.startswith("Only 2.9 GB of memory is free.") and by_kind["memory"][0].level == "warn"
    assert by_kind["battery"][0].text.startswith("Running on battery. Connect the charger")
    assert by_kind["battery"][1].text.startswith("Battery at 15 %.") and by_kind["battery"][1].level == "bad"
    (slept,) = by_kind["slept"]
    assert slept.text.startswith("The PC was asleep from ") and slept.text.endswith(
        "The run carried on when it woke up.")
    assert "The PC was asleep from" in open(results.run_log_path(config.results_dir), encoding="utf-8").read()
    assert "The PC was asleep from" in open(results.summary_html_path(config.results_dir), encoding="utf-8").read()

    # The charger comes back: the bar is cleared with an empty text.
    power[0] = (True, 80, True)
    jump[0] = 0.0

    def plug_in(_runner, event):
        if isinstance(event, SampleStatusChanged) and event.status is RunStatus.ASSEMBLING:
            power[0] = (False, 80, True)

    monkeypatch.setattr(osapi, "available_memory_bytes", lambda: 32 * 1024 ** 3)
    monkeypatch.setattr(osapi, "total_memory_bytes", lambda: 64 * 1024 ** 3)
    config = config_for(tmp_path / "r2", samples)
    _runner, events = run_batch(config, engines, on_event=plug_in)
    notices = [e for e in events if isinstance(e, Notice)]
    assert [(n.kind, bool(n.text)) for n in notices] == [("battery", True), ("battery", False)]


@posix_only
def test_a_windows_shutdown_is_remembered_as_the_cause(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"S01": 30})
    samples = stand_in_samples(tmp_path, 2, trim=False)
    config = config_for(tmp_path / "r", samples)

    def shutdown(runner, event):
        if isinstance(event, SampleStatusChanged) and event.status is RunStatus.ASSEMBLING:
            runner.note_interruption("shutdown")
            assert results.load_last_run(config.results_dir).interrupted == "shutdown"
            runner.request_stop(StopMode.NOW)

    _runner, events = run_batch(config, engines, on_event=shutdown)
    assert events[-1].reason is EndReason.STOPPED
    found = batch.interrupted_run(config.results_dir)
    assert found.cause == "windows" and (found.finished, found.total) == (0, 2)
    assert [s.name for s in found.config.samples] == ["S01", "S02"]
    assert "session is ending" in open(results.run_log_path(config.results_dir), encoding="utf-8").read()


@posix_only
def test_try_the_failed_samples_again_keeps_the_memory_of_the_rest(tmp_path):
    engines = stand_in_engines(tmp_path / "engines", {"S02": "crash"})
    samples = stand_in_samples(tmp_path, 3, trim=False)
    config = config_for(tmp_path / "r", samples)
    _runner, events = run_batch(config, engines)
    assert finals(events)["S02"] is RunStatus.FAILED
    assert batch.interrupted_run(config.results_dir) is None     # it reached its end: nothing to continue
    retry = batch.failed_config(config.results_dir)
    assert [s.name for s in retry.samples] == ["S02"] and retry.assembly == config.assembly

    _runner, events = run_batch(retry, engines, resume=True)     # still broken: the second time
    assert "This is the second time." in outcomes(events)["S02"].note
    assert last_states(config.results_dir)["S01"] in results.DONE_STATUSES
    assert [s.name for s in batch.failed_config(config.results_dir).samples] == ["S02"]

    set_plan(tmp_path / "engines", {})
    _runner, events = run_batch(batch.failed_config(config.results_dir), engines, resume=True)
    assert finals(events)["S02"] in results.DONE_STATUSES
    states = last_states(config.results_dir)
    assert set(states) == {"S01", "S02", "S03"} and all(s in results.DONE_STATUSES for s in states.values())
    assert batch.failed_config(config.results_dir) is None
    assert results.count_assemblies(config.results_dir) == 3


@posix_only
def test_a_summary_that_cannot_be_written_is_told_at_the_end_and_fails_nothing(tmp_path, monkeypatch):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 1, trim=False)
    monkeypatch.setattr(batch.summary_report, "write_run_summary", lambda *a, **k: ["run_summary.tsv"])
    config = config_for(tmp_path / "r", samples)
    _runner, events = run_batch(config, engines)
    assert events[-1].reason is EndReason.COMPLETED
    (notice,) = [e for e in events if isinstance(e, Notice)]
    assert notice.text.startswith("run_summary.tsv could not be updated because it is open in another program.")


@posix_only
def test_an_error_inside_the_runner_still_ends_with_batch_finished(tmp_path, monkeypatch):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 2, trim=False)

    def broken(*_args, **_kwargs):
        raise RuntimeError("a bug in the runner")

    monkeypatch.setattr(batch.BatchRunner, "_plan", broken)
    config = config_for(tmp_path / "r", samples)
    _runner, events = run_batch(config, engines)
    assert set(check_events(events, config).values()) == {RunStatus.NOT_STARTED}
    assert events[-1].reason is EndReason.ENVIRONMENT
    assert events[-1].message.startswith("Stopped: something went wrong inside TesserACT-Win.")
    assert osapi.FolderLock.holder_text(config.results_dir) == ""


def test_space_needed_and_the_interrupted_run_of_an_empty_folder(tmp_path):
    pair = tiny_reads(tmp_path / "reads", "a")
    sample = Sample("a", [pair], trim=True)
    size = os.path.getsize(pair[0]) + os.path.getsize(pair[1])
    assert batch.space_needed(sample) == 3 * size + batch.DISK_BASE_BYTES
    assert batch.interrupted_run(str(tmp_path / "nothing here")) is None
    assert batch.failed_config(str(tmp_path / "nothing here")) is None
    (tmp_path / "damaged").mkdir()
    (tmp_path / "damaged" / "last_run.json").write_text("{ not json")
    assert batch.interrupted_run(str(tmp_path / "damaged")) is None


# ============================================================================ added by the review

@posix_only
def test_continue_is_still_offered_after_the_failed_samples_were_tried_again(tmp_path):
    """A Stop leaves S03 and S04 waiting. "Try the failed samples again" then ends as completed;
    the two samples that are still waiting must not be forgotten for that."""
    engines = stand_in_engines(tmp_path / "engines", {"S02": "crash", "S03": 30})
    samples = stand_in_samples(tmp_path, 4, trim=False)
    config = config_for(tmp_path / "r", samples)
    folder = config.results_dir

    def stop_at_third(runner, event):
        if isinstance(event, SampleStatusChanged) and event.name == "S03" and event.status is RunStatus.ASSEMBLING:
            runner.request_stop(StopMode.NOW)

    _runner, events = run_batch(config, engines, on_event=stop_at_third)
    assert events[-1].reason is EndReason.STOPPED
    assert [s.name for s in batch.interrupted_run(folder).config.samples] == ["S03", "S04"]

    set_plan(tmp_path / "engines", {})
    _runner, events = run_batch(batch.failed_config(folder), engines, resume=True)
    assert events[-1].reason is EndReason.COMPLETED and finals(events)["S02"] in results.DONE_STATUSES
    found = batch.interrupted_run(folder)
    assert found is not None, "Continue was lost after 'Try the failed samples again'"
    assert [s.name for s in found.config.samples] == ["S03", "S04"]
    assert (found.finished, found.total, found.cause) == (2, 4, "stopped")

    _runner, events = run_batch(found.config, engines, resume=True)
    assert events[-1].reason is EndReason.COMPLETED
    assert batch.interrupted_run(folder) is None and batch.failed_config(folder) is None
    assert all(s in results.DONE_STATUSES for s in last_states(folder).values())
    assert results.count_assemblies(folder) == 4


@posix_only
def test_a_sample_finished_a_moment_before_a_crash_is_not_offered_for_ever(tmp_path):
    """The program died after S01's status file was written and before last_run.json said so."""
    engines = stand_in_engines(tmp_path / "engines", {"S02": 30})
    samples = stand_in_samples(tmp_path, 2, trim=False)
    config = config_for(tmp_path / "r", samples)
    folder = config.results_dir

    def stop_at_second(runner, event):
        if isinstance(event, SampleStatusChanged) and event.name == "S02" and event.status is RunStatus.ASSEMBLING:
            runner.request_stop(StopMode.NOW)

    run_batch(config, engines, on_event=stop_at_second)
    memory = fsutil.read_json(results.last_run_path(folder))
    memory["states"]["S01"] = {"status": "summarising", "note": ""}
    memory["states"]["S02"] = {"status": "assembling", "note": ""}
    memory["ended_at"] = memory["end_reason"] = None
    memory["started_at"] = "2026-01-01T00:00:00"         # times are kept to the second: not in the same one
    fsutil.atomic_write_json(results.last_run_path(folder), memory)

    found = batch.interrupted_run(folder)
    assert (found.finished, found.total, found.cause) == (1, 2, "unknown")
    assert [s.name for s in found.config.samples] == ["S02"]
    set_plan(tmp_path / "engines", {})
    _runner, events = run_batch(found.config, engines, resume=True)
    assert events[-1].reason is EndReason.COMPLETED
    assert all(s in results.DONE_STATUSES for s in last_states(folder).values()), last_states(folder)
    assert batch.interrupted_run(folder) is None, "a finished sample is offered for Continue for ever"
    assert [c.split(" ")[1] for c in calls(engines) if c.startswith("asm ")].count("S01") == 1


@posix_only
def test_names_that_are_one_folder_on_windows_and_names_that_are_no_folder_are_left_out(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    one, two = tiny_reads(tmp_path / "reads", "a"), tiny_reads(tmp_path / "reads", "b", seed=9)
    outside = tmp_path / "outside"
    samples = [Sample("kp1", [one], trim=False), Sample("KP1", [two], trim=False),
               Sample("..", [one], trim=False), Sample("a/b", [one], trim=False),
               Sample("c\\d", [one], trim=False), Sample("E:x", [one], trim=False),
               Sample("dot.", [one], trim=False), Sample("", [one], trim=False),
               Sample(str(outside), [one], trim=False)]
    config = config_for(tmp_path / "deep" / "r", samples)
    _runner, events = run_batch(config, engines)
    final = check_events(events, config)
    assert final.pop("kp1") in results.DONE_STATUSES
    assert set(final.values()) == {RunStatus.LEFT_OUT}, final
    notes = {e.name: e.note for e in events if isinstance(e, SampleStatusChanged) and e.status is RunStatus.LEFT_OUT}
    assert notes["KP1"].startswith("Another sample in this run is called kp1.") and "Rename" in notes["KP1"]
    assert all("cannot be used as the name of a folder" in notes[n] for n in ("..", "a/b", "c\\d", "E:x", "dot.", ""))
    assert [c.split(" ")[1] for c in calls(engines) if c.startswith("asm ")] == ["kp1"]
    # nothing was written outside the results folder, and nothing but the one sample inside it
    assert sorted(os.listdir(str(tmp_path / "deep"))) == ["r"] and not outside.exists()
    assert sorted(e for e in os.listdir(config.results_dir) if os.path.isdir(os.path.join(config.results_dir, e))) \
        == ["assemblies", "kp1"]
    assert events[-1].counts["left_out"] == 8


@posix_only
def test_a_stop_before_the_thread_has_started_runs_nothing(tmp_path):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 3, trim=False)
    config = config_for(tmp_path / "r", samples)
    events: "queue.Queue" = queue.Queue()
    runner = batch.BatchRunner(config, events, engines=engines)
    runner.request_stop(StopMode.NOW)
    runner.request_stop(StopMode.NOW)                    # twice is as good as once
    runner.cancel_stop()                                 # and a stop NOW cannot be taken back
    assert runner.stop_pending() is StopMode.NOW and runner.current() == ("", 0.0)
    runner.start()
    runner.join(60)
    seen = []
    while not events.empty():
        seen.append(events.get())
    assert seen[-1].reason is EndReason.STOPPED
    assert set(check_events(seen, config).values()) == {RunStatus.NOT_STARTED}
    assert calls(engines) == []
    found = batch.interrupted_run(config.results_dir)
    assert found.cause == "stopped" and len(found.config.samples) == 3
    assert osapi.FolderLock.holder_text(config.results_dir) == ""


@posix_only
def test_a_drive_that_fills_up_in_mid_batch_stops_it_and_keeps_what_was_finished(tmp_path, monkeypatch):
    engines = stand_in_engines(tmp_path / "engines")
    samples = stand_in_samples(tmp_path, 3, trim=False)
    config = config_for(tmp_path / "r", samples)
    real, state = fsutil.free_bytes, {"full": False}
    monkeypatch.setattr(batch.fsutil, "free_bytes", lambda path: 10 * 1024 ** 2 if state["full"] else real(path))

    def fill(_runner, event):
        if isinstance(event, SampleFinished) and event.outcome.name == "S01":
            state["full"] = True

    _runner, events = run_batch(config, engines, on_event=fill)
    end = events[-1]
    assert end.reason is EndReason.ENVIRONMENT
    assert " is full. Free some space, then press Continue. The 1 finished sample is safe." in end.message
    final = check_events(events, config)
    assert final["S01"] in results.DONE_STATUSES and final["S02"] is final["S03"] is RunStatus.NOT_STARTED
    assert results.is_assembled(config.results_dir, "S01")
    state["full"] = False
    found = batch.interrupted_run(config.results_dir)
    assert [s.name for s in found.config.samples] == ["S02", "S03"] and found.finished == 1


@pytest.mark.engines
def test_spaces_accents_brackets_and_other_alphabets_everywhere(tmp_path):
    engines = real_engines()
    pair = make_reads(tmp_path / "reads é [2] ß 读 it's", "KP é[1] ü")
    lanes = split_lanes(pair, tmp_path / "lanes [3] ö 日本")
    samples = [Sample("KP é[1] 日本", [pair], trim=True), Sample("Ωmega (2) & it's", [pair], trim=False),
               Sample("Łanes [3]", lanes, trim=True)]
    config = config_for(tmp_path / "résultats [1] 日本語 & x'y", samples)
    _runner, events = run_batch(config, engines)
    final = check_events(events, config)
    assert set(final.values()) == {RunStatus.DONE}, [(o.name, o.note, o.next_step) for o in outcomes(events).values()]
    folder = config.results_dir
    for sample in samples:
        assert results.is_assembled(folder, sample.name)
        assert os.path.getsize(results.collected_fasta(folder, sample.name)) > 90_000
        assert not os.path.exists(results.in_progress_dir(folder, sample.name))
        for name in ("contigs.fasta", "SUMMARY.txt", "assembly_stats.tsv", "preflight.txt", "assembly.log",
                     "report.html"):
            assert os.path.isfile(os.path.join(results.assembly_dir(folder, sample.name), name)), name
    trimmed = results.trimming_dir(folder, "KP é[1] 日本")
    assert sorted(os.listdir(trimmed)) == sorted([
        "KP é[1] 日本_trimmed_R1.fastq.gz", "KP é[1] 日本_trimmed_R2.fastq.gz", "trimming_report.html",
        "trimming_report.json", "trimming.log"])
    assert not os.path.exists(results.trimming_dir(folder, "Ωmega (2) & it's"))
    for name in ("run_summary.html", "run_summary.tsv", "run_log.txt", "last_run.json"):
        assert os.path.isfile(os.path.join(folder, name)), name
    assert not os.path.exists(results.lock_path(folder))
    assert batch.interrupted_run(folder) is None


def _cut_on_a_record_boundary(source, target, records=200):
    """A gzip file that ends after whole records but never got its end: what an interrupted copy
    or download can leave. The trimmer takes it for a complete file."""
    import zlib
    lines = gzip.decompress(open(source, "rb").read()).split(b"\n")
    packer = zlib.compressobj(1, zlib.DEFLATED, 31)
    with open(target, "wb") as handle:
        handle.write(packer.compress(b"\n".join(lines[:records * 4]) + b"\n") + packer.flush(zlib.Z_FULL_FLUSH))
    return target


@pytest.mark.engines
@pytest.mark.parametrize("trim", [True, False], ids=["trimmed", "untrimmed"])
def test_empty_files_and_a_gzip_cut_on_a_record_boundary_fail_in_words(tmp_path, reads, trim):
    engines = real_engines()
    bad = tmp_path / "bad"
    bad.mkdir()

    def path(name):
        return str(bad / name)

    for name in ("zero_R1.fastq.gz", "zero_R2.fastq.gz", "plain_R1.fastq", "plain_R2.fastq"):
        open(path(name), "wb").close()
    for name in ("nothing_R1.fastq.gz", "nothing_R2.fastq.gz"):
        with open(path(name), "wb") as handle:
            handle.write(gzip.compress(b""))
    samples = [
        Sample("zero_bytes", [(path("zero_R1.fastq.gz"), path("zero_R2.fastq.gz"))], trim=trim),
        Sample("plain_zero", [(path("plain_R1.fastq"), path("plain_R2.fastq"))], trim=trim),
        Sample("gzip_of_nothing", [(path("nothing_R1.fastq.gz"), path("nothing_R2.fastq.gz"))], trim=trim),
        Sample("both_cut", [(_cut_on_a_record_boundary(reads[0], path("cut_R1.fastq.gz")),
                             _cut_on_a_record_boundary(reads[1], path("cut_R2.fastq.gz")))], trim=trim),
        Sample("one_cut", [(reads[0], _cut_on_a_record_boundary(reads[1], path("cut2_R2.fastq.gz")))], trim=trim),
        Sample("same_file_twice", [(reads[0], reads[0])], trim=trim),
        Sample("good", [reads], trim=trim),
    ]
    config = config_for(tmp_path / "r", samples)
    _runner, events = run_batch(config, engines)
    final = check_events(events, config)
    out = outcomes(events)
    assert events[-1].reason is EndReason.COMPLETED and final.pop("good") is RunStatus.DONE
    assert set(final.values()) == {RunStatus.FAILED}, final
    assert out["zero_bytes"].note.startswith("A read file is empty (zero_R1.fastq.gz)")
    assert out["plain_zero"].note.startswith("A read file is empty (plain_R1.fastq)")
    assert "FASTQ" in out["gzip_of_nothing"].note
    assert out["both_cut"].note.startswith("A read file is incomplete or damaged (cut_R1.fastq.gz)")
    assert out["one_cut"].note.startswith("A read file is incomplete or damaged (cut2_R2.fastq.gz)")
    assert out["same_file_twice"].note.startswith("The same file was given as Read 1 and as Read 2")
    for name in final:
        assert out[name].next_step and not results.is_assembled(config.results_dir, name)
        assert not os.path.exists(results.in_progress_dir(config.results_dir, name))
        assert not os.path.exists(results.collected_fasta(config.results_dir, name))
        assert not os.path.exists(results.assembly_dir(config.results_dir, name))


_STAND_IN_HELPER = """\
import queue, sys
from tesseract_win import batch
from tesseract_win.model import AssemblySettings, BatchFinished, EnginePaths, Existing, RunConfig, Sample
folder, asm, scepter, read1, read2 = sys.argv[1:6]
samples = [Sample("S%02d" % i, [(read1, read2)], trim=True) for i in (1, 2)]
config = RunConfig(folder, samples, assembly=AssemblySettings(threads=4, know_genome_size=True, genome_size_mb=0.1),
                   existing=Existing.OVERWRITE)
events = queue.Queue()
batch.BatchRunner(config, events, engines=EnginePaths(asm, scepter, "")).start()
while not isinstance(events.get(), BatchFinished):
    pass
"""


def _processes_started_from(folder):
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as handle:
                    if folder.encode() in handle.read():
                        found.append(entry)
            except OSError:
                continue
    return found


@posix_only
@pytest.mark.parametrize("stage", ["trimming", "assembling"])
def test_the_program_killed_during_a_redo_leaves_no_engine_and_the_earlier_result(tmp_path, stage):
    """Power cut or Task Manager while a finished sample is being done again (decision D2)."""
    if not os.path.isdir("/proc"):
        pytest.skip("needs /proc")
    plan = {"trim:S01": 60} if stage == "trimming" else {"S01": 60}
    engines = stand_in_engines(tmp_path / "engines")
    samples = [Sample("S%02d" % i, [tiny_reads(tmp_path / "reads", "shared")], trim=True) for i in (1, 2)]
    config = config_for(tmp_path / "r", samples)
    folder = config.results_dir
    run_batch(config, engines)
    before = snapshot(results.sample_dir(folder, "S01"))
    fasta = open(results.collected_fasta(folder, "S01"), "rb").read()
    summary = open(results.summary_tsv_path(folder), "rb").read()

    set_plan(tmp_path / "engines", plan)
    time.sleep(1.1)                                      # times are kept to the second: a later one
    script = tmp_path / "helper.py"
    script.write_text(_STAND_IN_HELPER, encoding="utf-8")
    helper = subprocess.Popen([sys.executable, str(script), folder, engines.tesseract_asm, engines.scepter,
                               *samples[0].lanes[0]], env=dict(os.environ, PYTHONPATH=REPO), cwd=str(tmp_path),
                              stdin=subprocess.DEVNULL)
    engine_folder = os.path.dirname(engines.tesseract_asm)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            data = fsutil.read_json(results.last_run_path(folder), {}) or {}
            if (data.get("states") or {}).get("S01", {}).get("status") == stage and not data.get("ended_at") \
                    and _processes_started_from(engine_folder):
                break
            assert helper.poll() is None, "the helper ended too early"
            time.sleep(0.02)
        else:
            pytest.fail("the redo never reached %s" % stage)
        running = _processes_started_from(engine_folder)
        os.kill(helper.pid, signal.SIGKILL)
        helper.wait(30)
    finally:
        if helper.poll() is None:
            helper.kill()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(os.path.exists("/proc/%s" % p) for p in running):
        time.sleep(0.05)
    assert not any(os.path.exists("/proc/%s" % p) for p in running), "an engine outlived the program"

    # Before anything heals it: the earlier result is whole and is still the result.
    assert results.is_assembled(folder, "S01") and results.is_assembled(folder, "S02")
    assert open(results.collected_fasta(folder, "S01"), "rb").read() == fasta
    assert open(results.summary_tsv_path(folder), "rb").read() == summary
    after = snapshot(results.sample_dir(folder, "S01"))
    assert {k: v for k, v in after.items() if not k.startswith("_in_progress/")} == before
    found = batch.interrupted_run(folder)
    assert found.cause == "unknown" and [s.name for s in found.config.samples] == ["S01", "S02"]
    assert found.config.existing is Existing.OVERWRITE and batch.preflight_batch(found.config, engines) == []

    set_plan(tmp_path / "engines", {})
    _runner, events = run_batch(found.config, engines, resume=True)
    assert all(s in results.DONE_STATUSES for s in check_events(events, found.config).values())
    assert not os.path.exists(results.in_progress_dir(folder, "S01"))
    assert batch.interrupted_run(folder) is None
