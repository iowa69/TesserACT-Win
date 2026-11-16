# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""What is on disk in the results folder: layout, status files, safe swaps, the batch memory."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from tesseract_win import branding, fsutil, results, versions
from tesseract_win.model import (AssemblySettings, EndReason, Existing, RunConfig, RunStatus, Sample,
                                 SampleOutcome, TrimSettings)

FINGERPRINT = "trim=standard;assembly=standard"
SETTINGS_TEXT = "standard trimming, standard assembly"


# --------------------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Retries wait about two seconds in all; the tests do not need the wait."""
    monkeypatch.setattr(fsutil.time, "sleep", lambda seconds: None)


@pytest.fixture
def rdir(tmp_path):
    return str(tmp_path / "results")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def snapshot(folder):
    """Every file below ``folder`` with its content; {} when the folder is not there."""
    out = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            full = os.path.join(root, name)
            with open(full, "rb") as handle:
                out[os.path.relpath(full, folder).replace(os.sep, "/")] = handle.read()
    return out


def make_reads(folder, name, size=1000):
    """Two read files of a known size; returns them as a Sample."""
    os.makedirs(folder, exist_ok=True)
    r1 = os.path.join(folder, name + "_R1.fastq.gz")
    r2 = os.path.join(folder, name + "_R2.fastq.gz")
    for path, extra in ((r1, 0), (r2, 7)):
        with open(path, "wb") as handle:
            handle.write(b"x" * (size + extra))
    return Sample(name=name, lanes=[(r1, r2)], size_bytes=2 * size + 7)


def outcome_for(name, status=RunStatus.DONE, **extra):
    values = dict(name=name, status=status, trimmed=True, contigs=71, total_length=5482113,
                  largest=611204, n50=214880, depth=63, seconds=1234.4,
                  finished_at="2026-11-17T03:42:10")
    if status is RunStatus.FAILED:
        values.update(contigs=None, total_length=None, largest=None, n50=None, depth=None,
                      note="A read file is incomplete or damaged.", next_step="Copy it again.")
    values.update(extra)
    return SampleOutcome(**values)


def fill_attempt(rdir, name, tag, trimmed=True):
    """Start an attempt and fill _in_progress the way the engines would."""
    work = results.begin_attempt(rdir, name)
    asm = results.in_progress_assembly_dir(rdir, name)
    write(os.path.join(asm, "contigs.fasta"), ">contig_1 %s\nACGTACGT\n" % tag)
    write(os.path.join(asm, "report.html"), "<html>%s</html>" % tag)
    write(os.path.join(asm, "assembly.log"), "assembly log %s\n" % tag)
    write(os.path.join(asm, "preflight.txt"), "preflight %s\n" % tag)
    write(os.path.join(asm, "SUMMARY.txt"), "SUMMARY %s\n" % tag)
    write(os.path.join(asm, "assembly_stats.tsv"), "contigs\t71\n")
    write(os.path.join(asm, "attempt1_default_k", "assembly.log"), "first try %s\n" % tag)
    if trimmed:
        trim = results.in_progress_trimming_dir(rdir, name)
        r1, r2 = results.trimmed_read_names(name)
        write(os.path.join(trim, r1), "trimmed reads 1 %s" % tag)
        write(os.path.join(trim, r2), "trimmed reads 2 %s" % tag)
        write(os.path.join(trim, "trimming_report.html"), "<html>trim %s</html>" % tag)
        write(os.path.join(trim, "trimming_report.json"), '{"tag": "%s"}' % tag)
        write(os.path.join(trim, "trimming.log"), "trimming log %s\n" % tag)
    write(os.path.join(results.in_progress_joined_dir(rdir, name), "joined_R1.fastq.gz"), "joined")
    return work


def status_args(sample=None, fingerprint=FINGERPRINT, text=SETTINGS_TEXT, trimmed=True):
    reads = results.reads_of(sample) if sample is not None else [("/data/x_R1.fastq.gz", 10),
                                                                   ("/data/x_R2.fastq.gz", 12)]
    return dict(reads=reads, settings_fingerprint=fingerprint, settings_text=text, trimmed=trimmed)


def finish(rdir, name, tag, *, sample=None, trimmed=True, keep_trimmed_reads=True,
           status=RunStatus.DONE, fingerprint=FINGERPRINT, text=SETTINGS_TEXT):
    """A whole good attempt: fill, commit, collect, status file last."""
    fill_attempt(rdir, name, tag, trimmed=trimmed)
    committed = results.commit_attempt(rdir, name, keep_trimmed_reads=keep_trimmed_reads)
    assert committed.in_place
    results.collect(rdir, name)
    args = status_args(sample, fingerprint=fingerprint, text=text, trimmed=trimmed)
    results.write_status(rdir, outcome_for(name, status, trimmed=trimmed), **args)


def contigs_tag(path):
    return read(path).split("\n")[0].split(" ", 1)[1]


# --------------------------------------------------------------------------- paths (decision D1)

def test_every_path_of_decision_d1(tmp_path):
    r = str(tmp_path)
    j = os.path.join
    assert results.summary_html_path(r) == j(r, "run_summary.html")
    assert results.summary_tsv_path(r) == j(r, "run_summary.tsv")
    assert results.run_log_path(r) == j(r, "run_log.txt")
    assert results.last_run_path(r) == j(r, "last_run.json")
    assert results.lock_path(r) == j(r, "tesseract-win.lock")
    assert results.assemblies_dir(r) == j(r, "assemblies")
    assert results.collected_fasta(r, "KP_001") == j(r, "assemblies", "KP_001.fasta")
    assert results.sample_dir(r, "KP_001") == j(r, "KP_001")
    assert results.status_path(r, "KP_001") == j(r, "KP_001", "sample_status.json")
    assert results.trimming_dir(r, "KP_001") == j(r, "KP_001", "trimming")
    assert results.assembly_dir(r, "KP_001") == j(r, "KP_001", "assembly")
    assert results.in_progress_dir(r, "KP_001") == j(r, "KP_001", "_in_progress")
    assert results.in_progress_trimming_dir(r, "KP_001") == j(r, "KP_001", "_in_progress", "trimming")
    assert results.in_progress_assembly_dir(r, "KP_001") == j(r, "KP_001", "_in_progress", "assembly")
    assert results.in_progress_joined_dir(r, "KP_001") == j(r, "KP_001", "_in_progress", "joined")
    assert results.last_failed_dir(r, "KP_001") == j(r, "KP_001", "last_failed_attempt")
    assert results.new_result_dir(r, "KP_001") == j(r, "KP_001", "new_result")
    assert results.contigs_path(r, "KP_001") == j(r, "KP_001", "assembly", "contigs.fasta")
    assert results.report_html_path(r, "KP_001") == j(r, "KP_001", "assembly", "report.html")
    assert results.trimming_report_html_path(r, "KP_001") == \
        j(r, "KP_001", "trimming", "trimming_report.html")
    assert results.trimmed_read_names("KP_001") == ("KP_001_trimmed_R1.fastq.gz",
                                                    "KP_001_trimmed_R2.fastq.gz")


def test_names_that_would_collide_with_the_folder_s_own_entries():
    assert results.is_reserved_name("assemblies")
    assert results.is_reserved_name("Assemblies")
    assert results.is_reserved_name("run_summary.html")
    assert not results.is_reserved_name("KP_001")


# --------------------------------------------------------------------------- status files

def test_status_file_says_what_the_result_was_made_from_and_with(rdir, tmp_path):
    sample = make_reads(str(tmp_path / "reads"), "KP_001")
    finish(rdir, "KP_001", "v1", sample=sample)

    stored = json.loads(read(results.status_path(rdir, "KP_001")))
    assert stored["format"] == versions.RESULTS_FORMAT
    assert stored["program_version"] == versions.APP_VERSION
    assert stored["tesseract_version"] == versions.TESSERACT_VERSION
    assert stored["scepter_version"] == versions.SCEPTER_VERSION
    assert stored["status"] == "done"
    assert stored["finished_at"] == "2026-11-17T03:42:10"
    assert stored["settings_fingerprint"] == FINGERPRINT
    assert stored["settings_text"] == SETTINGS_TEXT
    assert [(r["name"], r["size"]) for r in stored["reads"]] == [
        ("KP_001_R1.fastq.gz", 1000), ("KP_001_R2.fastq.gz", 1007)]

    record = results.read_status(rdir, "KP_001")
    assert isinstance(record, results.SampleRecord)
    assert record.name == "KP_001" and record.status is RunStatus.DONE
    assert record.done and record.assembled and record.trimmed
    assert record.finished_at == "2026-11-17T03:42:10"
    assert record.reads == [("KP_001_R1.fastq.gz", 1000), ("KP_001_R2.fastq.gz", 1007)]
    assert record.settings_fingerprint == FINGERPRINT and record.settings_text == SETTINGS_TEXT
    assert (record.program_version, record.tesseract_version, record.scepter_version) == \
        (versions.APP_VERSION, versions.TESSERACT_VERSION, versions.SCEPTER_VERSION)
    out = record.outcome
    assert (out.contigs, out.total_length, out.largest, out.n50, out.depth) == \
        (71, 5482113, 611204, 214880, 63)
    assert out.folder == results.sample_dir(rdir, "KP_001")
    assert out.report_html == results.report_html_path(rdir, "KP_001")


def test_status_file_is_text_with_lf_and_utf8(rdir):
    finish(rdir, "KP_001", "v1", text="mode Careful — très soigné")
    with open(results.status_path(rdir, "KP_001"), "rb") as handle:
        raw = handle.read()
    assert b"\r" not in raw and raw.endswith(b"\n")
    assert "très soigné" in raw.decode("utf-8")


def test_finishing_time_is_filled_in_when_the_outcome_has_none(rdir):
    fill_attempt(rdir, "KP_001", "v1")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    results.write_status(rdir, outcome_for("KP_001", finished_at=""), **status_args())
    assert len(results.read_status(rdir, "KP_001").finished_at) == 19


def test_paths_in_the_status_file_survive_a_move_of_the_results_folder(rdir, tmp_path):
    fill_attempt(rdir, "KP_001", "v1")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    in_progress_log = os.path.join(results.in_progress_assembly_dir(rdir, "KP_001"), "assembly.log")
    results.write_status(rdir, outcome_for("KP_001", log=in_progress_log), **status_args())
    stored = json.loads(read(results.status_path(rdir, "KP_001")))
    assert stored["outcome"]["log"] == "assembly/assembly.log"
    assert stored["outcome"]["folder"] == ""

    moved = str(tmp_path / "moved to a stick")
    shutil.move(rdir, moved)
    record = results.read_status(moved, "KP_001")
    assert record.outcome.folder == results.sample_dir(moved, "KP_001")
    assert record.outcome.log == os.path.join(results.assembly_dir(moved, "KP_001"), "assembly.log")
    assert record.outcome.report_html == results.report_html_path(moved, "KP_001")
    assert results.is_assembled(moved, "KP_001")


@pytest.mark.parametrize("content", [
    "", "{", '{"format": 1, "status": "do', "[1, 2, 3]", '"done"', '{"status": "done"}',
    '{"format": 1}', '{"format": 1, "status": "no such status"}', '{"format": "x", "status": "done"}',
    "\x00\x00\x00\x00",
])
def test_missing_or_damaged_status_file_gives_none(rdir, content):
    assert results.read_status(rdir, "KP_001") is None            # missing
    write(results.contigs_path(rdir, "KP_001"), ">c\nACGT\n")
    write(results.status_path(rdir, "KP_001"), content)
    assert results.read_status(rdir, "KP_001") is None
    assert results.is_assembled(rdir, "KP_001") is False
    assert results.all_records(rdir) == []


def test_odd_values_in_a_status_file_do_not_break_reading(rdir):
    write(results.contigs_path(rdir, "KP_001"), ">c\nACGT\n")
    write(results.status_path(rdir, "KP_001"), json.dumps({
        "format": 1, "status": "done", "finished_at": 5, "reads": [{"name": 3}, "x", {"name": "a", "size": "12"}],
        "outcome": {"contigs": "71", "n50": 2.0, "depth": "many", "notes": "not a list",
                    "seconds": "soon", "note": None, "unknown_field": 1}}))
    record = results.read_status(rdir, "KP_001")
    assert record.assembled
    assert record.outcome.contigs == 71 and record.outcome.n50 == 2 and record.outcome.depth is None
    assert record.outcome.notes == [] and record.outcome.note == "" and record.outcome.seconds == 0.0
    assert record.reads == [("a", 12)] and record.finished_at == ""


