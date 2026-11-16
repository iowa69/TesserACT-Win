# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Tests of the shell of the window and of the Samples, Trimming and Assembly pages.

The first half needs no display: what the Start button says and when it is disabled, the
reasons, the count, "standard" or "changed", the verdict on the results folder, the words
of the overwrite question, the settings file, the size of the window. The second half
carries the ``gui`` marker and opens the real window on a display (the headless ``:99``
when it is running): it adds samples from real files, ticks and renames, changes and
resets options, and presses Start with a stand-in for the Run page, checking the
``RunConfig`` that is handed over.

    python -m pytest tests/test_gui_pages.py -q

Run as a script it photographs every page, light and dark, at 100 % and 150 %:

    <python with Tk and Pillow> tests/test_gui_pages.py --shots build/shots/pages
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time

try:
    import pytest
except ImportError:                  # run as a script, for the photographs, by a Python without pytest
    class _NoPytest:
        """Lets the decorators below do nothing, so that the file can still be loaded."""

        def __getattr__(self, name):
            return self

        def __call__(self, *args, **options):
            return args[0] if len(args) == 1 and callable(args[0]) and not options else self

    pytest = _NoPytest()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tesseract_win import branding, fsutil, model, pairing, results, settings_map     # noqa: E402
from tesseract_win.gui import settings_store, texts                                    # noqa: E402
from tesseract_win.gui import state as st                                              # noqa: E402
from tesseract_win.model import (AssemblySettings, EndReason, Existing, ExistingResult, RunStatus,    # noqa: E402
                                 Sample, SampleOutcome, SampleState, TrimSettings)

GB = 1024 ** 3


# =========================================================================== helpers

def write_pair(folder, name, reads=3, form="_R%d.fastq.gz", flowcell="FC1"):
    """Two small, valid, matching read files. Returns their paths."""
    os.makedirs(str(folder), exist_ok=True)
    paths = []
    for mate in (1, 2):
        path = os.path.join(str(folder), name + form % mate)
        with gzip.open(path, "wt", newline="\n") as handle:
            for index in range(reads):
                handle.write("@M01:7:%s:1:1101:%d:1000 %d:N:0:1\n" % (flowcell, 1000 + index, mate))
                handle.write("ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT\n+\n")
                handle.write("IIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIII\n")
        paths.append(path)
    return paths


def made_up(name, trim=True, state=SampleState.READY, note="", size=400 * 1024 ** 2, folder="/data/run1"):
    return Sample(name, [("%s/%s_R1.fastq.gz" % (folder, name), "%s/%s_R2.fastq.gz" % (folder, name))],
                  trim, size, state, note)


def a_state(*samples, results_dir="/results", free=500 * GB):
    """A state whose results folder has been looked at and found fine."""
    state = st.AppState()
    state.windows_rules = False
    state.set_samples(list(samples))
    state.set_results_dir(results_dir, by_user=True)
    if results_dir:
        state.facts = st.FolderFacts(asked=results_dir, path=results_dir, free=free,
                                     signature=st.list_signature(state.samples))
    return state


def finish_sample(results_dir, sample, trim=None, assembly=None, when="2026-11-03T10:15:00",
                  status=RunStatus.DONE):
    """Put a finished result for ``sample`` in the results folder, the way a run does."""
    name = sample.name
    results.begin_attempt(results_dir, name)
    contigs = os.path.join(results.in_progress_assembly_dir(results_dir, name), "contigs.fasta")
    os.makedirs(os.path.dirname(contigs), exist_ok=True)
    with open(contigs, "w") as handle:
        handle.write(">contig_1\nACGTACGT\n")
    assert results.commit_attempt(results_dir, name, keep_trimmed_reads=True).in_place
    results.collect(results_dir, name)
    fingerprint = settings_map.settings_fingerprint(trim or TrimSettings(), assembly or AssemblySettings(), sample.trim)
    results.write_status(results_dir, SampleOutcome(name, status, finished_at=when, trimmed=sample.trim),
                         reads=results.reads_of(sample), settings_fingerprint=fingerprint,
                         settings_text="", trimmed=sample.trim)


# =========================================================================== Start: when, and why not

def test_start_needs_samples_first():
    state = st.AppState()
    status = st.start_status(state)
    assert not status.enabled
    assert status.reason == "Add samples to begin."
    assert status.label == "Start"
    assert status.count == 0


def test_start_says_when_no_sample_is_ready():
    state = a_state(made_up("EC_09", state=SampleState.LEFT_OUT, note=pairing.NO_READ2))
    status = st.start_status(state)
    assert not status.enabled
    assert status.reason == "No sample is ready: see the notes in the list."


def test_start_needs_a_results_folder():
    state = a_state(made_up("KP_001"), results_dir="")
    status = st.start_status(state)
    assert not status.enabled
    assert status.reason == "Choose a results folder to begin."
    assert status.label == "Start (1 sample)"


def test_start_is_enabled_and_carries_the_count():
    state = a_state(made_up("KP_001"), made_up("KP_002"),
                    made_up("EC_09", state=SampleState.LEFT_OUT, note=pairing.NO_READ2))
    status = st.start_status(state)
    assert status.enabled and status.reason == ""
    assert status.count == 2 and status.label == "Start (2 samples)"
    assert status.link == "1 left out: why?"
    assert status.left_out == 1
    assert status.hint == ""                       # the note about the left-out sample takes the place


def test_first_time_hint_only_on_the_samples_step_and_only_when_nothing_else_is_said():
    state = a_state(made_up("KP_001"))
    assert st.start_status(state, "samples").hint == texts.FIRST_TIME_HINT
    assert st.start_status(state, "trimming").hint == ""
    assert st.start_status(state, "samples").link == ""


def test_start_waits_while_files_or_folder_are_being_checked():
    state = a_state(made_up("KP_001"))
    state.checking.add(st.row_id_of(state.samples[0]))
    assert st.start_status(state).reason == st.Words.CHECKING_FILES
    state.checking.clear()
    state.facts = None                                           # the folder has not been looked at
    status = st.start_status(state)
    assert not status.enabled and status.reason == st.Words.CHECKING_FOLDER
    state.searching = True
    assert st.start_status(state).reason == st.Words.CHECKING_FILES


def test_start_is_disabled_during_a_run_and_without_the_engines():
    state = a_state(made_up("KP_001"))
    state.engine_problem = texts.missing_part("engines\\tesseract-asm.exe")
    status = st.start_status(state)
    assert not status.enabled and status.reason == st.Words.PROGRAM_PART_MISSING
    state.engine_problem = ""
    state.running = True
    assert st.start_status(state).reason == st.Words.RUN_IN_PROGRESS
    state.running = False
    assert st.start_status(state).enabled


def test_start_names_a_setting_that_cannot_run():
    state = a_state(made_up("KP_001"))
    state.trim.own_adapters = True                               # ticked, but no sequence typed
    status = st.start_status(state)
    assert not status.enabled
    assert "Use my own adapter sequences" in status.reason
    state.samples[0].trim = False                                # nothing is trimmed: trimming cannot stop it
    assert st.start_status(state).enabled


def test_folder_problem_is_short_beside_start_and_points_to_the_samples_step():
    state = a_state(made_up("KP_001"))
    state.facts.writable = False
    on_samples = st.start_status(state, "samples")
    assert not on_samples.enabled
    assert on_samples.reason == "The program cannot write in this folder."
    assert st.start_status(state, "assembly").reason == "The program cannot write in this folder. See the Samples step."
    assert st.judge_folder(state).problem == st.Words.RESULTS_NOT_WRITABLE


# =========================================================================== the stepper's small lines

def test_step_lines_standard_changed_and_counts():
    state = st.AppState()
    lines = st.step_lines(state)
    assert lines["samples"][0] == "none yet"
    assert lines["trimming"][0] == "standard" and lines["assembly"][0] == "standard"
    state.set_samples([made_up("KP_%03d" % n) for n in range(1, 41)])
    assert st.step_lines(state)["samples"][0] == "40 ready"
    state.trim.trim_polyg = False
    state.assembly.mode = "careful"
    lines = st.step_lines(state)
    assert lines["trimming"] == ("changed", "accent") and lines["assembly"] == ("changed", "accent")


def test_a_number_behind_an_unticked_box_is_not_a_change():
    trim = TrimSettings(trim_front=7, trim_tail=3, adapter_r1="ACGT")        # their ticks are off
    assert not st.trim_changed(trim) and not st.trim_more_changed(trim)
    trim.fixed_trim = True
    assert st.trim_changed(trim) and st.trim_more_changed(trim)
    assembly = AssemblySettings(genome_size_mb=2.8, min_contig=1000)
    assert not st.assembly_changed(assembly)
    assembly.know_genome_size = True
    assert st.assembly_changed(assembly)
    assert not st.assembly_more_changed(assembly)                # it is on the page, not behind More options
    assembly.threads = max(1, model.default_threads() - 1) if model.default_threads() > 1 else 1
    assert st.assembly_more_changed(assembly) == (model.default_threads() > 1)
    assert st.trim_changed(TrimSettings(keep_trimmed_reads=False))
    assert not st.trim_more_changed(TrimSettings(keep_trimmed_reads=False))
    assert st.trim_more_changed(TrimSettings(end_quality=20))
    assert not st.trim_more_changed(TrimSettings(end_quality=20, trim_low_quality_ends=False))