def test_newer_format_is_refused(rdir):
    finish(rdir, "KP_001", "v1")
    path = results.status_path(rdir, "KP_001")
    stored = json.loads(read(path))
    stored["format"] = versions.RESULTS_FORMAT + 1
    write(path, json.dumps(stored))
    sample = Sample("KP_001", [("a_R1.fastq.gz", "a_R2.fastq.gz")])

    with pytest.raises(results.NewerFormat) as caught:
        results.read_status(rdir, "KP_001")
    assert caught.value.found == versions.RESULTS_FORMAT + 1 and caught.value.path == path
    for call in (lambda: results.is_assembled(rdir, "KP_001"),
                 lambda: results.all_records(rdir),
                 lambda: results.existing_results(rdir, [sample], lambda s: FINGERPRINT),
                 lambda: results.name_clashes(rdir, [sample]),
                 lambda: results.write_status(rdir, outcome_for("KP_001", RunStatus.FAILED),
                                              **status_args())):
        with pytest.raises(results.NewerFormat):
            call()
    assert results.made_by_newer_version(rdir)
    assert results.is_results_folder(rdir)          # still one of ours: refused, not mistaken
    # Nothing was changed by the refusals.
    assert json.loads(read(path))["format"] == versions.RESULTS_FORMAT + 1


def test_older_or_equal_format_is_read(rdir):
    finish(rdir, "KP_001", "v1")
    path = results.status_path(rdir, "KP_001")
    stored = json.loads(read(path))
    stored["format"] = 0
    write(path, json.dumps(stored))
    assert results.read_status(rdir, "KP_001").format == 0
    assert not results.made_by_newer_version(rdir)


def test_is_assembled_is_decision_d2_exactly(rdir):
    assert results.is_assembled(rdir, "KP_001") is False                    # nothing at all
    write(results.contigs_path(rdir, "KP_001"), ">c\nACGT\n")
    assert results.is_assembled(rdir, "KP_001") is False                    # contigs, no status file
    write(results.status_path(rdir, "KP_001"), json.dumps({"format": 1, "status": "failed"}))
    assert results.is_assembled(rdir, "KP_001") is False                    # status says failed
    for value in ("stopped", "skipped", "assembling", "waiting", "not_started", "left_out"):
        write(results.status_path(rdir, "KP_001"), json.dumps({"format": 1, "status": value}))
        assert results.is_assembled(rdir, "KP_001") is False
    write(results.status_path(rdir, "KP_001"), json.dumps({"format": 1, "status": "done"}))
    assert results.is_assembled(rdir, "KP_001") is True                     # both
    write(results.status_path(rdir, "KP_001"), json.dumps({"format": 1, "status": "done_notes"}))
    assert results.is_assembled(rdir, "KP_001") is True                     # done, with notes
    os.remove(results.contigs_path(rdir, "KP_001"))
    assert results.is_assembled(rdir, "KP_001") is False                    # status, no contigs
    os.makedirs(results.contigs_path(rdir, "KP_001"))
    assert results.is_assembled(rdir, "KP_001") is False                    # a folder is not a file


def test_done_cannot_be_recorded_without_contigs(rdir):
    results.begin_attempt(rdir, "KP_001")
    with pytest.raises(results.AttemptIncomplete):
        results.write_status(rdir, outcome_for("KP_001"), **status_args())
    assert not os.path.exists(results.status_path(rdir, "KP_001"))


class Crash(BaseException):
    """Stands for the process dying: nothing may catch it and carry on."""


def test_status_file_is_never_left_half_written(rdir, monkeypatch):
    finish(rdir, "KP_001", "old")
    path = results.status_path(rdir, "KP_001")
    before = read(path)

    # 1. The process dies after the new text is on disk under its temporary name.
    real_replace = os.replace

    def dying_replace(src, dst):
        if os.path.basename(dst) == "sample_status.json":
            raise Crash()
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", dying_replace)
    with pytest.raises(Crash):
        results.write_status(rdir, outcome_for("KP_001", contigs=5), **status_args())
    monkeypatch.setattr(os, "replace", real_replace)
    assert read(path) == before
    assert results.read_status(rdir, "KP_001").outcome.contigs == 71

    # 2. The process dies half-way through writing the text.
    real_dumps = json.dumps

    def dying_dumps(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(fsutil.json, "dumps", dying_dumps)
    with pytest.raises(Crash):
        results.write_status(rdir, outcome_for("KP_001", contigs=5), **status_args())
    monkeypatch.setattr(fsutil.json, "dumps", real_dumps)
    assert read(path) == before

    # 3. The disk refuses the rename: an error, the old file intact, no temporary file left.
    def refusing_replace(src, dst):
        raise PermissionError(13, "in use", dst)

    monkeypatch.setattr(os, "replace", refusing_replace)
    with pytest.raises(PermissionError):
        results.write_status(rdir, outcome_for("KP_001", contigs=5), **status_args())
    monkeypatch.setattr(os, "replace", real_replace)
    assert read(path) == before
    leftovers = [n for n in os.listdir(results.sample_dir(rdir, "KP_001")) if ".tmp" in n]
    assert len(leftovers) <= 1      # only the one of the simulated crash in step 1

    # The next attempt clears the temporary file of the crash and never the real one.
    results.begin_attempt(rdir, "KP_001")
    assert [n for n in os.listdir(results.sample_dir(rdir, "KP_001")) if ".tmp" in n] == []
    assert read(path) == before


def test_first_status_file_is_all_or_nothing(rdir, monkeypatch):
    fill_attempt(rdir, "KP_001", "v1")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    real_replace = os.replace

    def dying_replace(src, dst):
        raise Crash()

    monkeypatch.setattr(os, "replace", dying_replace)
    with pytest.raises(Crash):
        results.write_status(rdir, outcome_for("KP_001"), **status_args())
    monkeypatch.setattr(os, "replace", real_replace)
    assert not os.path.exists(results.status_path(rdir, "KP_001"))
    assert results.read_status(rdir, "KP_001") is None
    assert results.is_assembled(rdir, "KP_001") is False


def test_failed_first_attempt_is_recorded_with_its_reason(rdir):
    fill_attempt(rdir, "KP_001", "v1")
    log = os.path.join(results.in_progress_assembly_dir(rdir, "KP_001"), "assembly.log")
    kept = results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    path = results.write_status(rdir, outcome_for("KP_001", RunStatus.FAILED, log=log), **status_args())
    assert path == results.status_path(rdir, "KP_001")
    record = results.read_status(rdir, "KP_001")
    assert record.status is RunStatus.FAILED and not record.done and not record.assembled
    assert record.outcome.note == "A read file is incomplete or damaged."
    assert record.outcome.next_step == "Copy it again."
    assert results.is_assembled(rdir, "KP_001") is False
    # The log the outcome named inside _in_progress is found where it was kept.
    expected = os.path.join(results.last_failed_dir(rdir, "KP_001"), "assembly", "assembly.log")
    assert record.outcome.log == expected and expected in kept


def test_failed_redo_keeps_the_done_status_and_notes_the_failure(rdir):
    finish(rdir, "KP_001", "old")
    before = json.loads(read(results.status_path(rdir, "KP_001")))
    fill_attempt(rdir, "KP_001", "new")
    results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    results.write_status(rdir, outcome_for("KP_001", RunStatus.FAILED, finished_at="2026-11-18T10:00:00"),
                         **status_args(fingerprint="other"))
    after = json.loads(read(results.status_path(rdir, "KP_001")))
    assert after.pop("last_attempt") == {"status": "failed", "note": "A read file is incomplete or damaged.",
                                         "next_step": "Copy it again.", "finished_at": "2026-11-18T10:00:00"}
    before.pop("last_attempt")
    assert after == before                                  # everything else untouched
    record = results.read_status(rdir, "KP_001")
    assert record.assembled and record.last_attempt["status"] == "failed"
    assert results.is_assembled(rdir, "KP_001")
    assert contigs_tag(results.contigs_path(rdir, "KP_001")) == "old"


def test_a_done_status_without_its_assembly_does_not_shield_a_failure(rdir):
    finish(rdir, "KP_001", "old")
    shutil.rmtree(results.assembly_dir(rdir, "KP_001"))      # the user deleted the assembly
    results.write_status(rdir, outcome_for("KP_001", RunStatus.FAILED), **status_args())
    assert results.read_status(rdir, "KP_001").status is RunStatus.FAILED


@pytest.mark.parametrize("status", [RunStatus.STOPPED, RunStatus.SKIPPED, RunStatus.NOT_STARTED,
                                    RunStatus.LEFT_OUT, RunStatus.WAITING])
def test_other_endings_write_no_status_file(rdir, status):
    results.begin_attempt(rdir, "KP_001")
    assert results.write_status(rdir, outcome_for("KP_001", status), **status_args()) == ""
    assert not os.path.exists(results.status_path(rdir, "KP_001"))
    finish(rdir, "KP_002", "old")
    before = read(results.status_path(rdir, "KP_002"))
    assert results.write_status(rdir, outcome_for("KP_002", status), **status_args()) == ""
    assert read(results.status_path(rdir, "KP_002")) == before


def test_all_records_are_naturally_sorted_and_skip_what_is_not_a_sample(rdir):
    for name in ("KP_10", "KP_2", "kp_1", "SA_7"):
        finish(rdir, name, name)
    fill_attempt(rdir, "EC_1", "x")
    results.abandon_attempt(rdir, "EC_1", keep_logs=True)
    results.write_status(rdir, outcome_for("EC_1", RunStatus.FAILED), **status_args())
    os.makedirs(os.path.join(rdir, "not a sample"))
    write(os.path.join(rdir, "damaged", "sample_status.json"), "{")
    write(os.path.join(rdir, "run_log.txt"), "log")
    records = results.all_records(rdir)
    assert [r.name for r in records] == ["EC_1", "kp_1", "KP_2", "KP_10", "SA_7"]
    assert [r.status for r in records][0] is RunStatus.FAILED
    assert results.all_records(os.path.join(rdir, "nowhere")) == []


# --------------------------------------------------------------------------- existing results

def test_existing_results_and_name_clashes(rdir, tmp_path):
    reads = str(tmp_path / "run 1")
    same = make_reads(reads, "KP_001")
    other_settings = make_reads(reads, "KP_002")
    failed = make_reads(reads, "KP_003")
    fresh = make_reads(reads, "KP_004")
    finish(rdir, "KP_001", "a", sample=same)
    finish(rdir, "KP_002", "b", sample=other_settings, fingerprint="trim=off;mode=careful",
           text="not trimmed; mode Careful")
    fill_attempt(rdir, "KP_003", "c")
    results.abandon_attempt(rdir, "KP_003", keep_logs=True)
    results.write_status(rdir, outcome_for("KP_003", RunStatus.FAILED), **status_args(failed))

    samples = [same, other_settings, failed, fresh]
    found = results.existing_results(rdir, samples, lambda sample: FINGERPRINT)
    assert sorted(found) == ["KP_001", "KP_002"]        # failed and new samples are not "existing"
    assert found["KP_001"].same_reads and found["KP_001"].same_settings
    assert found["KP_001"].settings_summary == "" and found["KP_001"].finished_at == "2026-11-17T03:42:10"
    assert found["KP_002"].same_reads and not found["KP_002"].same_settings
    assert found["KP_002"].settings_summary == "not trimmed; mode Careful"
    assert results.name_clashes(rdir, samples) == []

    # The fingerprint may also be one string for all, or a dict by name.
    assert results.existing_results(rdir, samples, FINGERPRINT)["KP_001"].same_settings
    by_name = results.existing_results(rdir, samples, {"KP_002": "trim=off;mode=careful"})
    assert by_name["KP_002"].same_settings and not by_name["KP_001"].same_settings


def test_a_moved_reads_folder_is_still_the_same_reads(rdir, tmp_path):
    sample = make_reads(str(tmp_path / "run 1"), "KP_001")
    finish(rdir, "KP_001", "a", sample=sample)
    moved = make_reads(str(tmp_path / "archive" / "2026" / "run 1"), "KP_001")
    assert results.existing_results(rdir, [moved], FINGERPRINT)["KP_001"].same_reads
    assert results.name_clashes(rdir, [moved]) == []
    # Windows does not tell KP_001_R1 from kp_001_r1.
    assert results.same_reads([("KP_001_R1.fastq.gz", 5)], [("C:\\x\\kp_001_r1.FASTQ.gz", 5)])


def test_same_name_from_different_reads_is_a_clash(rdir, tmp_path):
    finish(rdir, "KP_001", "a", sample=make_reads(str(tmp_path / "march"), "KP_001", size=1000))
    other_size = make_reads(str(tmp_path / "april"), "KP_001", size=2222)
    assert results.name_clashes(rdir, [other_size]) == ["KP_001"]
    assert results.existing_results(rdir, [other_size], FINGERPRINT)["KP_001"].same_reads is False

    # Same sizes, but one file has another name: other reads all the same.
    other_names = make_reads(str(tmp_path / "may"), "KP_001", size=1000)
    new_read1 = other_names.read1.replace("KP_001_R1", "isolate9_R1")
    os.rename(other_names.read1, new_read1)
    other_names.lanes = [(new_read1, other_names.read2)]
    assert results.name_clashes(rdir, [other_names]) == ["KP_001"]

    two_lanes = make_reads(str(tmp_path / "march"), "KP_001", size=1000)
    two_lanes.lanes = two_lanes.lanes * 2
    assert results.name_clashes(rdir, [two_lanes]) == ["KP_001"]       # more files than before


def test_unreachable_reads_are_not_called_a_clash(rdir, tmp_path):
    sample = make_reads(str(tmp_path / "usb"), "KP_001")
    finish(rdir, "KP_001", "a", sample=sample)
    shutil.rmtree(str(tmp_path / "usb"))                 # the drive was unplugged
    assert results.reads_of(sample) == [(sample.read1, -1), (sample.read2, -1)]
    assert results.name_clashes(rdir, [sample]) == []
    assert results.existing_results(rdir, [sample], FINGERPRINT)["KP_001"].same_reads


def test_a_result_made_with_another_engine_version_counts_as_other_settings(rdir, tmp_path):
    sample = make_reads(str(tmp_path / "reads"), "KP_001")
    finish(rdir, "KP_001", "a", sample=sample)
    path = results.status_path(rdir, "KP_001")
    stored = json.loads(read(path))
    stored["tesseract_version"] = "1.5.0"
    write(path, json.dumps(stored))
    found = results.existing_results(rdir, [sample], FINGERPRINT)["KP_001"]
    assert not found.same_settings
    assert found.settings_summary == "an earlier version (TesserACT 1.5.0)"

    stored["settings_fingerprint"] = "something else"
    stored["scepter_version"] = "9.0.0"
    write(path, json.dumps(stored))
    found = results.existing_results(rdir, [sample], FINGERPRINT)["KP_001"]
    assert found.settings_summary == \
        "standard trimming, standard assembly; another version (TesserACT 1.5.0, scepter 9.0.0)"

    # The window's own version alone changes nothing about the result.
    stored.update(tesseract_version=versions.TESSERACT_VERSION, scepter_version=versions.SCEPTER_VERSION,
                  settings_fingerprint=FINGERPRINT, program_version="0.0.1")
    write(path, json.dumps(stored))
    assert results.existing_results(rdir, [sample], FINGERPRINT)["KP_001"].same_settings


def test_folders_this_program_did_not_make_are_recognised(rdir):
    assert results.foreign_content(rdir, "KP_001") == ""               # no folder at all
    os.makedirs(results.sample_dir(rdir, "KP_001"))
    assert results.foreign_content(rdir, "KP_001") == ""               # a folder of reads: fine
    write(os.path.join(results.trimming_dir(rdir, "KP_001"), "mine.txt"), "x")
    assert results.foreign_content(rdir, "KP_001") == "trimming"
    write(os.path.join(results.assembly_dir(rdir, "KP_001"), "mine.fasta"), "x")
    assert results.foreign_content(rdir, "KP_001") == "assembly"
    finish(rdir, "KP_002", "ours")
    assert results.foreign_content(rdir, "KP_002") == ""


# --------------------------------------------------------------------------- attempts

def test_begin_attempt_gives_a_fresh_folder_and_deletes_a_leftover(rdir):
    work = results.begin_attempt(rdir, "KP_001")
    assert work == results.in_progress_dir(rdir, "KP_001")
    assert os.path.isdir(work) and os.listdir(work) == ["attempt.json"]      # only the mark that it is ours
    assert os.path.isdir(results.sample_dir(rdir, "KP_001"))

    write(os.path.join(work, "assembly", "contigs.fasta"), ">half\nAC")
    write(os.path.join(results.sample_dir(rdir, "KP_001"), "my notes.txt"), "keep me")
    again = results.begin_attempt(rdir, "KP_001")
    assert again == work and os.listdir(work) == ["attempt.json"]
    assert read(os.path.join(results.sample_dir(rdir, "KP_001"), "my notes.txt")) == "keep me"


def test_an_in_progress_folder_that_is_not_ours_is_never_deleted(rdir):
    """Rule R7 beats rule R3: only our own leftovers are thrown away."""
    theirs = write(os.path.join(results.in_progress_dir(rdir, "KP_001"), "draft.docx"), "precious")
    with pytest.raises(FileExistsError):
        results.begin_attempt(rdir, "KP_001")
    assert results.abandon_attempt(rdir, "KP_001", keep_logs=True) == []
    assert results.recover_interrupted(rdir) == []
    assert read(theirs) == "precious"
    assert not os.path.exists(results.last_failed_dir(rdir, "KP_001"))

    # An empty folder of that name is nobody's loss, and a leftover in a folder that carries our
    # status file is ours even without its note (made by hand here).
    os.makedirs(results.in_progress_dir(rdir, "KP_002"))
    assert os.listdir(results.begin_attempt(rdir, "KP_002")) == ["attempt.json"]
    finish(rdir, "KP_003", "old")
    write(os.path.join(results.in_progress_dir(rdir, "KP_003"), "assembly", "half.fasta"), "x")
    assert os.listdir(results.begin_attempt(rdir, "KP_003")) == ["attempt.json"]


def test_begin_attempt_says_so_when_the_leftover_cannot_be_deleted(rdir, monkeypatch):
    work = results.begin_attempt(rdir, "KP_001")
    write(os.path.join(work, "assembly", "assembly.log"), "open in another program")
    monkeypatch.setattr(fsutil, "remove_tree", lambda path: False)
    with pytest.raises(OSError):
        results.begin_attempt(rdir, "KP_001")


def test_first_good_attempt_goes_into_place_and_counts_only_with_its_status_file(rdir):
    fill_attempt(rdir, "KP_001", "v1")
    committed = results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    assert committed == results.CommitResult(in_place=True, folder=results.sample_dir(rdir, "KP_001"))
    assert contigs_tag(results.contigs_path(rdir, "KP_001")) == "v1"
    assert os.path.isfile(results.trimming_report_html_path(rdir, "KP_001"))
    assert results.is_assembled(rdir, "KP_001") is False         # moved, not proven yet

    assert results.collect(rdir, "KP_001") == results.collected_fasta(rdir, "KP_001")
    assert contigs_tag(results.collected_fasta(rdir, "KP_001")) == "v1"
    assert results.is_assembled(rdir, "KP_001") is False

    results.write_status(rdir, outcome_for("KP_001"), **status_args())
    assert results.is_assembled(rdir, "KP_001") is True
    assert not os.path.exists(results.in_progress_dir(rdir, "KP_001"))
    assert sorted(os.listdir(results.sample_dir(rdir, "KP_001"))) == \
        ["assembly", "sample_status.json", "trimming"]
    r1, r2 = results.trimmed_read_names("KP_001")
    assert sorted(os.listdir(results.trimming_dir(rdir, "KP_001"))) == sorted(
        [r1, r2, "trimming.log", "trimming_report.html", "trimming_report.json"])
    assert results.count_assemblies(rdir) == 1


def test_trimmed_reads_are_deleted_on_request_but_reports_and_log_stay(rdir):
    finish(rdir, "KP_001", "v1", keep_trimmed_reads=False)
    assert sorted(os.listdir(results.trimming_dir(rdir, "KP_001"))) == \
        ["trimming.log", "trimming_report.html", "trimming_report.json"]
    assert results.is_assembled(rdir, "KP_001")


def test_an_attempt_without_contigs_is_not_committed_and_nothing_is_touched(rdir):
    finish(rdir, "KP_001", "old")
    before = snapshot(results.sample_dir(rdir, "KP_001"))
    fill_attempt(rdir, "KP_001", "new")
    os.remove(os.path.join(results.in_progress_assembly_dir(rdir, "KP_001"), "contigs.fasta"))
    with pytest.raises(results.AttemptIncomplete):
        results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=False)
    results.abandon_attempt(rdir, "KP_001", keep_logs=False)
    assert snapshot(results.sample_dir(rdir, "KP_001")) == before
    assert results.is_assembled(rdir, "KP_001")


def test_overwrite_replaces_the_old_result_only_when_the_new_one_is_ready(rdir):
    finish(rdir, "KP_001", "old")
    old = snapshot(results.sample_dir(rdir, "KP_001"))
    fill_attempt(rdir, "KP_001", "new")
    # All the while the engines work, the old result is whole and counts.
    assert {k: v for k, v in snapshot(results.sample_dir(rdir, "KP_001")).items()
            if not k.startswith("_in_progress/")} == old
    assert results.is_assembled(rdir, "KP_001")

    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    results.collect(rdir, "KP_001")
    results.write_status(rdir, outcome_for("KP_001", contigs=12), **status_args())
    assert contigs_tag(results.contigs_path(rdir, "KP_001")) == "new"
    assert contigs_tag(results.collected_fasta(rdir, "KP_001")) == "new"
    assert "new" in read(os.path.join(results.trimming_dir(rdir, "KP_001"), "trimming.log"))
    assert results.read_status(rdir, "KP_001").outcome.contigs == 12
    assert sorted(os.listdir(results.sample_dir(rdir, "KP_001"))) == \
        ["assembly", "sample_status.json", "trimming"]


def test_an_untrimmed_redo_does_not_keep_the_old_trimming_folder(rdir):
    finish(rdir, "KP_001", "old", trimmed=True)
    finish(rdir, "KP_001", "new", trimmed=False)
    assert not os.path.exists(results.trimming_dir(rdir, "KP_001"))
    record = results.read_status(rdir, "KP_001")
    assert record.assembled and record.trimmed is False


def test_a_failed_redo_keeps_the_old_result_and_the_logs_of_the_failure(rdir):
    finish(rdir, "KP_001", "old")
    write(os.path.join(results.sample_dir(rdir, "KP_001"), "my notes.txt"), "keep me")
    old = snapshot(results.sample_dir(rdir, "KP_001"))
    old_collected = read(results.collected_fasta(rdir, "KP_001"))

    fill_attempt(rdir, "KP_001", "bad")
    kept = results.abandon_attempt(rdir, "KP_001", keep_logs=True)

    failed = results.last_failed_dir(rdir, "KP_001")
    assert sorted(snapshot(failed)) == [
        "assembly/SUMMARY.txt", "assembly/assembly.log", "assembly/attempt1_default_k/assembly.log",
        "assembly/preflight.txt", "trimming/trimming.log"]
    assert sorted(kept) == sorted(os.path.join(failed, *k.split("/")) for k in snapshot(failed))
    assert read(os.path.join(failed, "assembly", "assembly.log")) == "assembly log bad\n"
    assert not os.path.exists(results.in_progress_dir(rdir, "KP_001"))
    now = snapshot(results.sample_dir(rdir, "KP_001"))
    assert {k: v for k, v in now.items() if not k.startswith("last_failed_attempt/")} == old
    assert read(results.collected_fasta(rdir, "KP_001")) == old_collected
    assert results.is_assembled(rdir, "KP_001")

    # last_failed_attempt is replaced each time, never added to.
    fill_attempt(rdir, "KP_001", "worse", trimmed=False)
    results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    assert "trimming/trimming.log" not in snapshot(failed)
    assert read(os.path.join(failed, "assembly", "assembly.log")) == "assembly log worse\n"


def test_a_stopped_attempt_is_deleted_without_touching_earlier_failure_logs(rdir):
    fill_attempt(rdir, "KP_001", "bad")
    results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    failed = snapshot(results.last_failed_dir(rdir, "KP_001"))
    assert failed

    fill_attempt(rdir, "KP_001", "stopped")
    assert results.abandon_attempt(rdir, "KP_001", keep_logs=False) == []
    assert snapshot(results.last_failed_dir(rdir, "KP_001")) == failed
    assert not os.path.exists(results.in_progress_dir(rdir, "KP_001"))
    assert results.abandon_attempt(rdir, "KP_001", keep_logs=True) == []      # nothing to abandon
    assert results.abandon_attempt(rdir, "never_started", keep_logs=True) == []
    assert not os.path.exists(results.sample_dir(rdir, "never_started"))


def test_a_failure_after_the_move_puts_the_old_result_back(rdir):
    """commit_attempt worked, then the copy to assemblies\\ failed: the sample failed after all."""
    finish(rdir, "KP_001", "old")
    old = snapshot(results.sample_dir(rdir, "KP_001"))
    fill_attempt(rdir, "KP_001", "new")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    results.collect(rdir, "KP_001")
    assert results.is_assembled(rdir, "KP_001") is False

    results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    now = snapshot(results.sample_dir(rdir, "KP_001"))
    assert {k: v for k, v in now.items() if not k.startswith("last_failed_attempt/")} == old
    assert read(os.path.join(results.last_failed_dir(rdir, "KP_001"), "assembly", "assembly.log")) == \
        "assembly log new\n"
    assert contigs_tag(results.collected_fasta(rdir, "KP_001")) == "old"
    assert results.is_assembled(rdir, "KP_001")


def test_a_failure_after_the_move_of_a_first_attempt_leaves_nothing_that_looks_finished(rdir):
    fill_attempt(rdir, "KP_001", "new")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    results.collect(rdir, "KP_001")
    results.write_status(rdir, outcome_for("KP_001", RunStatus.FAILED), **status_args())
    assert not os.path.exists(results.assembly_dir(rdir, "KP_001"))
    assert not os.path.exists(results.trimming_dir(rdir, "KP_001"))
    assert not os.path.exists(results.collected_fasta(rdir, "KP_001"))
    assert results.read_status(rdir, "KP_001").status is RunStatus.FAILED
    results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    assert os.path.isfile(os.path.join(results.last_failed_dir(rdir, "KP_001"), "assembly", "assembly.log"))
    assert sorted(os.listdir(results.sample_dir(rdir, "KP_001"))) == \
        ["last_failed_attempt", "sample_status.json"]