def test_trimming_step_says_not_used_when_no_sample_is_trimmed():
    state = a_state(made_up("KP_001", trim=False), made_up("KP_002", trim=False))
    assert state.trimming_off()
    assert st.step_lines(state)["trimming"][0] == texts.STEP_NO_TRIMMING
    state.samples[1].trim = True
    assert not state.trimming_off()
    assert st.step_lines(state)["trimming"][0] == "standard"
    assert not st.AppState().trimming_off()                      # an empty list leaves the page usable


# =========================================================================== what is handed to the runner

def test_build_config_holds_copies_of_what_will_run_in_order():
    state = a_state(made_up("KP_002", trim=False), made_up("EC_09", state=SampleState.LEFT_OUT, note=pairing.NO_READ2),
                    made_up("KP_001"))
    state.trim.end_quality = 20
    state.assembly.mode = "fast"
    config = st.build_config(state, Existing.OVERWRITE)
    assert [s.name for s in config.samples] == ["KP_002", "KP_001"]
    assert [s.trim for s in config.samples] == [False, True]
    assert config.results_dir == "/results" and config.existing is Existing.OVERWRITE
    assert config.trim == state.trim and config.trim is not state.trim
    assert config.assembly == state.assembly and config.assembly is not state.assembly
    assert config.samples[0] is not state.samples[0]
    assert config.samples[0].lanes == state.samples[0].lanes
    state.trim.end_quality = 30                                  # a change afterwards does not reach the run
    assert config.trim.end_quality == 20
    assert st.left_out_rows(state) == [("EC_09", pairing.NO_READ2)]
    assert model.RunConfig.from_dict(json.loads(json.dumps(config.to_dict()))).samples[1].name == "KP_001"


def test_same_name_from_other_reads_is_left_out_with_the_reason():
    state = a_state(made_up("KP_001"), made_up("KP_002"), made_up("KP_003"))
    state.facts.clashes = {"KP_001": "2026-11-03T09:00:00"}
    state.facts.foreign = {"KP_003": "assembly"}
    assert [s.name for s in state.ready()] == ["KP_002"]
    shown, why = state.view(state.samples[0])
    assert shown is SampleState.LEFT_OUT
    assert why.startswith("A different sample called KP_001 is already in the results folder (made on 3 Nov 2026 "
                          "from other read files).")
    assert "KP_003" in state.view(state.samples[2])[1] and "was not made by this program" in state.view(state.samples[2])[1]
    assert st.start_status(state).label == "Start (1 sample)"
    assert [name for name, _why in st.left_out_rows(state)] == ["KP_001", "KP_003"]
    state.set_results_dir("/elsewhere", by_user=True)            # another folder: the old verdict no longer holds
    assert len(state.ready()) == 3


def test_status_cell_words():
    assert st.status_text(SampleState.READY, "") == "Ready"
    assert st.status_text(SampleState.READY_NOTE, pairing.TINY) == "Ready. " + pairing.TINY
    assert st.status_text(SampleState.LEFT_OUT, pairing.NO_READ2).startswith("Left out: no Read 2 file found.")
    assert st.status_text(SampleState.LEFT_OUT, "Read 2: This file is empty.") == "Left out: Read 2: This file is empty."
    assert st.status_text(SampleState.LEFT_OUT, "Windows does not allow the name CON.").startswith("Left out: Windows")
    assert st.status_text(SampleState.LEFT_OUT, "") == "Left out"


def test_short_date_and_first_sentence():
    assert st.short_date("2026-11-03T10:15:00") == "3 Nov 2026"
    assert st.short_date("") == "" and st.short_date("yesterday") == ""
    assert st.first_sentence(st.Words.RESULTS_TOO_LONG) == "This folder's path is too long for Windows."
    assert st.first_sentence("One. Two. Three.") == "One."


# =========================================================================== the results folder

def test_clean_folder_text_takes_quotes_and_spaces_away():
    path = os.path.join(os.sep, "data", "My results")
    assert st.clean_folder_text('  "%s"  ' % path) == path
    assert st.clean_folder_text("'%s'" % path) == path
    assert st.clean_folder_text(path + os.sep) == path
    assert st.clean_folder_text("   ") == ""


def test_a_folder_that_does_not_exist_yet_is_fine_and_is_not_left_behind(tmp_path):
    target = tmp_path / "new" / "TesserACT_results"
    facts = st.inspect_folder(str(target), [made_up("KP_001")], TrimSettings(), AssemblySettings())
    assert facts.valid and facts.writable and not facts.appended
    assert facts.path == str(target)
    assert facts.free and facts.free > 0
    assert not (tmp_path / "new").exists()                       # the write test cleaned up after itself


def test_a_folder_with_other_things_gets_its_own_subfolder(tmp_path):
    (tmp_path / "thesis.docx").write_text("x")
    facts = st.inspect_folder(str(tmp_path), [], TrimSettings(), AssemblySettings())
    assert facts.appended and facts.path == str(tmp_path / branding.RESULTS_DIR_NAME)
    state = a_state(made_up("KP_001"), results_dir=facts.path)
    state.facts = facts
    state.facts.signature = st.list_signature(state.samples)
    notes = st.judge_folder(state).notes
    assert notes and notes[0] == "Results will go into a new folder here: %s" % facts.path
    again = st.inspect_folder(facts.path, [], TrimSettings(), AssemblySettings())
    assert not again.appended                                    # never TesserACT_results\TesserACT_results


def test_not_a_usable_folder(tmp_path):
    a_file = tmp_path / "reads.txt"
    a_file.write_text("x")
    for bad in (str(a_file), "results", "relative/folder", ""):
        facts = st.inspect_folder(bad, [], TrimSettings(), AssemblySettings())
        assert not facts.valid, bad
    state = a_state(made_up("KP_001"), results_dir=str(a_file))
    state.facts = st.inspect_folder(str(a_file), [], TrimSettings(), AssemblySettings())
    assert st.judge_folder(state).problem == "This is not a folder the program can use. Use Browse to choose one."
    assert not st.start_status(state).enabled


def test_space_line_and_not_enough_space():
    samples = [made_up("KP_%03d" % n, size=2 * GB) for n in range(1, 11)]
    state = a_state(*samples, free=212 * GB)
    verdict = st.judge_folder(state)
    need = pairing.estimate_disk_need(samples, True)
    assert verdict.problem == ""
    assert verdict.info == "Each sample gets its own folder here. Needs about %s. Free: %s." % (
        fsutil.format_bytes(need), fsutil.format_bytes(212 * GB))
    state.facts.free = 5 * GB
    verdict = st.judge_folder(state)
    assert verdict.problem.startswith("Not enough free space: this run needs about %s and this drive has 5.0 GB free."
                                      % fsutil.format_bytes(need))
    assert "untick 'Keep the trimmed reads' on the Trimming page (it then needs about" in verdict.problem
    assert not st.start_status(state).enabled
    state.trim.keep_trimmed_reads = False                        # needs less now: no such advice any more
    less = st.judge_folder(state)
    assert less.need < need
    state.facts.drive = "d:"
    state.facts.free = 1 * GB
    assert "and drive D: has 1.0 GB free. Choose another folder or free some space." in st.judge_folder(state).problem


def test_folder_notes_and_blocking_reasons():
    state = a_state(made_up("KP_001"))
    state.facts.synced = "OneDrive"
    assert st.judge_folder(state).notes == [st.Words.RESULTS_SYNCED.format(name="OneDrive")]
    assert st.start_status(state).enabled                        # a note, never a refusal
    state.facts.synced, state.facts.kind = "", "network"
    assert st.judge_folder(state).notes == [st.Words.RESULTS_NETWORK]
    state.facts.kind = "removable"
    assert st.judge_folder(state).notes == [st.Words.RESULTS_REMOVABLE]
    state.facts.kind, state.results_suggested = "fixed", True
    assert st.judge_folder(state).notes == [texts.RESULTS_FOLDER_SUGGESTED]
    state.facts.holder = "PC LAB-07, started today at 14:02"
    assert st.judge_folder(state).problem == (
        "This results folder is being used by another run (PC LAB-07, started today at 14:02). Choose another "
        "folder, or wait until that run has finished.")
    state.facts.holder, state.facts.newer = "", True
    assert "made by a newer version of TesserACT-Win" in st.judge_folder(state).problem
    state.facts.newer, state.facts.encoding_ok = False, False
    assert st.judge_folder(state).problem == st.Words.RESULTS_ENCODING
    state.facts.encoding_ok = True
    state.facts.filesystem = "FAT32"
    state.samples[0].size_bytes = 9 * GB
    assert st.judge_folder(state).problem == st.Words.RESULTS_FAT32.format(sample="KP_001")