@pytest.mark.parametrize("locked", ["assembly", "trimming", "sample_status.json"])
def test_an_old_result_that_cannot_be_replaced_gives_new_result(rdir, monkeypatch, locked):
    """Audit row 5.6: a file of the old result is open in another program."""
    finish(rdir, "KP_001", "old")
    folder = results.sample_dir(rdir, "KP_001")
    old = snapshot(folder)
    fill_attempt(rdir, "KP_001", "new")
    blocked = os.path.join(folder, locked)
    real_rename, real_replace = os.rename, os.replace
    tries = []

    def rename(src, dst):
        if os.path.abspath(src) == blocked:
            tries.append(src)
            raise PermissionError(13, "The process cannot access the file", src)
        return real_rename(src, dst)

    def replace(src, dst):
        if os.path.abspath(src) == blocked:
            tries.append(src)
            raise PermissionError(13, "The process cannot access the file", src)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", replace)
    committed = results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    monkeypatch.setattr(os, "rename", real_rename)
    monkeypatch.setattr(os, "replace", real_replace)

    assert len(tries) == fsutil.RETRIES                        # it did try a few times first
    waiting = results.new_result_dir(rdir, "KP_001")
    assert committed.in_place is False and committed.reason == "in_use"
    assert committed.folder == waiting and committed.blocked_by == blocked
    # The old result is exactly as it was, and still counts.
    now = snapshot(folder)
    assert {k: v for k, v in now.items() if not k.startswith("new_result/")} == old
    assert results.is_assembled(rdir, "KP_001")
    assert contigs_tag(results.contigs_path(rdir, "KP_001")) == "old"
    # The new result is whole, in new_result, with a note for the user.
    assert contigs_tag(os.path.join(waiting, "assembly", "contigs.fasta")) == "new"
    assert "new" in read(os.path.join(waiting, "trimming", "trimming.log"))
    note = read(os.path.join(waiting, "README.txt"))
    assert note.startswith(branding.APP_NAME) and "KP_001" in note and "Overwrite" in note
    assert not os.path.exists(results.in_progress_dir(rdir, "KP_001"))

    # The caller's usual next two calls do no harm: the status of the result in place is left
    # alone, and the new one gets its own beside it.
    results.collect(rdir, "KP_001")
    in_place_status = read(results.status_path(rdir, "KP_001"))
    written = results.write_status(rdir, outcome_for("KP_001", contigs=12), **status_args())
    assert written == os.path.join(waiting, "sample_status.json")
    assert json.loads(read(written))["outcome"]["contigs"] == 12
    assert read(results.status_path(rdir, "KP_001")) == in_place_status
    assert contigs_tag(results.collected_fasta(rdir, "KP_001")) == "old"
    assert results.read_status(rdir, "KP_001").outcome.contigs == 71

    # The same thing again replaces new_result; a later redo that works removes it.
    fill_attempt(rdir, "KP_001", "newer")
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", replace)
    again = results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    monkeypatch.setattr(os, "rename", real_rename)
    monkeypatch.setattr(os, "replace", real_replace)
    assert again.in_place is False
    assert contigs_tag(os.path.join(waiting, "assembly", "contigs.fasta")) == "newer"
    assert not os.path.exists(os.path.join(waiting, "sample_status.json"))

    finish(rdir, "KP_001", "newest")
    assert not os.path.exists(waiting)
    assert contigs_tag(results.contigs_path(rdir, "KP_001")) == "newest"
    assert sorted(os.listdir(folder)) == ["assembly", "sample_status.json", "trimming"]


def test_if_the_new_result_cannot_be_moved_at_all_nothing_half_done_is_left(rdir, monkeypatch):
    finish(rdir, "KP_001", "old")
    old = snapshot(results.sample_dir(rdir, "KP_001"))
    fill_attempt(rdir, "KP_001", "new")
    stuck = results.in_progress_assembly_dir(rdir, "KP_001")
    real_rename = os.rename

    def rename(src, dst):
        if os.path.abspath(src) == stuck:
            raise PermissionError(13, "The process cannot access the file", src)
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(PermissionError):
        results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    monkeypatch.setattr(os, "rename", real_rename)
    assert snapshot(results.sample_dir(rdir, "KP_001")).items() >= old.items()     # the old result, whole
    assert results.is_assembled(rdir, "KP_001")
    assert not os.path.exists(results.new_result_dir(rdir, "KP_001"))              # no half new_result
    kept = results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    assert os.path.join(results.last_failed_dir(rdir, "KP_001"), "assembly", "assembly.log") in kept
    now = snapshot(results.sample_dir(rdir, "KP_001"))
    assert {k: v for k, v in now.items() if not k.startswith("last_failed_attempt/")} == old


def test_an_unfinished_new_result_of_ours_is_cleared_by_the_next_attempt(rdir):
    finish(rdir, "KP_001", "old")
    waiting = results.new_result_dir(rdir, "KP_001")
    write(os.path.join(waiting, "README.txt"), results._new_result_note("KP_001"))
    write(os.path.join(waiting, "trimming", "trimming.log"), "half")              # a crash got this far
    results.begin_attempt(rdir, "KP_001")
    assert not os.path.exists(waiting)
    results.abandon_attempt(rdir, "KP_001", keep_logs=False)
    assert results.is_assembled(rdir, "KP_001")


def test_a_result_is_proven_even_if_the_clean_up_after_it_has_to_wait(rdir, monkeypatch):
    finish(rdir, "KP_001", "old")
    fill_attempt(rdir, "KP_001", "new")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    results.collect(rdir, "KP_001")
    real_remove_tree = fsutil.remove_tree
    monkeypatch.setattr(fsutil, "remove_tree", lambda path: False)       # antivirus holds a file
    results.write_status(rdir, outcome_for("KP_001", contigs=12), **status_args())
    monkeypatch.setattr(fsutil, "remove_tree", real_remove_tree)
    assert results.is_assembled(rdir, "KP_001")
    assert os.path.isdir(results.in_progress_dir(rdir, "KP_001"))        # still there, harmless
    for clean_up in (lambda: results.recover_interrupted(rdir),
                     lambda: results.abandon_attempt(rdir, "KP_001", keep_logs=False)):
        clean_up()                                                       # must not undo a proven result
        assert results.read_status(rdir, "KP_001").outcome.contigs == 12
        assert contigs_tag(results.contigs_path(rdir, "KP_001")) == "new"
    assert not os.path.exists(results.in_progress_dir(rdir, "KP_001"))
    assert contigs_tag(results.collected_fasta(rdir, "KP_001")) == "new"


def test_folders_that_are_not_ours_are_never_replaced(rdir):
    """Rule R7: an 'assembly' folder without our status file is somebody else's."""
    write(os.path.join(results.assembly_dir(rdir, "KP_001"), "thesis_contigs.fasta"), "precious")
    fill_attempt(rdir, "KP_001", "new")
    committed = results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    assert committed.in_place is False and committed.reason == "not_ours"
    assert committed.blocked_by == results.assembly_dir(rdir, "KP_001")
    assert snapshot(results.assembly_dir(rdir, "KP_001")) == {"thesis_contigs.fasta": b"precious"}
    assert contigs_tag(os.path.join(committed.folder, "assembly", "contigs.fasta")) == "new"
    assert results.is_assembled(rdir, "KP_001") is False
    results.write_status(rdir, outcome_for("KP_001"), **status_args())
    assert not os.path.exists(results.status_path(rdir, "KP_001"))
    assert os.path.isfile(os.path.join(committed.folder, "sample_status.json"))


def test_a_new_result_folder_that_is_not_ours_is_not_deleted(rdir, monkeypatch):
    finish(rdir, "KP_001", "old")
    mine = write(os.path.join(results.new_result_dir(rdir, "KP_001"), "my own file.txt"), "precious")
    old = snapshot(results.sample_dir(rdir, "KP_001"))
    fill_attempt(rdir, "KP_001", "new")
    real_rename = os.rename

    def rename(src, dst):
        if os.path.abspath(src) == results.assembly_dir(rdir, "KP_001"):
            raise PermissionError(13, "in use", src)
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(FileExistsError):
        results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    monkeypatch.setattr(os, "rename", real_rename)
    assert read(mine) == "precious"
    results.abandon_attempt(rdir, "KP_001", keep_logs=False)
    assert snapshot(results.sample_dir(rdir, "KP_001")) == old
    assert results.is_assembled(rdir, "KP_001")

    # A good redo leaves that folder alone as well.
    finish(rdir, "KP_001", "newest")
    assert read(mine) == "precious"


def test_only_our_own_folders_are_ever_deleted(rdir):
    """Rule R7 over a whole life of a sample: fail, succeed, redo, stop."""
    folder = results.sample_dir(rdir, "KP_001")
    theirs = {
        "KP_001_R1.fastq.gz": "reads 1", "KP_001_R2.fastq.gz": "reads 2",
        "notes/lab book.txt": "plate 7", "old_assembly/contigs.fasta": ">x\nAC\n",
    }
    for rel, text in theirs.items():
        write(os.path.join(folder, *rel.split("/")), text)
    write(os.path.join(rdir, "thesis.docx"), "chapter 1")
    write(os.path.join(rdir, "assemblies", "someone_else.fasta"), ">y\nGT\n")

    fill_attempt(rdir, "KP_001", "bad")
    results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    results.write_status(rdir, outcome_for("KP_001", RunStatus.FAILED), **status_args())
    finish(rdir, "KP_001", "v1", keep_trimmed_reads=False)
    finish(rdir, "KP_001", "v2")
    fill_attempt(rdir, "KP_001", "stopped")
    results.abandon_attempt(rdir, "KP_001", keep_logs=False)
    results.recover_interrupted(rdir)

    for rel, text in theirs.items():
        assert read(os.path.join(folder, *rel.split("/"))) == text
    assert read(os.path.join(rdir, "thesis.docx")) == "chapter 1"
    assert read(os.path.join(rdir, "assemblies", "someone_else.fasta")) == ">y\nGT\n"
    assert contigs_tag(results.contigs_path(rdir, "KP_001")) == "v2"
    assert results.count_assemblies(rdir) == 2


# --------------------------------------------------------------------------- crash safety

class Interrupter:
    """Counts the low-level file operations and lets the process "die" at one of them.

    ``mode`` "before" dies instead of doing the operation, "after" dies straight after doing it,
    "partial" (deleting a folder) dies with half the folder gone.
    """
    TARGETS = [(os, "rename"), (os, "replace"), (os, "remove"), (os, "mkdir"),
               (shutil, "rmtree"), (shutil, "copyfileobj"), (shutil, "copyfile")]

    def __init__(self, at, mode):
        self.at, self.mode = at, mode
        self.calls = 0
        self.fired = False
        self.real = {(module, name): getattr(module, name) for module, name in self.TARGETS}

    def __enter__(self):
        for module, name in self.TARGETS:
            setattr(module, name, self._wrap(name, self.real[(module, name)]))
        return self

    def __exit__(self, *exc):
        for (module, name), function in self.real.items():
            setattr(module, name, function)
        return False

    def _wrap(self, name, function):
        def wrapped(*args, **kwargs):
            self.calls += 1
            if self.calls != self.at or self.fired:
                return function(*args, **kwargs)
            self.fired = True
            if self.mode == "before":
                raise Crash(name)
            if self.mode == "partial" and name == "rmtree":
                victim = args[0]
                for root, _dirs, files in os.walk(victim):
                    for file_name in sorted(files)[:1]:
                        self.real[(os, "remove")](os.path.join(root, file_name))
                raise Crash(name)
            if self.mode == "partial" and name == "copyfileobj":
                args[1].write(args[0].read(3))          # a few bytes got out before the lights went
                raise Crash(name)
            function(*args, **kwargs)
            raise Crash(name)
        return wrapped


def swap_states(rdir, name, old, new, new_status):
    """Which of the two results the sample folder shows, piece by piece."""
    folder = results.sample_dir(rdir, name)

    def which(sub):
        if not os.path.exists(os.path.join(folder, sub)):
            return "none"
        got = snapshot(os.path.join(folder, sub))
        if got and got == {k[len(sub) + 1:]: v for k, v in old.items() if k.startswith(sub + "/")}:
            return "old"
        if got and got == new[sub]:
            return "new"
        return "MIXED"

    status_file = results.status_path(rdir, name)
    if not os.path.exists(status_file):
        status = "none"
    else:
        text = read(status_file)
        status = "old" if text == old.get("sample_status.json", b"").decode() else \
            "new" if json.loads(text)["outcome"]["contigs"] == new_status else "MIXED"
    return which("assembly"), which("trimming"), status


def _swap_fixture(rdir, name, with_old, new_trimmed):
    if with_old:
        finish(rdir, name, "old")
    old = snapshot(results.sample_dir(rdir, name))
    old_collected = read(results.collected_fasta(rdir, name)) if with_old else None
    fill_attempt(rdir, name, "new", trimmed=new_trimmed)
    work = results.in_progress_dir(rdir, name)
    new = {"assembly": snapshot(os.path.join(work, "assembly")),
           "trimming": snapshot(os.path.join(work, "trimming"))}
    return old, old_collected, new


@pytest.mark.parametrize("with_old", [True, False], ids=["redo", "first"])
@pytest.mark.parametrize("new_trimmed", [True, False], ids=["trimmed", "untrimmed"])
@pytest.mark.parametrize("mode", ["before", "after", "partial"])
@pytest.mark.parametrize("recover", ["begin_attempt", "recover_interrupted", "abandon_attempt"])
def test_a_crash_at_any_step_of_the_swap_loses_nothing(tmp_path, with_old, new_trimmed, mode, recover):
    """Die at every file operation of commit_attempt -> collect -> write_status, one by one.

    Before any repair: is_assembled never says yes to a mixture. After the repair the next start
    makes: either the new result is fully in place and proven, or the earlier state is back, byte
    for byte.
    """
    name = "KP_001"
    crash_points = 0
    saw_new = saw_old = False
    for at in range(1, 200):
        rdir = str(tmp_path / ("try %d" % at))
        old, old_collected, new = _swap_fixture(rdir, name, with_old, new_trimmed)
        with Interrupter(at, mode) as interrupter:
            try:
                results.commit_attempt(rdir, name, keep_trimmed_reads=True)
                results.collect(rdir, name)
                results.write_status(rdir, outcome_for(name, contigs=12, trimmed=new_trimmed),
                                     **status_args(trimmed=new_trimmed))
            except Crash:
                pass
        if not interrupter.fired:
            break                                   # the whole thing ran: every step was tried
        crash_points += 1

        # --- as the crash left it, before anything repairs it
        assembly, trimming, status = swap_states(rdir, name, old, new, 12)
        if results.is_assembled(rdir, name):
            new_trim_state = "new" if new_trimmed else "none"
            assert (assembly, trimming, status) in (("old", "old", "old"),
                                                    ("new", new_trim_state, "new")), (at, mode)
        if with_old and status != "new":
            # The earlier assembly still exists somewhere, whole.
            parked = os.path.join(results.in_progress_dir(rdir, name), "replaced", "assembly")
            assert assembly == "old" or snapshot(parked) == \
                {k[9:]: v for k, v in old.items() if k.startswith("assembly/")}, (at, mode)

        # --- what the next start does
        if recover == "begin_attempt":
            results.begin_attempt(rdir, name)
            results.abandon_attempt(rdir, name, keep_logs=False)
        elif recover == "recover_interrupted":
            results.recover_interrupted(rdir)
            results.begin_attempt(rdir, name)
            results.abandon_attempt(rdir, name, keep_logs=False)
        else:
            results.abandon_attempt(rdir, name, keep_logs=False)
        assert not os.path.exists(results.in_progress_dir(rdir, name)), (at, mode)

        assembly, trimming, status = swap_states(rdir, name, old, new, 12)
        folder_now = {k: v for k, v in snapshot(results.sample_dir(rdir, name)).items()}
        if status == "new":
            saw_new = True
            assert assembly == "new" and trimming == ("new" if new_trimmed else "none"), (at, mode)
            assert results.is_assembled(rdir, name), (at, mode)
            assert contigs_tag(results.collected_fasta(rdir, name)) == "new", (at, mode)
        else:
            saw_old = True
            assert folder_now == old, (at, mode)                   # byte for byte, nothing extra
            assert results.is_assembled(rdir, name) is with_old, (at, mode)
            if with_old:
                assert read(results.collected_fasta(rdir, name)) == old_collected, (at, mode)
            else:
                assert not os.path.exists(results.collected_fasta(rdir, name)), (at, mode)
        shutil.rmtree(rdir)
    else:
        pytest.fail("the swap never ran to its end")
    assert crash_points >= (10 if with_old else 6)       # the test really did interrupt it
    assert saw_old and (saw_new or mode == "before")


def test_a_crash_while_undoing_a_swap_can_be_undone_again(tmp_path):
    """The repair itself is cut short, at every one of its steps, and repaired again."""
    name = "KP_001"
    for mode in ("before", "after"):
        for at in range(1, 100):
            rdir = str(tmp_path / ("%s %d" % (mode, at)))
            old, old_collected, _new = _swap_fixture(rdir, name, True, True)
            results.commit_attempt(rdir, name, keep_trimmed_reads=True)
            results.collect(rdir, name)                 # ...and then the power goes
            with Interrupter(at, mode) as interrupter:
                try:
                    results.recover_interrupted(rdir)
                except Crash:
                    pass
            if results.is_assembled(rdir, name):
                assert swap_states(rdir, name, old, _new, 12) == ("old", "old", "old"), (mode, at)
            results.recover_interrupted(rdir)
            results.begin_attempt(rdir, name)
            results.abandon_attempt(rdir, name, keep_logs=False)
            assert snapshot(results.sample_dir(rdir, name)) == old, (mode, at)
            assert read(results.collected_fasta(rdir, name)) == old_collected, (mode, at)
            assert results.is_assembled(rdir, name), (mode, at)
            shutil.rmtree(rdir)
            if not interrupter.fired:
                break
        else:
            pytest.fail("the repair never ran to its end")
        assert at > 5


def test_recover_interrupted_reports_what_it_put_right_and_leaves_the_rest(rdir):
    finish(rdir, "KP_001", "old")
    finish(rdir, "KP_002", "fine")
    fill_attempt(rdir, "KP_001", "new")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)      # ...crash
    fill_attempt(rdir, "KP_003", "plain leftover")                          # an ordinary leftover
    before_2 = snapshot(results.sample_dir(rdir, "KP_002"))
    assert results.is_assembled(rdir, "KP_001") is False

    assert results.recover_interrupted(rdir) == ["KP_001"]
    assert results.is_assembled(rdir, "KP_001")
    assert contigs_tag(results.contigs_path(rdir, "KP_001")) == "old"
    assert snapshot(results.sample_dir(rdir, "KP_002")) == before_2
    assert os.path.isdir(results.in_progress_dir(rdir, "KP_003"))       # begin_attempt's job
    assert results.recover_interrupted(rdir) == []
    assert results.recover_interrupted(os.path.join(rdir, "nowhere")) == []


def test_reading_never_changes_a_folder_in_the_middle_of_a_swap(rdir, tmp_path):
    """Another window may look at a results folder that a run is writing to."""
    sample = make_reads(str(tmp_path / "reads"), "KP_001")
    finish(rdir, "KP_001", "old", sample=sample)
    fill_attempt(rdir, "KP_001", "new")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    before = snapshot(rdir)
    assert results.is_assembled(rdir, "KP_001") is False
    assert results.read_status(rdir, "KP_001") is None
    assert results.all_records(rdir) == []
    assert results.existing_results(rdir, [sample], FINGERPRINT) == {}
    assert results.name_clashes(rdir, [sample]) == []
    assert results.foreign_content(rdir, "KP_001") == ""
    assert results.is_results_folder(rdir) is True           # the attempt in progress gives it away
    assert results.made_by_newer_version(rdir) is False
    assert snapshot(rdir) == before


def test_parked_folders_are_put_back_even_if_the_note_of_the_swap_is_lost(rdir):
    finish(rdir, "KP_001", "old")
    old = snapshot(results.sample_dir(rdir, "KP_001"))
    fill_attempt(rdir, "KP_001", "new")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    os.remove(os.path.join(results.in_progress_dir(rdir, "KP_001"), "commit.json"))
    results.begin_attempt(rdir, "KP_001")
    results.abandon_attempt(rdir, "KP_001", keep_logs=False)
    assert snapshot(results.sample_dir(rdir, "KP_001")) == old
    assert results.is_assembled(rdir, "KP_001")


# --------------------------------------------------------------------------- collect

def test_collect_copies_the_contigs_whole_or_not_at_all(rdir, monkeypatch):
    finish(rdir, "KP_001", "old")
    target = results.collected_fasta(rdir, "KP_001")
    assert read(target) == read(results.contigs_path(rdir, "KP_001"))
    write(results.contigs_path(rdir, "KP_001"), ">contig_1 edited\nAC\n")

    real_replace = os.replace

    def dying_replace(src, dst):
        raise Crash()

    monkeypatch.setattr(os, "replace", dying_replace)
    with pytest.raises(Crash):
        results.collect(rdir, "KP_001")
    monkeypatch.setattr(os, "replace", real_replace)
    assert contigs_tag(target) == "old"                         # the old copy, whole
    assert len(os.listdir(results.assemblies_dir(rdir))) == 2      # plus the temporary file
    assert results.count_assemblies(rdir) == 1                  # which is not counted as an assembly

    results.collect(rdir, "KP_001")
    assert contigs_tag(target) == "edited"
    assert os.listdir(results.assemblies_dir(rdir)) == ["KP_001.fasta"]

    with pytest.raises(OSError):
        results.collect(rdir, "no such sample")
    assert os.listdir(results.assemblies_dir(rdir)) == ["KP_001.fasta"]


# --------------------------------------------------------------------------- odd names

@pytest.mark.parametrize("folder, name", [
    ("Results of José & Zoë (2026)", "KP 001 [rerun]"),
    ("Résultats séquençage", "Ünïcode_ñame"),
    ("結果 フォルダ", "サンプル_01"),
    ("Результаты", "образец №7"),
    ("αποτελέσματα", "δείγμα-3"),
    ("with 'quotes' and #hash %20", "it's_100%"),
])
def test_names_and_paths_with_spaces_and_non_latin_letters(tmp_path, folder, name):
    rdir = str(tmp_path / folder)
    sample = make_reads(str(tmp_path / ("reads " + folder)), name)
    finish(rdir, name, "old", sample=sample)
    finish(rdir, name, "new", sample=sample, keep_trimmed_reads=False)
    assert results.is_assembled(rdir, name)
    assert contigs_tag(results.collected_fasta(rdir, name)) == "new"
    record = results.read_status(rdir, name)
    assert record.name == name and [n for n, _ in record.reads] == [
        name + "_R1.fastq.gz", name + "_R2.fastq.gz"]
    assert [r.name for r in results.all_records(rdir)] == [name]
    assert results.existing_results(rdir, [sample], FINGERPRINT)[name].same_reads
    assert results.name_clashes(rdir, [sample]) == []

    fill_attempt(rdir, name, "bad")
    kept = results.abandon_attempt(rdir, name, keep_logs=True)
    assert kept and all(os.path.isfile(path) for path in kept)
    assert contigs_tag(results.contigs_path(rdir, name)) == "new"

    config = RunConfig(results_dir=rdir, samples=[sample])
    results.save_last_run(rdir, config, {name: (RunStatus.DONE, "tout va bien — 完了")},
                          started_at="2026-11-16T18:00:00")
    loaded = results.load_last_run(rdir)
    assert loaded.config.samples[0].name == name and loaded.notes[name] == "tout va bien — 完了"
    assert loaded.config.samples[0].lanes == sample.lanes
    with open(results.last_run_path(rdir), "rb") as handle:
        raw = handle.read()
    assert name.encode("utf-8") in raw and b"\r" not in raw
    assert results.is_results_folder(rdir)


# --------------------------------------------------------------------------- last_run.json

def batch(rdir, names):
    samples = [Sample(name=n, lanes=[("/reads/%s_R1.fastq.gz" % n, "/reads/%s_R2.fastq.gz" % n)],
                      trim=(n != "KP_2"), size_bytes=5) for n in names]
    return RunConfig(results_dir=rdir, samples=samples, trim=TrimSettings(min_length=70),
                     assembly=AssemblySettings(mode="careful", threads=4), existing=Existing.OVERWRITE)


def test_last_run_round_trip(rdir):
    assert results.load_last_run(rdir) is None
    config = batch(rdir, ["KP_1", "KP_2", "KP_3"])
    states = {"KP_1": (RunStatus.DONE, ""), "KP_2": (RunStatus.ASSEMBLING, "Step 3 of 7"),
              "KP_3": RunStatus.WAITING}
    results.save_last_run(rdir, config, states, started_at="2026-11-16T18:00:00")

    loaded = results.load_last_run(rdir)
    assert isinstance(loaded, results.LastRun)
    assert loaded.config == config                                   # samples, ticks and options
    assert loaded.config.samples[1].trim is False and loaded.config.existing is Existing.OVERWRITE
    assert loaded.states == {"KP_1": RunStatus.DONE, "KP_2": RunStatus.ASSEMBLING, "KP_3": RunStatus.WAITING}
    assert loaded.notes == {"KP_1": "", "KP_2": "Step 3 of 7", "KP_3": ""}
    assert loaded.started_at == "2026-11-16T18:00:00" and len(loaded.saved_at) == 19
    assert loaded.ended_at is None and loaded.end_reason is None and loaded.interrupted is None
    assert loaded.program_version == versions.APP_VERSION
    assert results.was_interrupted(loaded) is True

    results.save_last_run(rdir, config, {n: {"status": "done", "note": "ok"} for n in states},
                          started_at="2026-11-16T18:00:00", ended_at="2026-11-17T03:42:10",
                          end_reason=EndReason.COMPLETED)
    ended = results.load_last_run(rdir)
    assert ended.ended_at == "2026-11-17T03:42:10" and ended.end_reason is EndReason.COMPLETED
    assert ended.notes["KP_3"] == "ok"
    assert results.was_interrupted(ended) is False
    assert results.pending_samples(ended) == [] and results.failed_samples(ended) == []
    assert results.was_interrupted(None) is False