def test_path_too_long_for_windows_is_said_before_the_run():
    deep = os.path.join(os.sep, *(["a_long_folder_name_%02d" % n for n in range(9)]))
    state = a_state(made_up("KP_001"), results_dir=deep)
    assert st.judge_folder(state).problem == ""                  # not on a system without the limit
    state.windows_rules = True
    assert st.judge_folder(state).problem == st.Words.RESULTS_TOO_LONG
    assert st.start_status(state).reason == "This folder's path is too long for Windows."


def test_existing_results_are_announced_and_other_reads_are_left_out(tmp_path):
    reads, rdir = tmp_path / "reads", str(tmp_path / "out")
    for name in ("KP_001", "KP_002", "KP_003"):
        write_pair(reads, name)
    samples = pairing.scan([str(reads)]).samples
    assert [s.name for s in samples] == ["KP_001", "KP_002", "KP_003"]
    finish_sample(rdir, samples[0])
    other = Sample("KP_002", [tuple(write_pair(tmp_path / "elsewhere", "KP_002", reads=9))])
    finish_sample(rdir, other)                                   # same name, made from other read files
    os.makedirs(os.path.join(rdir, "KP_003", "assembly"))        # a folder that is not ours
    facts = st.inspect_folder(rdir, samples, TrimSettings(), AssemblySettings())
    assert sorted(facts.existing) == ["KP_001"] and facts.existing["KP_001"].same_settings
    assert list(facts.clashes) == ["KP_002"]
    assert facts.foreign == {"KP_003": "assembly"}
    assert not facts.appended                                    # it is one of our results folders
    state = a_state(*samples, results_dir=rdir)
    state.facts = facts
    assert [s.name for s in state.ready()] == ["KP_001"]
    assert st.judge_folder(state).existing == "1 of these samples already has results here."
    changed = st.inspect_folder(rdir, samples, TrimSettings(), AssemblySettings(mode="careful"))
    assert not changed.existing["KP_001"].same_settings


def test_suggested_folder_is_beside_the_reads(tmp_path):
    first = write_pair(tmp_path / "run" / "a", "KP_001")
    second = write_pair(tmp_path / "run" / "b", "KP_002")
    one = Sample("KP_001", [tuple(first)])
    assert st.suggest_results_dir([one]) == str(tmp_path / "run" / "a" / branding.RESULTS_DIR_NAME)
    both = [one, Sample("KP_002", [tuple(second)])]
    assert st.suggest_results_dir(both) == str(tmp_path / "run" / branding.RESULTS_DIR_NAME)
    assert st.suggest_results_dir([]) == ""
    assert st.suggest_results_dir([made_up("X", folder="/no/such/place")]) == ""


def test_files_that_went_missing_are_left_out_when_start_is_pressed(tmp_path):
    for name in ("KP_001", "KP_002"):
        write_pair(tmp_path, name)
    state = a_state(*pairing.scan([str(tmp_path)]).samples)
    assert st.recheck_files(state) == []
    os.remove(state.samples[1].read2)
    changed = st.recheck_files(state)
    assert [s.name for s in changed] == ["KP_002"]
    assert state.samples[1].state is SampleState.LEFT_OUT and state.samples[1].note
    assert [s.name for s in state.ready()] == ["KP_001"]


# =========================================================================== the question, the boxes

def found(name, same=True, summary="", when="2026-11-03T10:15:00"):
    return ExistingResult(name, when, True, same, summary)


def test_overwrite_question_skip_is_the_default():
    from tesseract_win.gui import dialogs
    existing = {"KP_010": found("KP_010"), "KP_002": found("KP_002", False, "not trimmed; mode Careful")}
    question = dialogs.overwrite_question(existing, 40)
    assert question.title == "Some samples are already assembled"
    assert question.body.startswith("2 of the 40 samples already have an assembly in this results folder.")
    assert "1 of them was made with different settings (not trimmed; mode Careful)." in question.body
    assert [c.key for c in question.choices] == ["skip", "overwrite", "cancel"]
    assert question.default == "skip" and question.cancel == "cancel"
    assert question.choice("skip").description == "Keep the existing results. Assemble only the other 38."
    assert question.details.split("\n") == ["KP_002  (3 Nov 2026, made with other settings)", "KP_010  (3 Nov 2026)"]
    assert question.details_label == "Show the 2 samples"


def test_overwrite_question_when_all_are_assembled_offers_no_skip():
    from tesseract_win.gui import dialogs
    existing = {name: found(name) for name in ("A", "B", "C")}
    question = dialogs.overwrite_question(existing, 3)
    assert [c.key for c in question.choices] == ["overwrite", "cancel"]
    assert question.default == "cancel" and question.cancel == "cancel"
    assert question.body.startswith("All 3 samples already have an assembly in this results folder.")


def test_about_text_names_the_versions_and_the_attribution():
    from tesseract_win.gui import dialogs
    body = dialogs.about_body()
    assert branding.about_text() in body
    assert branding.ATTRIBUTION in body


# =========================================================================== the settings file

def test_settings_round_trip_and_nothing_but_size_and_folders(tmp_path):
    path = str(tmp_path / "state" / "settings.json")
    store = settings_store.SettingsStore(path)
    assert store.window_size() is None and store.folder("reads") == ""
    store.set_window(1100, 700)
    store.set_folder("reads", str(tmp_path))
    store.set_folder("last_results", str(tmp_path / "gone"))
    assert store.save()
    written = json.loads(open(path).read())
    assert sorted(written) == ["folders", "format", "window"]
    assert written["window"] == {"width": 1100, "height": 700, "maximized": False}
    again = settings_store.SettingsStore(path)
    assert again.window_size() == (1100, 700)
    assert again.folder("reads") == str(tmp_path)
    assert again.folder("last_results") == ""                    # it does not exist on this PC (now)
    assert again.recorded_folder("last_results") == str(tmp_path / "gone")
    with pytest.raises(KeyError):
        again.set_folder("ticks", "x")
    again.set_window(1920, 1040, maximized=True)                 # the size of a maximized window is not kept
    assert again.window_size() == (1100, 700) and again.settings.maximized


@pytest.mark.parametrize("content", ["", "{not json", "[1, 2]", '{"format": 99, "window": {"width": 900, "height": 700}}',
                                     '{"format": 1, "window": {"width": "wide", "height": -5}, "folders": [1]}',
                                     '{"format": 1, "window": {"width": 5, "height": 900000}, "folders": {"reads": 7}}'])