def test_last_run_remembers_why_windows_interrupted_it(rdir):
    config = batch(rdir, ["KP_1"])
    results.save_last_run(rdir, config, {"KP_1": (RunStatus.TRIMMING, "")},
                          started_at="2026-11-16T18:00:00", interrupted="shutdown")
    loaded = results.load_last_run(rdir)
    assert loaded.interrupted == "shutdown" and results.was_interrupted(loaded)
    results.save_last_run(rdir, config, {"KP_1": (RunStatus.STOPPED, "")}, started_at="2026-11-16T18:00:00",
                          ended_at="2026-11-16T19:00:00", end_reason="stopped")
    assert results.load_last_run(rdir).end_reason is EndReason.STOPPED


def test_what_continue_and_try_again_should_run(rdir):
    names = ["done", "notes", "failed_1", "skipped", "stopped", "not_started", "waiting",
             "joining", "trimming", "checking", "assembling", "summarising", "failed_2", "unlisted"]
    config = batch(rdir, names)
    states = {
        "done": RunStatus.DONE, "notes": RunStatus.DONE_NOTES, "failed_1": RunStatus.FAILED,
        "skipped": RunStatus.SKIPPED, "stopped": RunStatus.STOPPED,
        "not_started": RunStatus.NOT_STARTED, "waiting": RunStatus.WAITING,
        "joining": RunStatus.JOINING_LANES, "trimming": RunStatus.TRIMMING,
        "checking": RunStatus.CHECKING_READS, "assembling": RunStatus.ASSEMBLING,
        "summarising": RunStatus.SUMMARISING, "failed_2": (RunStatus.FAILED, "no contigs"),
    }
    results.save_last_run(rdir, config, states, started_at="2026-11-16T18:00:00")
    loaded = results.load_last_run(rdir)
    assert [s.name for s in results.pending_samples(loaded)] == [
        "stopped", "not_started", "waiting", "joining", "trimming", "checking", "assembling",
        "summarising", "unlisted"]
    assert [s.name for s in results.failed_samples(loaded)] == ["failed_1", "failed_2"]
    assert all(isinstance(s, Sample) and s.lanes for s in results.pending_samples(loaded))
    assert loaded.status_of("unlisted") is RunStatus.WAITING


@pytest.mark.parametrize("content", ["", "{", "[]", '{"format": 1}', '{"format": 1, "config": 5}',
                                     '{"format": 1, "config": {"samples": []}}', '{"config": {}}'])
def test_a_damaged_last_run_file_gives_none(rdir, content):
    write(results.last_run_path(rdir), content)
    assert results.load_last_run(rdir) is None


def test_odd_states_in_last_run_are_treated_as_not_done_yet(rdir):
    config = batch(rdir, ["KP_1", "KP_2"])
    results.save_last_run(rdir, config, {"KP_1": "from a later version", "KP_2": "done"},
                          started_at="2026-11-16T18:00:00")
    loaded = results.load_last_run(rdir)
    assert [s.name for s in results.pending_samples(loaded)] == ["KP_1"]


def test_last_run_of_a_newer_program_is_refused(rdir):
    results.save_last_run(rdir, batch(rdir, ["KP_1"]), {}, started_at="2026-11-16T18:00:00")
    stored = json.loads(read(results.last_run_path(rdir)))
    stored["format"] = versions.RESULTS_FORMAT + 3
    write(results.last_run_path(rdir), json.dumps(stored))
    with pytest.raises(results.NewerFormat):
        results.load_last_run(rdir)
    assert results.made_by_newer_version(rdir)
    assert results.is_results_folder(rdir)


def test_last_run_is_rewritten_whole_or_not_at_all(rdir, monkeypatch):
    config = batch(rdir, ["KP_1", "KP_2"])
    results.save_last_run(rdir, config, {"KP_1": RunStatus.ASSEMBLING}, started_at="2026-11-16T18:00:00")
    before = read(results.last_run_path(rdir))
    real_replace = os.replace

    def dying_replace(src, dst):
        raise Crash()

    monkeypatch.setattr(os, "replace", dying_replace)
    with pytest.raises(Crash):
        results.save_last_run(rdir, config, {"KP_1": RunStatus.DONE}, started_at="2026-11-16T18:00:00")
    monkeypatch.setattr(os, "replace", real_replace)
    assert read(results.last_run_path(rdir)) == before
    assert results.load_last_run(rdir).states["KP_1"] is RunStatus.ASSEMBLING


# --------------------------------------------------------------------------- the folder as a whole

def test_is_results_folder(tmp_path, rdir):
    assert results.is_results_folder(rdir) is False                       # not there
    assert results.is_results_folder("") is False
    os.makedirs(rdir)
    assert results.is_results_folder(rdir) is False                       # empty
    write(os.path.join(rdir, "thesis.docx"), "x")
    write(os.path.join(rdir, "KP_001", "KP_001_R1.fastq.gz"), "x")
    write(os.path.join(rdir, "last_run.json"), '{"something": "else"}')
    write(os.path.join(rdir, "run_summary.html"), "<html>somebody else's</html>")
    assert results.is_results_folder(rdir) is False                       # other people's files

    for marker in ("lock", "last_run", "sample", "summary", "attempt"):
        folder = str(tmp_path / marker)
        os.makedirs(folder)
        if marker == "lock":
            write(results.lock_path(folder), "PC LAB-07")
        elif marker == "attempt":
            results.begin_attempt(folder, "KP_001")          # the very first sample is being made
        elif marker == "last_run":
            results.save_last_run(folder, batch(folder, ["KP_1"]), {}, started_at="2026-11-16T18:00:00")
        elif marker == "sample":
            finish(folder, "KP_001", "v1")
            os.remove(results.collected_fasta(folder, "KP_001"))
        else:
            write(results.summary_html_path(folder),
                  '<!DOCTYPE html><html><head><meta name="generator" content="%s 0.1.0"></head></html>'
                  % branding.APP_NAME)
        assert results.is_results_folder(folder) is True, marker
        assert results.foreign_and_not_empty(folder) is False, marker


def test_foreign_and_not_empty(tmp_path):
    """Audit row 4.2: only then does the caller add a TesserACT_results folder of its own."""
    missing = str(tmp_path / "not yet there")
    assert results.foreign_and_not_empty(missing) is False
    assert results.foreign_and_not_empty("") is False
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    assert results.foreign_and_not_empty(empty) is False
    write(os.path.join(empty, "desktop.ini"), "[.ShellClassInfo]")
    assert results.foreign_and_not_empty(empty) is False                  # Explorer's own file
    documents = str(tmp_path / "Documents")
    write(os.path.join(documents, "thesis.docx"), "x")
    assert results.foreign_and_not_empty(documents) is True
    reads = str(tmp_path / "run")
    write(os.path.join(reads, "KP_001", "KP_001_R1.fastq.gz"), "x")
    assert results.foreign_and_not_empty(reads) is True
    a_file = write(str(tmp_path / "a file.txt"), "x")
    assert results.foreign_and_not_empty(a_file) is False
    ours = str(tmp_path / "ours")
    finish(ours, "KP_001", "v1")
    assert results.foreign_and_not_empty(ours) is False
    assert branding.RESULTS_DIR_NAME == "TesserACT_results"


# --------------------------------------------------------------------------- added in review

def test_names_with_square_brackets_in_the_folder_and_the_sample(tmp_path):
    """Review focus 2: '[' and ']' are legal on Windows and special to pattern matching."""
    rdir = str(tmp_path / "Run [2026] (résultats) #1")
    name = "KP[1]_ré"
    sample = make_reads(str(tmp_path / "reads [raw]"), name)
    finish(rdir, name, "old", sample=sample)
    fill_attempt(rdir, name, "bad")
    kept = results.abandon_attempt(rdir, name, keep_logs=True)
    assert kept and all(os.path.isfile(path) for path in kept)
    finish(rdir, name, "new", sample=sample, keep_trimmed_reads=False)
    assert results.is_assembled(rdir, name) and results.name_clashes(rdir, [sample]) == []
    assert contigs_tag(results.collected_fasta(rdir, name)) == "new"
    assert sorted(os.listdir(results.trimming_dir(rdir, name))) == \
        ["trimming.log", "trimming_report.html", "trimming_report.json"]
    assert [r.name for r in results.all_records(rdir)] == [name]
    assert results.recover_interrupted(rdir) == [] and results.is_results_folder(rdir)


def test_two_threads_saving_last_run_at_once_never_damage_it(rdir, monkeypatch):
    """The window notes a Windows shutdown while the batch thread records a change of state."""
    import threading

    # Real waiting between retries here: on Windows a file that another thread is reading at
    # that instant cannot be replaced, and the retry needs its pause to be worth anything.
    monkeypatch.undo()
    os.makedirs(rdir)
    config = batch(rdir, ["KP_%d" % i for i in range(30)])
    problems = []

    def save_many(worker):
        for turn in range(40):
            try:
                results.save_last_run(rdir, config, {"KP_1": (RunStatus.ASSEMBLING, "x" * (worker * 700 + turn))},
                                      started_at="2026-11-16T18:00:00",
                                      interrupted="shutdown" if worker == 0 else None)
                if results.load_last_run(rdir) is None:
                    problems.append("unreadable")
            except BaseException as exc:            # noqa: BLE001 - whatever it is, it is a failure
                problems.append(repr(exc))

    threads = [threading.Thread(target=save_many, args=(worker,)) for worker in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert problems == []
    assert results.load_last_run(rdir).states["KP_1"] is RunStatus.ASSEMBLING
    assert os.listdir(rdir) == ["last_run.json"]                # no temporary file left


def test_values_no_number_can_hold_do_not_break_reading(rdir):
    """A damaged status file gives None or a usable record, never an exception."""
    finish(rdir, "KP_001", "v1")
    path = results.status_path(rdir, "KP_001")
    stored = json.loads(read(path))
    for change in ({"reads": 5}, {"reads": "abc"}, {"reads": {"name": "x"}}, {"reads": None}):
        write(path, json.dumps(dict(stored, **change)))
        record = results.read_status(rdir, "KP_001")
        assert record.assembled and record.reads == [], change
        # Reads that are not known are never taken for the same reads (decision D3: left out).
        sample = Sample("KP_001", [("x_R1.fastq.gz", "x_R2.fastq.gz")])
        assert results.name_clashes(rdir, [sample]) == ["KP_001"], change
    for change in ({"last_attempt": "failed"}, {"last_attempt": [1]}, {"trimmed": "perhaps"},
                   {"settings_text": 7, "program_version": None, "finished_at": ["x"]}):
        write(path, json.dumps(dict(stored, **change)))
        record = results.read_status(rdir, "KP_001")
        assert record.assembled and record.last_attempt is None and len(record.reads) == 2, change
    for seconds in (float("inf"), float("-inf"), float("nan"), -5, 1e308 * 10, "soon", None, [1]):
        outcome = dict(stored["outcome"], seconds=seconds, contigs=float("inf"), depth=float("nan"))
        write(path, json.dumps(dict(stored, outcome=outcome)))
        record = results.read_status(rdir, "KP_001")
        assert record.outcome.seconds == 0.0, seconds
        assert record.outcome.contigs is None and record.outcome.depth is None
    assert results.is_assembled(rdir, "KP_001") and len(results.all_records(rdir)) == 1


def test_last_run_follows_a_results_folder_that_was_moved(rdir, tmp_path):
    """Audit row 11.6: Continue on another PC, or after the folder was moved to a stick."""
    os.makedirs(rdir)
    results.save_last_run(rdir, batch(rdir, ["KP_1", "KP_2"]), {"KP_1": RunStatus.DONE, "KP_2": RunStatus.STOPPED},
                          started_at="2026-11-16T18:00:00")
    moved = str(tmp_path / "E - stick" / "Results")
    os.makedirs(os.path.dirname(moved))
    shutil.move(rdir, moved)
    loaded = results.load_last_run(moved)
    assert loaded.config.results_dir == moved            # never the place it was in before
    assert [s.name for s in results.pending_samples(loaded)] == ["KP_2"]


def test_collect_never_copies_an_assembly_this_program_did_not_make(rdir):
    """A folder 'assembly' with a contigs.fasta of its own (another assembler writes that name)."""
    foreign = write(results.contigs_path(rdir, "KP_001"), ">NODE_1 FOREIGN\nACGT\n")
    with pytest.raises(results.AttemptIncomplete):
        results.collect(rdir, "KP_001")
    fill_attempt(rdir, "KP_001", "new")
    committed = results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    assert committed.in_place is False and committed.reason == "not_ours"
    with pytest.raises(results.AttemptIncomplete):
        results.collect(rdir, "KP_001")                      # the caller's usual next call
    assert not os.path.exists(results.collected_fasta(rdir, "KP_001"))
    assert not os.path.exists(results.assemblies_dir(rdir)) or os.listdir(results.assemblies_dir(rdir)) == []
    assert read(foreign) == ">NODE_1 FOREIGN\nACGT\n"

    # What is ours is collected as before: during a swap, and again once it is proven.
    fill_attempt(rdir, "KP_002", "ours")
    results.commit_attempt(rdir, "KP_002", keep_trimmed_reads=True)
    assert contigs_tag(results.collect(rdir, "KP_002")) == "ours"
    results.write_status(rdir, outcome_for("KP_002"), **status_args())
    assert contigs_tag(results.collect(rdir, "KP_002")) == "ours"
    # A sample whose only status is a failure has no assembly of ours in place either.
    results.write_status(rdir, outcome_for("KP_003", RunStatus.FAILED), **status_args())
    write(results.contigs_path(rdir, "KP_003"), ">NODE_1 FOREIGN\nACGT\n")
    with pytest.raises(results.AttemptIncomplete):
        results.collect(rdir, "KP_003")


def test_recording_done_twice_never_vouches_for_the_folder_in_place(rdir, monkeypatch):
    """After a commit that left the result in new_result, every 'done' goes there."""
    # 1. The sample folder holds somebody else's assembly.
    foreign = write(results.contigs_path(rdir, "KP_001"), ">NODE_1 FOREIGN\nACGT\n")
    fill_attempt(rdir, "KP_001", "new")
    committed = results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    beside = os.path.join(committed.folder, "sample_status.json")
    for _ in range(3):
        assert results.write_status(rdir, outcome_for("KP_001"), **status_args()) == beside
    assert not os.path.exists(results.status_path(rdir, "KP_001"))
    assert results.is_assembled(rdir, "KP_001") is False         # so an Overwrite can never delete it
    assert read(foreign) == ">NODE_1 FOREIGN\nACGT\n"

    # 2. The old result of ours could not be replaced: its own status file keeps describing it.
    finish(rdir, "KP_002", "old")
    in_place = read(results.status_path(rdir, "KP_002"))
    fill_attempt(rdir, "KP_002", "new")
    real_rename = os.rename
    blocked = results.assembly_dir(rdir, "KP_002")

    def rename(src, dst):
        if os.path.abspath(src) == blocked:
            raise PermissionError(13, "The process cannot access the file", src)
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    committed = results.commit_attempt(rdir, "KP_002", keep_trimmed_reads=True)
    monkeypatch.setattr(os, "rename", real_rename)
    assert committed.in_place is False and committed.reason == "in_use"
    beside = os.path.join(committed.folder, "sample_status.json")
    for contigs in (12, 13):
        assert results.write_status(rdir, outcome_for("KP_002", contigs=contigs), **status_args()) == beside
    assert json.loads(read(beside))["outcome"]["contigs"] == 13
    assert read(results.status_path(rdir, "KP_002")) == in_place
    assert results.read_status(rdir, "KP_002").outcome.contigs == 71
    assert contigs_tag(results.contigs_path(rdir, "KP_002")) == "old"


def test_a_last_failed_attempt_folder_keeps_what_is_not_ours(rdir):
    """Rule R7: of that folder only the logs this program put there are replaced."""
    failed = results.last_failed_dir(rdir, "KP_001")
    theirs = write(os.path.join(failed, "thesis.docx"), "precious")
    deeper = write(os.path.join(failed, "assembly", "my notes.txt"), "plate 7")
    stale = write(os.path.join(failed, "trimming", "trimming.log"), "from the attempt before")
    fill_attempt(rdir, "KP_001", "bad", trimmed=False)
    kept = results.abandon_attempt(rdir, "KP_001", keep_logs=True)
    assert read(theirs) == "precious" and read(deeper) == "plate 7"
    assert not os.path.exists(stale)                             # ours, and of an older attempt
    assert not os.path.exists(os.path.dirname(stale))            # no empty folder left behind
    assert read(os.path.join(failed, "assembly", "assembly.log")) == "assembly log bad\n"
    assert os.path.join(failed, "assembly", "assembly.log") in kept
    assert not os.path.exists(results.in_progress_dir(rdir, "KP_001"))


def test_an_empty_new_result_folder_is_no_obstacle(rdir, monkeypatch):
    """A crash between making new_result and writing its note leaves it empty, without our mark."""
    finish(rdir, "KP_001", "old")
    waiting = results.new_result_dir(rdir, "KP_001")
    os.makedirs(waiting)
    write(os.path.join(waiting, "README.txt.tmp4242"), "TesserACT-W")       # the note, cut short
    fill_attempt(rdir, "KP_001", "new")
    assert not os.path.exists(waiting)                           # begin_attempt cleared it
    os.makedirs(waiting)
    real_rename = os.rename
    blocked = results.assembly_dir(rdir, "KP_001")

    def rename(src, dst):
        if os.path.abspath(src) == blocked:
            raise PermissionError(13, "The process cannot access the file", src)
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    committed = results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    monkeypatch.setattr(os, "rename", real_rename)
    assert committed.in_place is False and committed.reason == "in_use"
    assert contigs_tag(os.path.join(waiting, "assembly", "contigs.fasta")) == "new"
    assert results.is_assembled(rdir, "KP_001") and contigs_tag(results.contigs_path(rdir, "KP_001")) == "old"


def _repair(rdir, name):
    """What the next start does with whatever a crash left."""
    results.recover_interrupted(rdir)
    results.begin_attempt(rdir, name)
    results.abandon_attempt(rdir, name, keep_logs=False)


@pytest.mark.parametrize("mode", ["before", "after", "partial"])
def test_a_crash_at_any_step_of_leaving_a_new_result_loses_nothing(tmp_path, mode):
    """Audit row 5.6 with the power failing at every file operation of it, one by one.

    The old result stays whole and proven throughout; new_result is whole or it is cleared by the
    next start; and the same thing can happen again afterwards without an error.
    """
    name = "KP_001"
    real_rename = os.rename
    crash_points = 0

    def run_with_a_locked_folder(rdir):
        blocked = results.trimming_dir(rdir, name)
        inner = os.rename

        def rename(src, dst):
            if os.path.abspath(src) == blocked:
                raise PermissionError(13, "The process cannot access the file", src)
            return inner(src, dst)

        os.rename = rename
        try:
            committed = results.commit_attempt(rdir, name, keep_trimmed_reads=True)
            results.collect(rdir, name)
            results.write_status(rdir, outcome_for(name, contigs=12), **status_args())
            return committed
        finally:
            os.rename = inner

    for at in range(1, 300):
        rdir = str(tmp_path / ("try %d" % at))
        old, old_collected, new = _swap_fixture(rdir, name, True, True)
        with Interrupter(at, mode) as interrupter:
            try:
                run_with_a_locked_folder(rdir)
            except Crash:
                pass
        assert os.rename is real_rename
        # --- as the crash left it: never "assembled" on a mixture
        if results.is_assembled(rdir, name):
            assert swap_states(rdir, name, old, new, 12) == ("old", "old", "old"), (at, mode)
        _repair(rdir, name)
        waiting = results.new_result_dir(rdir, name)
        now = {k: v for k, v in snapshot(results.sample_dir(rdir, name)).items()
               if not k.startswith("new_result/")}
        assert now == old, (at, mode)
        assert results.is_assembled(rdir, name), (at, mode)
        assert read(results.collected_fasta(rdir, name)) == old_collected, (at, mode)
        if os.path.exists(waiting):                                  # whole, or not there at all
            assert snapshot(os.path.join(waiting, "assembly")) == new["assembly"], (at, mode)
            assert snapshot(os.path.join(waiting, "trimming")) == new["trimming"], (at, mode)
            assert read(os.path.join(waiting, "README.txt")).startswith(branding.APP_NAME), (at, mode)
        # --- and it can all happen again
        fill_attempt(rdir, name, "newer")
        again = run_with_a_locked_folder(rdir)
        assert again.in_place is False and again.reason == "in_use", (at, mode)
        assert contigs_tag(os.path.join(waiting, "assembly", "contigs.fasta")) == "newer", (at, mode)
        assert contigs_tag(results.contigs_path(rdir, name)) == "old", (at, mode)
        shutil.rmtree(rdir)
        if not interrupter.fired:
            break
        crash_points += 1
    else:
        pytest.fail("it never ran to its end")
    assert crash_points >= 15


@pytest.mark.parametrize("step", ["begin_attempt", "abandon_attempt", "write_status_failed"])
@pytest.mark.parametrize("with_old", [True, False], ids=["redo", "first"])
@pytest.mark.parametrize("mode", ["before", "after", "partial"])
def test_a_crash_while_giving_up_an_attempt_loses_nothing(tmp_path, step, with_old, mode):
    """The moves were made, the status file was not; then the clean-up itself is cut short.

    Every way of giving up (the next start, the failure path, recording the failure) is cut at
    every file operation. The earlier result comes back whole, or nothing looks finished.
    """
    name = "KP_001"
    crash_points = 0
    for at in range(1, 200):
        rdir = str(tmp_path / ("try %d" % at))
        old, old_collected, new = _swap_fixture(rdir, name, with_old, True)
        results.commit_attempt(rdir, name, keep_trimmed_reads=True)
        results.collect(rdir, name)
        with Interrupter(at, mode) as interrupter:
            try:
                if step == "begin_attempt":
                    results.begin_attempt(rdir, name)
                elif step == "abandon_attempt":
                    results.abandon_attempt(rdir, name, keep_logs=True)
                else:
                    results.write_status(rdir, outcome_for(name, RunStatus.FAILED), **status_args())
            except Crash:
                pass
        if results.is_assembled(rdir, name):
            assembly, trimming, _status = swap_states(rdir, name, old, new, 12)
            assert with_old and (assembly, trimming) == ("old", "old"), (at, mode)
        _repair(rdir, name)
        now = {k: v for k, v in snapshot(results.sample_dir(rdir, name)).items()
               if not k.startswith("last_failed_attempt/") and k != "sample_status.json"}
        assert now == {k: v for k, v in old.items() if k != "sample_status.json"}, (at, mode)
        assert results.is_assembled(rdir, name) is with_old, (at, mode)
        if with_old:
            assert read(results.collected_fasta(rdir, name)) == old_collected, (at, mode)
            assert results.read_status(rdir, name).outcome.contigs == 71, (at, mode)
        else:
            assert not os.path.exists(results.collected_fasta(rdir, name)), (at, mode)
        shutil.rmtree(rdir)
        if not interrupter.fired:
            break
        crash_points += 1
    else:
        pytest.fail("it never ran to its end")
    assert crash_points >= 5


def test_a_crash_at_any_step_of_a_whole_attempt_never_looks_finished(tmp_path):
    """From the first leftover to the status file: is_assembled turns true only at the very end."""
    name = "KP_001"
    for at in range(1, 300):
        rdir = str(tmp_path / ("try %d" % at))
        completed = False
        with Interrupter(at, "after") as interrupter:
            try:
                fill_attempt(rdir, name, "v1")
                results.commit_attempt(rdir, name, keep_trimmed_reads=False)
                results.collect(rdir, name)
                results.write_status(rdir, outcome_for(name), **status_args())
                completed = True
            except Crash:
                pass
        if results.is_assembled(rdir, name):
            # Only the very last step can have been cut: the status file is on disk, whole.
            assert results.read_status(rdir, name).outcome.contigs == 71
            assert contigs_tag(results.contigs_path(rdir, name)) == "v1"
            assert contigs_tag(results.collected_fasta(rdir, name)) == "v1"
        else:
            assert not completed
            _repair(rdir, name)
            assert not os.path.exists(results.assembly_dir(rdir, name)), at
            assert not os.path.exists(results.collected_fasta(rdir, name)), at
        shutil.rmtree(rdir)
        if not interrupter.fired:
            assert completed
            break
    else:
        pytest.fail("it never ran to its end")


def test_a_deletion_of_the_leftover_cut_short_leaves_a_folder_we_still_recognise(rdir, monkeypatch):
    """Stop now, then the PC is switched off while gigabytes of reads are being deleted.

    Windows lists a folder by name, so "attempt.json" (the mark that _in_progress is ours) used to
    go before "joined" and "trimming". What was left then carried no mark and no status file, and
    the sample could never be run in this results folder again. The mark must be the last to go.
    """
    name = "KP_001"
    real_rmtree = shutil.rmtree

    def rmtree_by_name_then_power_cut(path, *args, **kwargs):
        for entry in sorted(os.listdir(path), key=str.lower)[:2]:      # two entries go, then darkness
            full = os.path.join(path, entry)
            real_rmtree(full) if os.path.isdir(full) else os.remove(full)
        raise Crash()

    ways = [(False, lambda: results.abandon_attempt(rdir, name, keep_logs=False)),
            (False, lambda: results.begin_attempt(rdir, name)),
            (True, lambda: results.recover_interrupted(rdir)),
            (True, lambda: results.abandon_attempt(rdir, name, keep_logs=True))]
    for swap_cut_short, give_up in ways:
        fill_attempt(rdir, name, "stopped")                 # a first attempt: no status file yet
        if swap_cut_short:
            results.commit_attempt(rdir, name, keep_trimmed_reads=True)
        work = results.in_progress_dir(rdir, name)
        monkeypatch.setattr(shutil, "rmtree", rmtree_by_name_then_power_cut)
        with pytest.raises(Crash):
            give_up()
        monkeypatch.setattr(shutil, "rmtree", real_rmtree)
        assert os.path.isdir(work) and len(os.listdir(work)) > 1           # half of it is still there
        assert os.path.isfile(os.path.join(work, "attempt.json"))
        # The next start clears it without a word, as rule R3 says.
        assert os.listdir(results.begin_attempt(rdir, name)) == ["attempt.json"]
        assert results.abandon_attempt(rdir, name, keep_logs=False) == []
        assert not os.path.exists(work)
    assert not os.path.exists(results.assembly_dir(rdir, name))
    assert results.is_assembled(rdir, name) is False


def test_a_crash_between_making_the_attempt_folder_and_marking_it(rdir, monkeypatch):
    real_replace = os.replace

    def dying_replace(src, dst):
        raise Crash()

    monkeypatch.setattr(os, "replace", dying_replace)
    with pytest.raises(Crash):
        results.begin_attempt(rdir, "KP_001")
    monkeypatch.setattr(os, "replace", real_replace)
    work = results.in_progress_dir(rdir, "KP_001")
    assert [n.split(".tmp")[0] for n in os.listdir(work)] == ["attempt.json"]    # only the half-made note
    assert os.listdir(results.begin_attempt(rdir, "KP_001")) == ["attempt.json"]

    # ...but one file of somebody else's beside it, and the folder is not ours to delete.
    shutil.rmtree(work)
    write(os.path.join(work, "attempt.json.tmp77"), "{")
    theirs = write(os.path.join(work, "draft.docx"), "precious")
    with pytest.raises(FileExistsError):
        results.begin_attempt(rdir, "KP_001")
    assert read(theirs) == "precious"


def test_where_the_log_of_a_failed_attempt_was_kept(rdir):
    work = fill_attempt(rdir, "KP 001", "bad")
    log = os.path.join(work, "assembly", "assembly.log")
    first_try = os.path.join(work, "assembly", "attempt1_default_k", "assembly.log")
    not_kept = os.path.join(work, "assembly", "contigs.fasta")
    assert results.kept_log_path(rdir, "KP 001", log) == ""                  # nothing was kept yet
    results.abandon_attempt(rdir, "KP 001", keep_logs=True)
    failed = results.last_failed_dir(rdir, "KP 001")
    assert results.kept_log_path(rdir, "KP 001", log) == os.path.join(failed, "assembly", "assembly.log")
    assert results.kept_log_path(rdir, "KP 001", first_try) == \
        os.path.join(failed, "assembly", "attempt1_default_k", "assembly.log")
    for elsewhere in (not_kept, "", work, os.path.join(failed, "assembly", "assembly.log"),
                      os.path.join(rdir, "run_log.txt"), os.path.join(work, "..", "..", "x.log")):
        assert results.kept_log_path(rdir, "KP 001", elsewhere) == "", elsewhere


def test_recover_interrupted_sweeps_half_written_bookkeeping_and_names_only_what_it_undid(rdir, monkeypatch):
    """A full disk or a crash in mid-write leaves "run_summary.html.tmp1234" beside the real file."""
    finish(rdir, "KP_001", "old")
    results.save_last_run(rdir, batch(rdir, ["KP_001"]), {"KP_001": RunStatus.DONE},
                          started_at="2026-11-16T18:00:00")
    real = {name: write(os.path.join(rdir, name), "whole") for name in ("run_summary.html", "run_summary.tsv")}
    for name in ("last_run.json.tmp4242", "run_summary.html.tmp4242", "run_summary.tsv.tmp7"):
        write(os.path.join(rdir, name), "hal")
    theirs = write(os.path.join(rdir, "my notes.tmp"), "keep me")
    kept_folder = write(os.path.join(rdir, "last_run.json.tmp.d", "x.txt"), "a folder is not our file")
    half_fasta = write(os.path.join(rdir, "assemblies", "KP_009.fasta.tmp4242"), ">contig_1\nAC")
    someone_s = write(os.path.join(rdir, "assemblies", "other.fasta.tmpl"), "not a name of ours")

    # A proven result whose clean-up had to wait is tidied, but nothing was "put right".
    fill_attempt(rdir, "KP_001", "new")
    results.commit_attempt(rdir, "KP_001", keep_trimmed_reads=True)
    results.collect(rdir, "KP_001")
    real_remove_tree = fsutil.remove_tree
    monkeypatch.setattr(fsutil, "remove_tree", lambda path: False)
    results.write_status(rdir, outcome_for("KP_001", contigs=12), **status_args())
    monkeypatch.setattr(fsutil, "remove_tree", real_remove_tree)
    assert os.path.isdir(results.in_progress_dir(rdir, "KP_001"))

    assert results.recover_interrupted(rdir) == []
    assert sorted(os.listdir(rdir)) == ["KP_001", "assemblies", "last_run.json", "last_run.json.tmp.d",
                                        "my notes.tmp", "run_summary.html", "run_summary.tsv"]
    assert read(theirs) == "keep me" and read(kept_folder) == "a folder is not our file"
    assert not os.path.exists(half_fasta) and read(someone_s) == "not a name of ours"
    assert sorted(os.listdir(results.assemblies_dir(rdir))) == ["KP_001.fasta", "other.fasta.tmpl"]
    assert results.count_assemblies(rdir) == 1
    assert all(read(path) == "whole" for path in real.values())
    assert results.load_last_run(rdir).states["KP_001"] is RunStatus.DONE
    assert not os.path.exists(results.in_progress_dir(rdir, "KP_001"))
    assert results.read_status(rdir, "KP_001").outcome.contigs == 12


def test_a_failure_after_the_contigs_were_taken_away_does_not_disown_our_folders(rdir):
    """The user moved contigs.fasta out of the folder; the sample is run again and fails.

    The status file then says "failed", but "assembly" and "trimming" beside it are still this
    program's own: the sample must not be left out for ever as "a folder not made by this
    program" (audit row 3.7), and the next good attempt must go into place.
    """
    name = "KP_001"
    finish(rdir, name, "old")
    os.remove(results.contigs_path(rdir, name))
    assert results.is_assembled(rdir, name) is False and results.foreign_content(rdir, name) == ""

    for _ in range(2):                                           # it fails, and fails again
        fill_attempt(rdir, name, "bad")
        results.abandon_attempt(rdir, name, keep_logs=True)
        results.write_status(rdir, outcome_for(name, RunStatus.FAILED), **status_args())
        record = results.read_status(rdir, name)
        assert record.status is RunStatus.FAILED and not record.assembled
        assert results.foreign_content(rdir, name) == ""
    assert results.recover_interrupted(rdir) == []

    fill_attempt(rdir, name, "new")
    committed = results.commit_attempt(rdir, name, keep_trimmed_reads=True)
    assert committed.in_place is True and committed.reason == ""
    results.collect(rdir, name)
    results.write_status(rdir, outcome_for(name), **status_args())
    assert results.is_assembled(rdir, name) and contigs_tag(results.contigs_path(rdir, name)) == "new"
    assert sorted(os.listdir(results.sample_dir(rdir, name))) == \
        ["assembly", "last_failed_attempt", "sample_status.json", "trimming"]

    # A "failed" status by itself proves nothing about folders that were there before it.
    write(os.path.join(results.assembly_dir(rdir, "KP_002"), "mine.fasta"), "precious")
    results.write_status(rdir, outcome_for("KP_002", RunStatus.FAILED), **status_args())
    results.write_status(rdir, outcome_for("KP_002", RunStatus.FAILED), **status_args())
    assert results.foreign_content(rdir, "KP_002") == "assembly"
    fill_attempt(rdir, "KP_002", "new")
    assert results.commit_attempt(rdir, "KP_002", keep_trimmed_reads=True).reason == "not_ours"
    assert read(os.path.join(results.assembly_dir(rdir, "KP_002"), "mine.fasta")) == "precious"


@pytest.mark.parametrize("seed", [5, 14, 17])
def test_random_lives_of_a_sample_keep_every_promise(tmp_path, seed):
    """Good runs, failures, stops, crashes at a random file operation and locked folders, in a
    random order. After every step: what counts as assembled is one whole result with its own
    status file and its own copy in assemblies; nothing of anybody else's was touched; and the
    next attempt can always begin (no leftover the program does not recognise as its own).

    These three seeds are ones that, before the review, ended in a leftover _in_progress the
    program took for a stranger's (FileExistsError at every later attempt).
    """
    import random

    rng = random.Random(seed)
    name = "KP 001 [x]"
    real_rename, real_replace = os.rename, os.replace

    def tag_in(folder):
        path = os.path.join(folder, "assembly", "contigs.fasta")
        return contigs_tag(path) if os.path.isfile(path) else None

    def attempt(rdir, tag, trimmed, lock=None):
        """A whole good attempt; ``lock`` names an old entry that another program holds open."""
        fill_attempt(rdir, name, tag, trimmed=trimmed)

        def held(real):
            def call(src, dst):
                if lock and os.path.abspath(src) == lock:
                    raise PermissionError(13, "The process cannot access the file", src)
                return real(src, dst)
            return call

        inner_rename, inner_replace = os.rename, os.replace
        os.rename, os.replace = held(inner_rename), held(inner_replace)
        try:
            committed = results.commit_attempt(rdir, name, keep_trimmed_reads=rng.random() < 0.5)
        finally:
            os.rename, os.replace = inner_rename, inner_replace
        if committed.in_place:
            results.collect(rdir, name)
        written = results.write_status(rdir, outcome_for(name, note=tag, trimmed=trimmed),
                                       **status_args(trimmed=trimmed))
        return committed, written

    for life in range(60):
        rdir = str(tmp_path / ("life %d" % life))
        folder = results.sample_dir(rdir, name)
        theirs = {write(os.path.join(folder, "notes", "lab book.txt"), "plate 7"): "plate 7",
                  write(os.path.join(folder, name + "_R1.fastq.gz"), "reads"): "reads",
                  write(os.path.join(rdir, "thesis.docx"), "chapter 1"): "chapter 1"}
        proven = None                           # the tag of the result that must count just now
        for step in range(rng.randint(3, 9)):
            tag = "g%d" % step
            action = rng.choice(["good", "good", "fail", "fail_late", "stop", "crash", "crash",
                                 "crash_giving_up", "in_use"])
            trimmed = rng.random() < 0.7
            where = (seed, life, step, action)
            if action == "good":
                committed, _written = attempt(rdir, tag, trimmed)
                assert committed.in_place, where
                proven = tag
            elif action == "in_use":
                lock = os.path.join(folder, rng.choice(["assembly", "trimming", "sample_status.json"]))
                committed, written = attempt(rdir, tag, trimmed, lock=lock)
                if committed.in_place:              # nothing was there to be held open
                    proven = tag
                else:
                    assert committed.reason == "in_use" and tag_in(committed.folder) == tag, where
                    assert os.path.dirname(written) == committed.folder, where
            elif action in ("fail", "fail_late", "stop"):
                fill_attempt(rdir, name, tag, trimmed=trimmed)
                if action == "fail_late" and results.commit_attempt(rdir, name, keep_trimmed_reads=True).in_place:
                    results.collect(rdir, name)
                results.abandon_attempt(rdir, name, keep_logs=action != "stop")
                status = RunStatus.STOPPED if action == "stop" else RunStatus.FAILED
                results.write_status(rdir, outcome_for(name, status), **status_args())
            else:
                where += (rng.randint(1, 40), rng.choice(["before", "after", "partial"]))
                with Interrupter(where[-2], where[-1]):
                    try:
                        if action == "crash":
                            attempt(rdir, tag, trimmed)
                        else:
                            fill_attempt(rdir, name, tag, trimmed=trimmed)
                            results.commit_attempt(rdir, name, keep_trimmed_reads=True)
                            results.collect(rdir, name)
                            results.abandon_attempt(rdir, name, keep_logs=True)
                    except Crash:
                        pass
                assert (os.rename, os.replace) == (real_rename, real_replace)
                if results.is_assembled(rdir, name):        # as the crash left it, before any repair
                    stored = json.loads(read(results.status_path(rdir, name)))
                    assert stored["outcome"]["note"] == tag_in(folder) and tag_in(folder) in (proven, tag), where
                    proven = tag_in(folder)
                results.recover_interrupted(rdir)

            # --- the promises
            for path, text in theirs.items():
                assert read(path) == text, where
            assert results.is_assembled(rdir, name) is (proven is not None), where
            if proven is None:
                assert not os.path.exists(results.collected_fasta(rdir, name)), where
            else:
                stored = json.loads(read(results.status_path(rdir, name)))
                assert tag_in(folder) == proven and stored["outcome"]["note"] == proven, where
                assert contigs_tag(results.collected_fasta(rdir, name)) == proven, where
                assert stored["trimmed"] is os.path.isdir(results.trimming_dir(rdir, name)), where
                if stored["trimmed"]:
                    assert proven in read(os.path.join(results.trimming_dir(rdir, name), "trimming.log")), where
        assert set(os.listdir(folder)) <= {"notes", name + "_R1.fastq.gz", "assembly", "trimming",
                                           "sample_status.json", "last_failed_attempt", "new_result",
                                           "_in_progress"}, (seed, life)
        shutil.rmtree(rdir)