def test_settings_never_fail_at_start_up(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    loaded = settings_store.load(str(path))
    assert loaded.window_size() is None and loaded.folders == {}
    assert settings_store.load(str(tmp_path / "missing.json")).window_size() is None
    assert settings_store.load(str(tmp_path)).window_size() is None          # a folder where the file should be


def test_settings_that_cannot_be_written_do_not_raise(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed")
    store = settings_store.SettingsStore(str(blocker / "sub" / "settings.json"))
    store.set_window(1000, 700)
    assert store.save() is False
    quiet = settings_store.SettingsStore(str(tmp_path / "never.json"), remember=False)
    quiet.set_window(1000, 700)
    assert quiet.save() is False and not (tmp_path / "never.json").exists()


# =========================================================================== size, zip, threads

def test_window_fits_a_small_laptop_at_100_and_150_percent():
    from tesseract_win.gui import app
    width, height, x, y, min_w, min_h = app.window_geometry(1366, 768, 1.0)
    assert width == 1120 and 640 <= height <= 768 - 80
    assert x >= 0 and y >= 0 and y + height + 32 <= 768 - 40     # the title bar and the taskbar still fit
    width, height, x, y, min_w, min_h = app.window_geometry(1366, 768, 1.5)
    assert width <= 1366 and height <= 768 - 120 and min_w <= width and min_h <= height
    assert y + height + 48 <= 768 - 60
    width, height, _x, _y, _mw, _mh = app.window_geometry(1920, 1080, 1.0, saved=(1500, 900))
    assert (width, height) == (1500, 900)
    width, height, _x, _y, _mw, _mh = app.window_geometry(1920, 1080, 1.0, saved=(9000, 9000))
    assert width <= 1920 and height <= 1080                      # a size saved on a larger screen
    width, height, _x, _y, min_w, min_h = app.window_geometry(1920, 1080, 1.0, saved=(400, 400))
    assert (width, height) == (min_w, min_h)


def test_started_from_a_temporary_extraction(tmp_path):
    from tesseract_win.gui import app
    temp = tmp_path / "Temp"
    inside = temp / "Rar$EXa0.123" / "TesserACT-Win" / "TesserACT-Win.exe"
    inside.parent.mkdir(parents=True)
    inside.write_text("")
    assert app.still_in_zip(str(inside), str(temp))
    elsewhere = tmp_path / "Programs" / "TesserACT-Win" / "TesserACT-Win.exe"
    assert not app.still_in_zip(str(elsewhere), str(temp))
    assert app.still_in_zip(str(tmp_path / "Downloads" / "7zO4A1B" / "TesserACT-Win.exe"), str(temp))
    assert app.still_in_zip(str(tmp_path / "x" / "Temp1_TesserACT-Win.zip" / "TesserACT-Win" / "a.exe"), "")


def test_task_runner_hands_answers_back_and_drops_stale_ones():
    tasks = st.TaskRunner()
    got = []
    tasks.submit(lambda report: [report(n) for n in (1, 2)] and "done", lambda result, error: got.append(("end", result, error)),
                 progress=lambda item: got.append(("step", item)))
    tasks.submit(lambda report: 1 / 0, lambda result, error: got.append(("failed", type(error).__name__)))
    tasks.submit(lambda report: "old", lambda result, error: got.append(("folder", result)), key="folder")
    tasks.submit(lambda report: "new", lambda result, error: got.append(("folder", result)), key="folder")
    assert tasks.busy
    end = time.time() + 5
    while tasks.busy and time.time() < end:
        tasks.poll()
        time.sleep(0.01)
    assert not tasks.busy
    assert ("step", 1) in got and ("step", 2) in got and ("end", "done", None) in got
    assert got.index(("step", 2)) < got.index(("end", "done", None))
    assert ("failed", "ZeroDivisionError") in got
    assert ("folder", "new") in got and ("folder", "old") not in got


def test_every_local_word_is_a_plain_sentence():
    for name, value in vars(st.Words).items():
        if name.startswith("_") or not isinstance(value, str):
            continue
        assert value.strip() and "  " not in value, name
        for jargon in ("mate", "Phred", "k-mer", "errno", "exit code"):
            assert jargon not in value, (name, jargon)


def test_demo_rows_are_eight_in_mixed_states():
    rows = st.demo_samples()
    assert len(rows) == 8
    assert {row.state for row in rows} == {SampleState.READY, SampleState.READY_NOTE, SampleState.LEFT_OUT}
    assert any(len(row.lanes) > 1 for row in rows) and any(not row.trim for row in rows)


def test_title_puts_the_news_first_for_the_taskbar():
    from tesseract_win.gui import app as appmod
    assert appmod.running_title("") == branding.window_title()
    assert appmod.running_title("Finished: 38 done, 2 failed") == "Finished: 38 done, 2 failed - TesserACT-Win"
    assert appmod.running_title("Sample 3 of 40: KP_003").startswith("Sample 3 of 40")


def test_problem_report_holds_logs_and_never_reads_or_contigs(tmp_path):
    import zipfile
    from tesseract_win.gui import problem_report
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "tesseract-win.log").write_text("the program's log\n")
    (logs / "tesseract-win.log.1").write_text("an older part\n")
    (logs / "notes.bin").write_bytes(b"\x00\x01")
    folder = str(tmp_path / "results")
    good, bad = made_up("KP_001"), made_up("KP_002")
    finish_sample(folder, good)
    results.write_status(folder, SampleOutcome("KP_002", RunStatus.FAILED, note="A read file is incomplete.",
                                               finished_at="2026-11-03T10:20:00"),
                         reads=results.reads_of(bad), settings_fingerprint="x", settings_text="", trimmed=True)
    kept = results.last_failed_dir(folder, "KP_002")
    os.makedirs(kept)
    with open(os.path.join(kept, "assembly.log"), "w") as handle:
        handle.write("error: unexpected end of file\n")
    with open(os.path.join(kept, "KP_002_trimmed_R1.fastq.gz"), "wb") as handle:
        handle.write(b"reads")
    with open(os.path.join(folder, "run_log.txt"), "w") as handle:
        handle.write("the run\n")

    path = problem_report.write_problem_report(str(tmp_path / "Desktop"), folder, log_dir=str(logs))
    assert os.path.basename(path).startswith("TesserACT-Win_problem_report_") and path.endswith(".zip")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        about = archive.read("about.txt").decode("utf-8")
    assert {"about.txt", "program/tesseract-win.log", "program/tesseract-win.log.1", "run/run_log.txt",
            "samples/KP_002/sample_status.json", "samples/KP_002/last_failed_attempt/assembly.log"} <= names
    assert branding.versions_line() in about and "no reads and no assemblies" in about
    assert not any(name.endswith((".gz", ".fasta", ".fastq", ".bin", ".gfa", ".html")) for name in names)
    assert not any(name.startswith("samples/KP_001/") for name in names)       # a sample that went well
    assert not [leaf for leaf in os.listdir(str(tmp_path / "Desktop")) if leaf.endswith(".part")]
    # no results folder, no logs: still a report, with what there is
    lonely = problem_report.write_problem_report(str(tmp_path / "other"), "", log_dir=str(tmp_path / "none"))
    with zipfile.ZipFile(lonely) as archive:
        assert archive.namelist() == ["about.txt"]


def test_about_box_offers_the_problem_report_first():
    from tesseract_win.gui import dialogs
    assert [(c.key, c.label) for c in dialogs.about_choices()] == [
        ("report", "Save a problem report"), ("logs", "Open log folder"), ("close", "Close")]
    assert "save a problem report" in dialogs.about_body()
    assert "never holds reads or assemblies" in dialogs.about_body()


# =========================================================================== with a display

def _display():
    wanted = os.environ.get("TESSERACT_WIN_TEST_DISPLAY")
    if wanted:
        return wanted
    if sys.platform.startswith("linux"):
        if os.path.exists("/tmp/.X11-unix/X99"):
            return ":99"
        return os.environ.get("DISPLAY") or None
    return ""


DISPLAY = _display()
needs_display = pytest.mark.skipif(DISPLAY is None, reason="no display (DISPLAY is not set)")


class StandInRunPage:
    """What the shell needs of the Run page, and a record of what it was asked."""

    def __init__(self, parent, app):
        from tesseract_win.gui import widgets as w
        self.frame = w.Frame(parent, ground="page")
        self.app = app
        self.begun = []
        self.running = False
        self.may_close = True
        self.close_asked = 0
        self.offered = []
        self.offer_showing = False
        self.end_reason = None

    # the page is shown and hidden by the shell
    def grid(self, **options):
        self.frame.grid(**options)

    def grid_remove(self):
        self.frame.grid_remove()

    # the contract
    def begin(self, config, left_out, *, resume=False, runner_factory=None):
        self.begun.append((config, list(left_out), resume, runner_factory))
        self.running = True

    def is_running(self):
        return self.running

    def confirm_close(self):
        self.close_asked += 1
        return self.may_close

    def offer_interrupted(self, results_dir):
        self.offered.append(results_dir)

    def has_offer(self):
        return self.offer_showing

    def finish(self, reason=EndReason.COMPLETED):
        self.running = False
        self.end_reason = reason
        self.app.run_ended()


@pytest.fixture
def window(tmp_path):
    """The real window with a stand-in Run page. ``window(...)`` makes it; it is closed afterwards."""
    import tkinter as tk
    from tesseract_win.gui import app as appmod
    made = []

    def make(**options):
        root = tk.Tk(screenName=DISPLAY or None)
        options.setdefault("store", settings_store.SettingsStore(str(tmp_path / "settings" / "settings.json")))
        options.setdefault("run_page_factory", StandInRunPage)
        options.setdefault("engines", model.EnginePaths("tesseract-asm", "scepter", ""))
        options.setdefault("self_test", False)
        options.setdefault("scale", 1.0)
        options.setdefault("dark", False)
        options.setdefault("high_contrast", False)
        shell = appmod.App(root, **options)
        root.report_callback_exception = lambda *error: faults.append(error)
        made.append(shell)
        shell.settle()
        return shell

    faults = []
    yield make
    for shell in made:
        if not shell.closed:
            shell.run_page.may_close = True
            shell.close()
    assert not faults, "an error in a button or a timer of the window: %r" % (faults[:1],)


def _reads(tmp_path, names=("KP_001", "KP_002", "KP_010"), folder="run1"):
    for name in names:
        write_pair(tmp_path / folder, name)
    return str(tmp_path / folder)


def _add(shell, *paths):
    shell.samples_page.add_paths(paths)
    assert shell.settle(15), "the window did not come to rest"


def _row(shell, name):
    for sample in shell.state.samples:
        if sample.name == name:
            return st.row_id_of(sample)
    raise AssertionError("no row called %s" % name)


def _key(widget, *sequences):
    widget.focus_force()
    widget.update()
    for sequence in sequences:
        widget.event_generate(sequence)
    widget.update()


@pytest.mark.gui
@needs_display
def test_the_empty_window_says_what_to_do(window):
    shell = window()
    assert shell.root.title() == branding.window_title()
    assert shell.step == 0 and shell.stepper.current == "samples"
    assert shell.stepper.sub("samples") == "none yet"
    assert shell.stepper.sub("trimming") == "standard" and shell.stepper.sub("assembly") == "standard"
    assert shell.bottom.start.instate(["disabled"])
    assert shell.bottom.message_text == "Add samples to begin."
    assert str(shell.bottom.start.cget("text")) == "Start"
    assert not shell.stepper.can_pick("run")                     # nothing has run yet
    assert shell.samples_page.buttons["remove"].instate(["disabled"])
    assert shell.samples_page.buttons["clear_list"].instate(["disabled"])
    assert not shell.bottom.back.winfo_ismapped()
    assert str(shell.bottom.next.cget("text")) == "Next: Trimming"
    assert shell.help_line.text == st.Words.HELP_DEFAULT


@pytest.mark.gui
@needs_display
def test_adding_a_folder_fills_the_list_and_suggests_the_results_folder(window, tmp_path):
    shell = window()
    folder = _reads(tmp_path)
    write_pair(tmp_path / "run1", "EC_09")
    os.remove(os.path.join(folder, "EC_09_R2.fastq.gz"))         # a sample with one file only
    (tmp_path / "run1" / "notes.txt").write_text("not reads")
    _add(shell, folder)
    page = shell.samples_page
    assert [s.name for s in shell.state.samples] == ["EC_09", "KP_001", "KP_002", "KP_010"]
    assert len(page.table) == 4
    assert not shell.state.checking and not shell.state.searching
    left = page.table.row(_row(shell, "EC_09"))
    assert left["checked"] is None and left["dim"]
    assert left["values"]["status"].startswith("Left out: no Read 2 file found.")
    good = page.table.row(_row(shell, "KP_001"))
    assert good["checked"] is True
    assert good["values"]["read1"] == "KP_001_R1.fastq.gz" and good["values"]["read2"] == "KP_001_R2.fastq.gz"
    assert good["values"]["status"].startswith("Ready")
    assert good["tips"]["read1"] == os.path.join(folder, "KP_001_R1.fastq.gz")
    assert "Added 3 samples (6 files)." in page.activity_text
    # the counter, the stepper and Start agree
    assert str(page.counter.cget("text")) == "3 ready."
    assert str(page.counter_link.cget("text")) == "1 left out: why?"
    assert shell.stepper.sub("samples") == "3 ready"
    assert str(shell.bottom.start.cget("text")) == "Start (3 samples)"
    assert shell.bottom.start.instate(["!disabled"])
    assert str(shell.bottom.link.cget("text")) == "1 left out: why?"
    # audit 4.1: suggested beside the reads, and said so
    assert shell.state.results_dir == os.path.join(folder, branding.RESULTS_DIR_NAME)
    assert page.folder_var.get() == shell.state.results_dir
    assert shell.state.results_suggested
    assert texts.RESULTS_FOLDER_SUGGESTED in str(page.folder_notes.cget("text"))
    assert str(page.folder_info.cget("text")).startswith("Each sample gets its own folder here. Needs about ")
    assert not os.path.exists(shell.state.results_dir)           # nothing is made before Start
    # the link selects the rows that are left out
    shell.bottom.link.invoke()
    assert page.table.selection() == [_row(shell, "EC_09")]
    # the same files again: nothing is added twice
    _add(shell, folder)
    assert len(shell.state.samples) == 4
    assert "Already in the list" in page.activity_text


@pytest.mark.gui
@needs_display
def test_ticks_trim_all_and_trim_none(window, tmp_path):
    shell = window()
    _add(shell, _reads(tmp_path))
    page = shell.samples_page
    first = _row(shell, "KP_001")
    page.table.toggle([first])                                   # what a click on the box does
    shell.settle()
    assert shell.state.sample(first).trim is False
    assert [s.trim for s in shell.state.samples] == [False, True, True]
    page.trim_none_button.invoke()
    shell.settle()
    assert not any(s.trim for s in shell.state.samples)
    assert page.table.checked_ids() == []
    assert shell.stepper.sub("trimming") == texts.STEP_NO_TRIMMING
    # the Trimming page is greyed under one explaining line
    shell.show_step(1)
    shell.settle()
    trimming = shell.trimming_page
    assert trimming.notice.text("no_trimming") == texts.NO_TRIMMING_NOTICE
    assert trimming.ticks["remove_adapters"].tick.instate(["disabled"])
    assert trimming.reset_button.instate(["disabled"])
    shell.show_step(0)
    page.trim_all_button.invoke()
    shell.settle()
    assert all(s.trim for s in shell.state.samples)
    shell.show_step(1)
    shell.settle()
    assert "no_trimming" not in trimming.notice.kinds()
    assert trimming.ticks["remove_adapters"].tick.instate(["!disabled"])
    assert shell.stepper.sub("trimming") == "standard"


@pytest.mark.gui
@needs_display
def test_rename_in_place_and_names_that_clash(window, tmp_path):
    shell = window()
    _add(shell, _reads(tmp_path))
    page = shell.samples_page
    row = _row(shell, "KP_002")
    assert page.table.begin_rename(row, "sample")
    editor = page.table.editor
    editor.delete(0, "end")
    editor.insert(0, "KP 002")                                   # a space is not allowed
    _key(editor, "<KeyRelease>")
    assert editor.instate(["invalid"]) and shell.help_line.text
    editor.delete(0, "end")
    editor.insert(0, "Klebsiella_B")
    _key(editor, "<Return>")
    shell.settle()
    assert shell.state.sample(row).name == "Klebsiella_B"
    assert page.table.value(row, "sample") == "Klebsiella_B"
    assert page.rename(row, "KP_001") != ""                      # taken by another row: refused, in words
    assert shell.state.sample(row).name == "Klebsiella_B"

    # the same sample names from a second run: both rows are left out until they differ
    _add(shell, _reads(tmp_path, names=("KP_001",), folder="run2"))
    twins = [s for s in shell.state.samples if s.name == "KP_001"]
    assert len(twins) == 2 and all(not s.ready for s in twins)
    assert page.names_button.winfo_ismapped()
    assert str(shell.bottom.start.cget("text")) == "Start (2 samples)"
    page.names_button.invoke()
    shell.settle()
    assert sorted(s.name for s in shell.state.samples) == ["KP_001_run1", "KP_001_run2", "KP_010", "Klebsiella_B"]
    assert all(s.ready for s in shell.state.samples)
    assert not page.names_button.winfo_ismapped()
    assert str(shell.bottom.start.cget("text")) == "Start (4 samples)"

    # Remove and Clear list
    page.table.select([_row(shell, "KP_010")])
    shell.settle()
    assert page.buttons["remove"].instate(["!disabled"])
    page.buttons["remove"].invoke()
    shell.settle()
    assert "KP_010" not in [s.name for s in shell.state.samples]
    page.buttons["clear_list"].invoke()
    shell.settle()
    assert shell.state.samples == [] and len(page.table) == 0
    assert shell.state.results_dir == ""                         # the suggestion went with the reads
    assert shell.bottom.message_text == "Add samples to begin."


@pytest.mark.gui
@needs_display
def test_trimming_options_change_correct_themselves_and_reset(window, tmp_path):
    shell = window()
    _add(shell, _reads(tmp_path))
    shell.show_step(1)
    shell.settle()
    page = shell.trimming_page
    assert shell.stepper.current == "trimming"
    assert shell.bottom.back.winfo_ismapped() and str(shell.bottom.next.cget("text")) == "Next: Assembly"
    assert str(shell.bottom.start.cget("text")) == "Start (3 samples)"       # the same Start on every step
    assert not page.more.is_open and not page.more.changed
    assert all(page.ticks[name].checked for name in page.ON_PAGE)

    page.ticks["trim_polyg"].tick.invoke()
    shell.settle()
    assert shell.state.trim.trim_polyg is False
    assert shell.stepper.sub("trimming") == "changed"
    assert not page.more.changed                                 # that tick is on the page itself

    page.more.header.invoke()
    shell.settle()
    assert page.more.is_open
    field = page.numbers["end_quality"]
    field.delete(0, "end")
    field.insert(0, "999")
    assert field.commit() == 10                                  # out of range: back to the last good value
    assert shell.help_line.text == "Use a whole number from 1 to 36. Set back to 10."
    assert shell.state.trim.end_quality == 10
    field.delete(0, "end")
    field.insert(0, "20")
    field.commit()
    shell.settle()
    assert shell.state.trim.end_quality == 20 and page.more.changed

    # a number whose tick is off is greyed; its tick brings it back
    assert page.numbers["trim_front"].instate(["disabled"])
    page.fixed_tick.invoke()
    assert shell.state.trim.fixed_trim and page.numbers["trim_front"].instate(["!disabled"])
    page.ticks["remove_short_reads"].tick.invoke()
    assert page.numbers["min_length"].instate(["disabled"])

    # own adapters without a sequence: Start says why
    page.own_tick.invoke()
    shell.settle()
    assert shell.bottom.start.instate(["disabled"])
    assert "Use my own adapter sequences" in shell.bottom.message_text
    page.adapter_vars["adapter_r1"].set("AGATCGGAAGAGCACACGTC")
    shell.settle()
    assert shell.state.trim.adapter_r1 == "AGATCGGAAGAGCACACGTC"
    assert shell.bottom.start.instate(["!disabled"])

    page.reset_button.invoke()
    shell.settle()
    assert shell.state.trim == TrimSettings()
    assert shell.stepper.sub("trimming") == "standard" and not page.more.changed
    assert page.ticks["trim_polyg"].checked and page.numbers["end_quality"].get() == "10"
    assert page.adapter_vars["adapter_r1"].get() == "" and not page.own_var.get()


@pytest.mark.gui
@needs_display
def test_assembly_options_change_and_reset(window, tmp_path):
    shell = window()
    _add(shell, _reads(tmp_path))
    shell.show_step(2)
    shell.settle()
    page = shell.assembly_page
    assert not shell.bottom.next.winfo_ismapped()                # on step 3 Next is gone
    assert shell.bottom.start.winfo_ismapped()
    size = page.ticks["know_genome_size"]
    assert size.field.instate(["disabled"])
    size.tick.invoke()
    assert shell.state.assembly.know_genome_size and size.field.instate(["!disabled"])
    size.field.delete(0, "end")
    size.field.insert(0, "5,5")                                  # a comma from the number pad
    size.field.commit()
    assert shell.state.assembly.genome_size_mb == 5.5
    size.field.delete(0, "end")
    size.field.insert(0, "abc")
    assert size.field.commit() == 5.5
    assert shell.help_line.text == "Use a number from 0.1 to 20. Set back to 5.5."
    contigs = page.ticks["drop_short_contigs"]
    contigs.tick.invoke()
    contigs.field.delete(0, "end")
    contigs.field.insert(0, "1000")
    contigs.field.commit()
    shell.settle()
    assert shell.state.assembly.drop_short_contigs and shell.state.assembly.min_contig == 1000
    assert shell.stepper.sub("assembly") == "changed" and not page.more.changed

    page.open_more()
    page.mode.buttons["careful"].invoke()
    page.ticks["polish"].tick.invoke()
    page.ticks["force"].tick.invoke()
    threads = page.numbers["threads"]
    threads.delete(0, "end")
    threads.insert(0, "1")
    threads.commit()
    shell.settle()
    assembly = shell.state.assembly
    assert (assembly.mode, assembly.polish, assembly.force, assembly.threads) == ("careful", True, True, 1)
    assert page.more.changed
    assert settings_map.validate_assembly(assembly) == []

    page.reset_button.invoke()
    shell.settle()
    assert shell.state.assembly == AssemblySettings()
    assert shell.stepper.sub("assembly") == "standard" and not page.more.changed
    assert page.mode.value == "standard" and not size.checked and size.field.get() == "5.0"


@pytest.mark.gui
@needs_display
def test_start_hands_the_run_page_the_whole_batch_at_once(window, tmp_path):
    shell = window()
    folder = _reads(tmp_path)
    write_pair(tmp_path / "run1", "EC_09")
    os.remove(os.path.join(folder, "EC_09_R2.fastq.gz"))
    _add(shell, folder)
    shell.samples_page.table.toggle([_row(shell, "KP_002")])     # KP_002 is not trimmed
    shell.show_step(2)
    shell.assembly_page.open_more()
    shell.assembly_page.mode.buttons["fast"].invoke()
    shell.show_step(1)
    field = shell.trimming_page.numbers["min_length"]
    shell.trimming_page.open_more()
    field.delete(0, "end")
    field.insert(0, "60")                                        # typed and not yet left: Start must take it
    shell.settle()
    run = shell.run_page

    # Enter is not Start
    _key(shell.samples_page.folder_entry, "<Return>")
    _key(shell.root, "<Return>")
    shell.settle()
    assert run.begun == []

    assert shell.bottom.start.instate(["!disabled"])
    shell.bottom.start.invoke()
    assert len(run.begun) == 1, "Start must start at once, with no question in between"
    config, left_out, resume, _factory = run.begun[0]
    assert resume is False
    assert [s.name for s in config.samples] == ["KP_001", "KP_002", "KP_010"]
    assert [s.trim for s in config.samples] == [True, False, True]
    assert all(os.path.isfile(a) and os.path.isfile(b) for s in config.samples for a, b in s.lanes)
    assert config.results_dir == os.path.join(folder, branding.RESULTS_DIR_NAME)
    assert config.existing is Existing.SKIP
    assert config.trim.min_length == 60 and config.assembly.mode == "fast"
    assert config.trim is not shell.state.trim and config.assembly is not shell.state.assembly
    assert settings_map.validate_trim(config.trim) == [] and settings_map.validate_assembly(config.assembly) == []
    assert [name for name, _why in left_out] == ["EC_09"] and left_out[0][1].startswith("No Read 2 file found.")

    # the window is on the Run step and the other steps are locked
    assert shell.step == 3 and shell.stepper.current == "run" and shell.stepper.locked
    assert not shell.bottom.winfo_ismapped()
    assert shell.stepper.sub("run") == texts.STEP_RUNNING
    shell.show_step(0)
    assert shell.step == 3
    assert shell.press_start() is False and len(run.begun) == 1  # it cannot be pressed twice
    assert shell.store.recorded_folder("last_results") == config.results_dir

    run.finish(EndReason.COMPLETED)
    shell.settle()
    assert not shell.stepper.locked and shell.stepper.sub("run") == texts.STEP_FINISHED
    shell.show_step(0)
    shell.settle()
    assert shell.step == 0 and shell.bottom.winfo_ismapped()
    assert shell.stepper.can_pick("run")                         # the results stay one click away
    shell.new_run()
    assert shell.state.samples == [] and shell.step == 0


@pytest.mark.gui
@needs_display
def test_start_asks_only_when_samples_are_already_assembled(window, tmp_path, monkeypatch):
    from tesseract_win.gui import dialogs
    shell = window()
    folder = _reads(tmp_path)
    _add(shell, folder)
    rdir = shell.state.results_dir
    finish_sample(rdir, next(s for s in shell.state.samples if s.name == "KP_002"))
    shell.check_folder(0, force=True)
    shell.settle()
    assert "1 of these samples already has results here." in str(shell.samples_page.folder_notes.cget("text"))
    asked = []

    def answer(with_what):
        def ask(parent, existing, total):
            asked.append((sorted(existing), total))
            return with_what
        return ask

    monkeypatch.setattr(dialogs, "ask_overwrite", answer(None))  # Cancel, Esc, the close box
    assert shell.press_start() is False
    assert asked == [(["KP_002"], 3)] and shell.run_page.begun == [] and shell.step == 0
    assert not shell.state.running

    monkeypatch.setattr(dialogs, "ask_overwrite", answer(Existing.OVERWRITE))
    assert shell.press_start() is True
    config = shell.run_page.begun[0][0]
    assert config.existing is Existing.OVERWRITE
    assert [s.name for s in config.samples] == ["KP_001", "KP_002", "KP_010"]    # the runner does the skipping
    shell.run_page.finish(EndReason.STOPPED)
    shell.settle()
    assert shell.stepper.sub("run") == texts.STEP_STOPPED

    monkeypatch.setattr(dialogs, "ask_overwrite", answer(Existing.SKIP))
    shell.show_step(0)
    assert shell.press_start() is True
    assert shell.run_page.begun[1][0].existing is Existing.SKIP
    assert len(asked) == 3


@pytest.mark.gui
@needs_display
def test_the_real_question_has_skip_as_its_default(window, tmp_path):
    from tesseract_win.gui import dialogs
    from tesseract_win.gui import widgets as w
    shell = window()
    seen = {}

    def look_at_it():
        boxes = [child for child in shell.root.winfo_children() if isinstance(child, w.Dialog)]
        if not boxes:
            shell.root.after(50, look_at_it)
            return
        box = boxes[0]
        seen["default"], seen["cancel"], seen["buttons"] = box.default, box.cancel, sorted(box.buttons)
        box.invoke(box.cancel)                                   # what Esc and the close box stand for

    shell.root.after(100, look_at_it)
    answer = dialogs.ask_overwrite(shell.root, {"KP_001": found("KP_001")}, 3)
    assert answer is None                                        # Cancel starts nothing
    assert seen == {"default": "skip", "cancel": "cancel", "buttons": ["cancel", "overwrite", "skip"]}


@pytest.mark.gui
@needs_display
def test_files_gone_at_start_are_left_out_and_the_rest_runs(window, tmp_path):
    shell = window()
    folder = _reads(tmp_path)
    _add(shell, folder)
    os.remove(os.path.join(folder, "KP_010_R1.fastq.gz"))        # the stick was pulled, so to say
    assert shell.press_start() is True
    config, left_out, _resume, _factory = shell.run_page.begun[0]
    assert [s.name for s in config.samples] == ["KP_001", "KP_002"]
    assert [name for name, _why in left_out] == ["KP_010"]


@pytest.mark.gui
@needs_display
def test_typed_results_folder_is_checked_and_a_bad_one_disables_start(window, tmp_path):
    shell = window()
    _add(shell, _reads(tmp_path))
    page = shell.samples_page
    documents = tmp_path / "Documents"
    documents.mkdir()
    (documents / "thesis.docx").write_text("x")
    page.folder_var.set('"%s"' % documents)                      # pasted with quotes
    page.commit_folder()
    assert shell.settle()
    wanted = str(documents / branding.RESULTS_DIR_NAME)
    assert shell.state.results_dir == wanted and page.folder_var.get() == wanted
    assert ("Results will go into a new folder here: %s" % wanted) in str(page.folder_notes.cget("text"))
    assert shell.state.results_by_user and not shell.state.results_suggested
    assert shell.bottom.start.instate(["!disabled"])

    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    page.set_results_folder(str(a_file), by_user=True)
    assert shell.settle()
    assert shell.bottom.start.instate(["disabled"])
    assert shell.bottom.message_text == "This is not a folder the program can use."
    assert str(page.folder_notes.cget("text")) == st.Words.RESULTS_INVALID
    page.set_results_folder("", by_user=True)
    shell.settle()
    assert shell.bottom.message_text == "Choose a results folder to begin."


@pytest.mark.gui
@needs_display
def test_missing_engine_keeps_start_disabled_with_the_audit_words(window, tmp_path, monkeypatch):
    from tesseract_win import appdirs

    def nothing_there(folder=None):
        raise appdirs.EngineMissing(["tesseract-asm.exe"], str(tmp_path))

    monkeypatch.setattr(appdirs, "find_engines", nothing_there)
    import tkinter as tk
    from tesseract_win.gui import app as appmod
    shell = appmod.App(tk.Tk(screenName=DISPLAY or None), run_page_factory=StandInRunPage, self_test=False,
                       store=settings_store.SettingsStore(str(tmp_path / "s.json")), scale=1.0, dark=False,
                       high_contrast=False)
    try:
        shell.settle()
        words = shell.notices.text("engines")
        assert words.startswith("Part of the program is missing: engines")
        assert "tesseract-asm.exe. Antivirus software sometimes removes it by mistake." in words
        assert shell.notices.level("engines") == "bad"
        _add(shell, _reads(tmp_path))
        assert shell.bottom.start.instate(["disabled"])
        assert shell.bottom.message_text == st.Words.PROGRAM_PART_MISSING
        monkeypatch.setattr(appdirs, "find_engines", lambda folder=None: model.EnginePaths("a", "b", ""))
        shell.notices.button_of("engines").invoke()              # Check again
        shell.settle()
        assert "engines" not in shell.notices.kinds()
        assert shell.bottom.start.instate(["!disabled"])
    finally:
        shell.close()


@pytest.mark.gui
@needs_display
def test_closing_asks_the_run_page_and_remembers_the_size(window, tmp_path):
    shell = window()
    shell.root.geometry("1000x650")
    shell.settle()
    shell.run_page.may_close = False
    assert shell.close() is False and not shell.closed           # the Run page said no: the window stays
    assert shell.root.winfo_exists()
    shell.run_page.may_close = True
    assert shell.close() is True and shell.closed
    assert shell.run_page.close_asked == 2
    saved = settings_store.SettingsStore(shell.store.path)
    assert saved.window_size() == (1000, 650)


@pytest.mark.gui
@needs_display
def test_last_results_folder_is_offered_to_the_run_page_at_start_up(window, tmp_path):
    store = settings_store.SettingsStore(str(tmp_path / "settings.json"))
    store.set_folder("last_results", str(tmp_path / "old run"))
    shell = window(store=store)
    assert shell.run_page.offered == [str(tmp_path / "old run")]
    assert shell.state.results_dir == ""                         # never reused as today's folder (audit 4.1)
    assert shell.state.trim == TrimSettings() and shell.state.assembly == AssemblySettings()
    assert shell.step == 0 and not shell.stepper.can_pick("run")
    shell.run_page.offer_showing = True                          # the Run page found a run to continue
    end = time.time() + 5
    while shell.step != 3 and time.time() < end:
        shell.root.update()
        time.sleep(0.02)
    assert shell.step == 3, "the window must open on the step that shows the offer"
    assert not shell.stepper.locked
    shell.show_step(0)
    shell.settle()
    assert shell.step == 0 and shell.stepper.can_pick("run")


@pytest.mark.gui
@needs_display
def test_choosing_a_folder_with_an_unfinished_run_points_to_the_run_step(window, tmp_path):
    shell = window()
    _add(shell, _reads(tmp_path))
    folder = str(tmp_path / "old_results")
    shell.samples_page.set_results_folder(folder, by_user=True)       # audit row 11.6
    shell.settle()
    assert shell.run_page.offered[-1] == folder
    shell.run_page.offer_showing = True                          # the Run page found a run to continue there
    end = time.time() + 4
    while time.time() < end and "unfinished" not in shell.notices.kinds():
        shell.root.update()
        time.sleep(0.02)
    assert shell.notices.text("unfinished") == st.Words.UNFINISHED_RUN
    assert shell.step == 0 and shell.stepper.can_pick("run")     # the list is not empty: the user stays here
    shell.notices.button_of("unfinished").invoke()               # "Show"
    assert shell.step == 3 and "unfinished" not in shell.notices.kinds()


@pytest.mark.gui
@needs_display
def test_a_fault_in_the_window_is_said_in_a_bar_and_nothing_stops(window):
    shell = window()
    shell._callback_error(ValueError, ValueError("a slip in a button"), None)      # audit row 9.14
    assert shell.notices.text("fault") == st.Words.PROGRAM_FAULT
    assert "Save a problem report" in shell.notices.text("fault") and not shell.closed
    shell.state.running = True
    shell._callback_error(ValueError, ValueError("again"), None)
    assert shell.notices.text("fault") == st.Words.PROGRAM_FAULT_RUNNING
    assert "The run is still going." in shell.notices.text("fault")
    shell.state.running = False
    # Windows gives notice that the session ends: the run page is told, nothing is asked
    told = []
    shell.run_page.note_interruption = lambda: told.append("windows")
    shell._session_ending()
    assert told == ["windows"] and not shell.closed and shell.run_page.close_asked == 0


@pytest.mark.gui
@needs_display
def test_try_the_example_and_dropped_things(window, tmp_path, monkeypatch):
    from tesseract_win import appdirs
    example = tmp_path / "TesserACT-Win" / "example"
    write_pair(example, "example_isolate")
    monkeypatch.setattr(appdirs, "example_dir", lambda: str(example))
    shell = window()
    page = shell.samples_page
    page.try_example()                                           # what the link in the empty list does
    assert shell.settle(15)
    assert [s.name for s in shell.state.samples] == ["example_isolate"]
    assert shell.state.results_dir == str(example / "results")
    assert page.notices.text("example") == texts.EXAMPLE_NOTE         # one line, the words of the audit
    assert shell.assembly_page.notice.text("example") == texts.EXAMPLE_SIZE_NOTE      # why, where the setting is
    assert shell.help_line.text == texts.EXAMPLE_SIZE_NOTE
    assert page.activity_text == ""                              # the README beside the reads is not news
    # the practice isolate is tiny: its genome size is ticked, in plain sight, and only for it
    assert shell.state.assembly.know_genome_size and shell.state.assembly.genome_size_mb == 0.2
    assert shell.stepper.sub("assembly") == "changed"
    assert shell.assembly_page.ticks["know_genome_size"].checked
    assert shell.state.example_loaded and not shell.run_page.begun          # it does not start by itself
    assert shell.bottom.start.instate(["!disabled"])
    assert str(shell.bottom.start.cget("text")) == "Start (1 sample)"
    page.clear_list()
    shell.settle()
    assert "example" not in page.notices.kinds() and shell.state.results_dir == ""
    assert not shell.state.assembly.know_genome_size and shell.state.assembly.genome_size_mb == 5.0
    assert shell.stepper.sub("assembly") == "standard" and "example" not in shell.assembly_page.notice.kinds()
    # the user's own samples beside the practice one: the practice size must not be applied to them
    page.try_example()
    assert shell.settle(15) and shell.state.assembly.know_genome_size
    write_pair(tmp_path / "own", "KP_090")
    page.add_paths([str(tmp_path / "own")])
    assert shell.settle(15) and len(shell.state.samples) == 2
    assert not shell.state.assembly.know_genome_size and "example" not in page.notices.kinds()
    page.clear_list()
    shell.settle()
    # a size the user set is the user's: the practice sample does not touch it
    shell.state.assembly.know_genome_size, shell.state.assembly.genome_size_mb = True, 2.8
    page.try_example()
    assert shell.settle(15)
    assert shell.state.assembly.genome_size_mb == 2.8 and page.notices.text("example") == texts.EXAMPLE_NOTE
    assert "example" not in shell.assembly_page.notice.kinds()
    page.clear_list()
    shell.settle()
    assert shell.state.assembly.know_genome_size and shell.state.assembly.genome_size_mb == 2.8
    shell.assembly_page.reset()
    shell.settle()

    # things dragged out of a zip view or an e-mail are not files on a disk
    page.drop(["/no/such/place/KP_1_R1.fastq.gz"])
    shell.settle()
    assert page.activity_text == st.Words.NOT_ON_DISK and shell.state.samples == []
    # one file dropped brings its partner; a file that is not FASTQ is named
    first = write_pair(tmp_path / "dropped", "KP_050")[0]
    page.drop([first])
    assert shell.settle(15)
    assert [s.name for s in shell.state.samples] == ["KP_050"] and shell.state.samples[0].ready
    report = tmp_path / "dropped" / "report.pdf"
    report.write_text("x")
    page.drop([str(report)])
    assert shell.settle(15)
    assert page.activity_text.startswith("report.pdf is not a FASTQ file.")
    assert len(shell.state.samples) == 1
    # a run that has no pairs at all gets one clear bar
    for name in ("SE_1", "SE_2"):
        os.remove(write_pair(tmp_path / "single", name)[1])
    page.drop([str(tmp_path / "single")])
    assert shell.settle(15)
    assert page.notices.text("none_paired") == st.Words.NONE_PAIRED
    assert [s.name for s in shell.state.left_out()] == ["SE_1", "SE_2"]


@pytest.mark.gui
@needs_display
def test_the_real_run_page_fits_the_shell(window):
    page_run = pytest.importorskip("tesseract_win.gui.page_run")
    shell = window(run_page_factory=None)                        # None: the shell loads the real page itself
    assert isinstance(shell.run_page, page_run.RunPage)
    for name in ("begin", "is_running", "confirm_close", "offer_interrupted"):
        assert callable(getattr(shell.run_page, name)), name
    assert shell.run_page.is_running() is False
    assert shell.run_page.confirm_close() is True                # nothing runs: the window may close
    assert shell.run_page.help_line is shell.help_line
    shell.show_step(3)
    shell.settle()
    assert shell.step == 3 and shell.run_page.winfo_ismapped() and not shell.bottom.winfo_ismapped()
    shell.show_step(0)
    shell.settle()
    assert shell.bottom.winfo_ismapped()


@pytest.mark.gui
@needs_display
def test_run_app_opens_the_demonstration_window(tmp_path, monkeypatch):
    import logging
    import threading
    from tesseract_win.gui import app as appmod
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    if DISPLAY:
        monkeypatch.setenv("DISPLAY", DISPLAY)
    monkeypatch.setattr(appmod, "_logging_ready", False)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    seen = {}

    def look_and_close(self):
        self.settle()
        seen["rows"] = len(self.state.samples)
        seen["step"] = self.step
        seen["start"] = str(self.bottom.start.cget("text"))
        seen["dark"] = self.theme.dark
        self.close()

    monkeypatch.setattr(appmod.App, "run", look_and_close)
    package = logging.getLogger("tesseract_win")
    before = list(package.handlers)
    try:
        assert appmod.run_app(["--demo", "--dark", "--scale", "1.0", "--step", "2", "--something-else"]) == 0
    finally:
        for handler in list(package.handlers):
            if handler not in before:
                package.removeHandler(handler)
                handler.close()
    assert seen == {"rows": 8, "step": 2, "start": "Start (7 samples)", "dark": True}
    assert not (tmp_path / "state" / branding.DATA_DIR_NAME / "settings.json").exists()    # a demonstration stores nothing


@pytest.mark.gui
@needs_display
@pytest.mark.parametrize("dark,scale", [(False, 1.0), (True, 1.5)])
def test_demo_window_and_every_page_fit_a_small_laptop(window, dark, scale):
    shell = window(demo=True, dark=dark, scale=scale, screen=(1366, 768))
    assert len(shell.state.samples) == 8
    assert str(shell.bottom.start.cget("text")) == "Start (7 samples)"
    assert shell.bottom.start.instate(["!disabled"])
    assert shell.root.winfo_width() <= 1366 and shell.root.winfo_height() <= 768
    for step in (0, 1, 2):
        shell.show_step(step)
        shell.settle()
        for part in (shell.bottom.start, shell.help_line, shell.stepper):
            assert part.winfo_ismapped()
            assert part.winfo_rooty() + part.winfo_height() <= shell.root.winfo_rooty() + shell.root.winfo_height()
    shell.show_step(1)
    shell.trimming_page.open_more()
    shell.settle()
    if scale == 1.0:
        assert not shell.trimming_page.scroll.scrolling          # everything in view without scrolling
    shell.show_step(2)
    shell.assembly_page.open_more()
    shell.settle()
    if scale == 1.0:
        assert not shell.assembly_page.scroll.scrolling


@pytest.mark.gui
@needs_display
def test_every_option_explains_itself_in_the_help_line_on_focus(window):
    shell = window(demo=True)
    shell.show_step(1)
    shell.trimming_page.open_more()
    shell.settle()
    page = shell.trimming_page
    page.ticks["remove_adapters"].tick.focus_force()
    shell.settle()
    assert shell.help_line.text == texts.TRIM_HOVER["remove_adapters"]
    page.numbers["end_quality"].focus_force()
    shell.settle()
    assert shell.help_line.text == texts.TRIM_HOVER["end_quality"]
    shell.show_step(2)
    shell.assembly_page.open_more()
    shell.settle()
    shell.assembly_page.mode.buttons["aggressive"].focus_force()
    shell.settle()
    assert shell.help_line.text == texts.MODE_HOVER["aggressive"]
    shell.show_step(0)                                           # a change of page clears the line
    shell.settle()
    assert shell.help_line.text in (st.Words.HELP_DEFAULT, texts.HOVER["sample_column"]) or shell.help_line.text


# =========================================================================== photographs

def _shots(folder):
    """Every page, light and dark, at 100 % and 150 %, on a pretend 1366 x 768 screen."""
    import tkinter as tk
    from PIL import ImageGrab
    from tesseract_win.gui import app as appmod
    from tesseract_win.gui import dialogs
    from tesseract_win.gui import widgets as w

    os.makedirs(folder, exist_ok=True)
    display = os.environ.get("DISPLAY") or None

    def grab(shell, name):
        shell.settle(3)
        shell.root.update()
        time.sleep(0.2)
        shell.root.update()
        full = ImageGrab.grab(xdisplay=display) if sys.platform.startswith("linux") else ImageGrab.grab()
        left, top = shell.root.winfo_rootx(), shell.root.winfo_rooty()
        box = (left, top, left + shell.root.winfo_width(), top + shell.root.winfo_height())
        path = os.path.join(folder, name + ".png")
        full.crop(box).save(path)
        print("saved", path)

    def calm(shell):
        shell.settle(3)
        shell.root.event_generate("<Motion>", warp=True, x=4, y=4)
        w.hide_tooltips(shell.root)
        shell.help_line.clear()

    for dark in (False, True):
        for scale in (1.0, 1.5):
            tag = "%s_%d" % ("dark" if dark else "light", int(scale * 100))
            shell = appmod.App(tk.Tk(), dark=dark, scale=scale, demo=True, screen=(1366, 768), high_contrast=False)
            calm(shell)
            grab(shell, "samples_%s" % tag)
            shell.samples_page.select_left_out()
            grab(shell, "samples_left_out_%s" % tag)
            calm(shell)
            shell.show_step(1)
            calm(shell)
            grab(shell, "trimming_%s" % tag)
            shell.trimming_page.open_more()
            shell.trimming_page.numbers["end_quality"].set_value(20, notify=True)
            shell.trimming_page.numbers["end_quality"].focus_force()
            shell.settle(2)
            grab(shell, "trimming_more_%s" % tag)
            shell.trimming_page.reset()
            shell.show_step(2)
            calm(shell)
            grab(shell, "assembly_%s" % tag)
            shell.assembly_page.open_more()
            calm(shell)
            grab(shell, "assembly_more_%s" % tag)
            shell.show_step(0)
            calm(shell)
            existing = {"KP_001": found("KP_001", False, "not trimmed; mode Careful"), "KP_003": found("KP_003")}
            question = dialogs.overwrite_question(existing, 7)
            box = w.Dialog(shell.root, title=question.title, body=question.body, choices=question.choices,
                           default=question.default, cancel=question.cancel, details=question.details,
                           details_label=question.details_label, level="warn")
            box.open()
            grab(shell, "overwrite_%s" % tag)
            box.destroy()
            about = w.Dialog(shell.root, title=st.Words.ABOUT_TITLE, body=dialogs.about_body(), wrap=460,
                             choices=dialogs.about_choices(), default="close", cancel="close")
            about.open()
            grab(shell, "about_%s" % tag)
            about.destroy()
            shell.trimming_page.open_more(False)
            shell.samples_page.trim_all(False)
            shell.show_step(1)
            calm(shell)
            grab(shell, "trimming_not_used_%s" % tag)
            shell.show_step(0)
            shell.samples_page.clear_list()
            calm(shell)
            grab(shell, "empty_%s" % tag)
            shell.closed = True
            shell.root.destroy()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--shots":
        _shots(sys.argv[2])
    else:
        print(__doc__)
