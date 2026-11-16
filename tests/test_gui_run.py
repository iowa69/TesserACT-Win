# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Tests of the Run page, its questions and the stand-in runner (gui/page_run, run_dialogs, demo).

The first half needs no display: the words built for each state, the time-left arithmetic,
which buttons apply when, the bar for an interrupted run, and the stand-in played on its own.
The second half carries the ``gui`` marker and opens real windows: every scripted batch is
played at high speed and the final state of every row, the buttons shown and the stop
requests received by the stand-in are checked.

Which display: ``TESSERACT_WIN_TEST_DISPLAY`` if set; otherwise, on Linux, the headless
display ``:99`` when it exists; otherwise ``DISPLAY``.

    python -m pytest tests/test_gui_run.py -q
"""

from __future__ import annotations

import gc
import os
import queue
import subprocess
import sys
import time

import pytest

from tesseract_win import model, osapi
from tesseract_win.gui import demo, page_run, run_dialogs, texts
from tesseract_win.model import EndReason, RunStatus, Stage, StopMode

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI_DIR = os.path.join(REPO, "tesseract_win", "gui")
OWN_FILES = [os.path.join(GUI_DIR, name) for name in ("page_run.py", "run_dialogs.py", "demo.py")]
OWN_FILES.append(os.path.abspath(__file__))

S = RunStatus


# =========================================================================== words, no display

def test_files_start_with_the_two_header_lines():
    for path in OWN_FILES:
        with open(path, encoding="utf-8") as handle:
            first, second = handle.readline(), handle.readline()
        assert first == "# SPDX-License-Identifier: MIT\n", path
        assert second == "# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin\n", path


def test_modules_import_without_a_display_and_without_messagebox_or_batch():
    code = ("import sys; import tesseract_win.gui.page_run, tesseract_win.gui.run_dialogs, "
            "tesseract_win.gui.demo; "
            "bad = [m for m in ('tkinter.messagebox', 'PIL', 'tesseract_win.batch') if m in sys.modules]; "
            "sys.exit(1 if bad else 0)")
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env)
    assert done.returncode == 0


def test_time_left_counts_down_between_estimates_and_never_below_zero():
    assert page_run.time_left_now(None, 100.0, 500.0) is None
    assert page_run.time_left_now(3600.0, 100.0, 100.0) == 3600.0
    assert page_run.time_left_now(3600.0, 100.0, 700.0) == 3000.0
    assert page_run.time_left_now(3600.0, 100.0, 99999.0) == 0.0
    assert page_run.time_left_now(3600.0, 100.0, 50.0) == 3600.0      # a clock that went back


def test_clock_line_before_and_after_the_first_sample():
    before = page_run.clock_line(401, 401, None)
    assert before == "This sample 06:41. Whole run 06:41. " + texts.ESTIMATE_PENDING
    after = page_run.clock_line(401, 2 * 3600 + 14 * 60, 4 * 3600)
    assert after == "This sample 06:41. Whole run 2 h 14 min. About 4 hours left (rough estimate)."
    assert page_run.clock_line(None, 30, 20).startswith("This sample 00:00. Whole run 00:30. Less than a minute")


def test_header_counts():
    assert page_run.tally_text(34, 2) == "34 done, 2 failed."
    assert page_run.position_text(37, 120) == "Sample 37 of 120."
    assert texts.sample_header(13, 40, "KP_013") == "Sample 13 of 40: KP_013"


def test_took_words():
    assert page_run.took_words(20) == "less than a minute"
    assert page_run.took_words(14 * 60 + 5) == "14 min"
    assert page_run.took_words(9 * 3600 + 14 * 60) == "9 h 14 min"


def test_split_message_gives_heading_and_body():
    head, rest = page_run.split_message(
        "Stopped: the results folder can no longer be reached (E:\\Results). Reconnect the drive, "
        "then press Continue. The 12 finished samples are safe.")
    assert head == "Stopped: the results folder can no longer be reached (E:\\Results)."
    assert rest == "Reconnect the drive, then press Continue. The 12 finished samples are safe."
    assert page_run.split_message("Finished") == ("Finished", "")
    assert page_run.split_message("") == ("", "")


def test_strip_counts_takes_out_only_the_sentences_that_count_samples():
    strip = page_run.strip_counts
    assert strip("Finished at 03:42. It took 9 h 14 min. 36 done, 2 done (check notes), 2 failed.") == (
        "Finished at 03:42. It took 9 h 14 min.")
    assert strip("Stopped. 12 finished, 1 stopped, 27 not started. Nothing that was finished has been lost.") == (
        "Stopped. Nothing that was finished has been lost.")
    assert strip("Finished at 03:42. It took 2 min. 1,204 done, 12 skipped (already assembled), 3 left out.") == (
        "Finished at 03:42. It took 2 min.")
    for kept in ("Finished at 03:42. It took 9 h 14 min. No sample was run.",
                 "Stopped: drive D: is full. Free some space, then press Continue. The 12 finished samples are safe.",
                 "Stopped: the results folder can no longer be reached (E:\\Results). Reconnect the drive, then "
                 "press Continue.", "", "3 done"[:0]):
        assert strip(kept) == kept
    assert strip("3 done.") == ""


def test_end_message_uses_the_runners_line_or_makes_one():
    assert page_run.end_message(EndReason.COMPLETED, "All done.", 10, "03:42") == "All done."
    made = page_run.end_message(EndReason.COMPLETED, "", 9 * 3600 + 14 * 60, "03:42")
    assert made == "Finished at 03:42. It took 9 h 14 min."
    stopped = page_run.end_message(EndReason.STOPPED, "  ", 60, "03:42")
    assert stopped == "Stopped at 03:42. " + texts.NOTHING_LOST
    assert texts.NOTHING_LOST in page_run.end_message(EndReason.ENVIRONMENT, "", 60, "03:42")


def test_end_level():
    assert page_run.end_level(EndReason.COMPLETED, {"done": 3}) == "ok"
    assert page_run.end_level(EndReason.COMPLETED, {"done": 3, "failed": 1}) == "warn"
    assert page_run.end_level(EndReason.STOPPED, {"done": 3, "failed": 1}) == "info"
    assert page_run.end_level(EndReason.ENVIRONMENT, {"done": 3}) == "bad"


def test_every_status_is_counted_in_a_fixed_order():
    counts = page_run.tally([S.DONE] * 36 + [S.DONE_NOTES] * 2 + [S.FAILED] * 2 + [S.SKIPPED] * 12
                            + [S.STOPPED] + [S.NOT_STARTED] * 27 + [S.LEFT_OUT] * 2)
    words = [text for _status, text in page_run.count_words(counts)]
    assert words == ["36 done", "2 done (check notes)", "2 failed", "12 skipped (already assembled)",
                     "1 stopped", "27 not started", "2 left out"]
    assert page_run.count_words({"done": 0, "failed": 0}) == []
    # a status outside the usual list is still accounted for
    assert [t for _s, t in page_run.count_words({"waiting": 1})] == ["1 waiting"]


def test_where_text_names_the_assemblies_folder_and_the_number_of_files():
    folder = os.path.join("D:", "Results")
    text = page_run.where_text(folder, 38)
    assert os.path.join(folder, "assemblies") in text
    assert "(38 files, one per sample)" in text and "run_summary.html" in text
    assert "(1 file, one per sample)" in page_run.where_text(folder, 1)
    assert page_run.where_text(folder, 0) == page_run.WHERE_NONE.format(folder=folder)


def test_failed_hint_and_retry_label():
    assert page_run.failed_hint(0) == ""
    assert page_run.failed_hint(1).startswith("1 sample failed.")
    assert page_run.failed_hint(2) == "2 samples failed. Select a row to see why and what to do."
    assert page_run.retry_label(1) == "Try the failed sample again"
    assert page_run.retry_label(2) == "Try the 2 failed samples again"


def test_title_bar_text():
    done = {"done": 36, "done_notes": 2, "failed": 2, "left_out": 3}
    assert page_run.title_text(EndReason.COMPLETED, done) == "Finished: 38 done, 2 failed"
    assert page_run.title_text(EndReason.COMPLETED, {"done": 40}) == "Finished: 40 done"
    stopped = {"done": 12, "stopped": 1, "not_started": 27, "left_out": 2}
    assert page_run.title_text(EndReason.STOPPED, stopped) == "Stopped: 12 of 40 done"
    assert page_run.title_text(EndReason.ENVIRONMENT, dict(stopped, failed=1)) == "Stopped: 12 of 41 done, 1 failed"


def test_which_buttons_the_end_carries():
    actions = page_run.end_actions
    assert actions(EndReason.COMPLETED, {"done": 8}) == ["new_run", "back_to_samples"]
    assert actions(EndReason.COMPLETED, {"done": 7, "failed": 1}) == ["retry_failed", "new_run", "back_to_samples"]
    assert actions(EndReason.STOPPED, {"done": 3, "stopped": 1, "not_started": 4}) == [
        "continue", "new_run", "back_to_samples"]
    assert actions(EndReason.ENVIRONMENT, {"done": 3, "failed": 1, "not_started": 4}) == [
        "continue", "retry_failed", "new_run", "back_to_samples"]
    # a batch stopped after its last sample has nothing to continue
    assert "continue" not in actions(EndReason.STOPPED, {"done": 8})
    assert actions(EndReason.COMPLETED, {"done": 8}, can_new_run=False) == ["back_to_samples"]


def test_stop_button_in_each_phase():
    assert page_run.stop_button("idle") == ("Stop", False)
    assert page_run.stop_button("starting") == ("Stop", False)
    assert page_run.stop_button("running") == ("Stop", True)
    assert page_run.stop_button("running", has_sample=False) == ("Stop", False)
    assert page_run.stop_button("stopping_after") == ("Stop now", True)
    assert page_run.stop_button("stopping_now") == ("Stop now", False)
    assert page_run.stop_button("ended") == ("Stop", False)
    assert set(page_run.PHASES) == {"idle", "starting", "running", "stopping_after", "stopping_now", "ended"}


def _outcome(**changes):
    values = dict(name="KP_001", status=S.DONE, contigs=71, total_length=5482113, n50=214880)
    values.update(changes)
    return model.SampleOutcome(**values)


def test_row_values_fill_in_when_the_outcome_arrives():
    waiting = page_run.row_values("KP_001", S.WAITING)
    assert waiting == {"sample": "KP_001", "status": "Waiting", "contigs": "", "total_length": "",
                       "n50": "", "notes": ""}
    done = page_run.row_values("KP_001", S.DONE, _outcome())
    assert (done["contigs"], done["total_length"], done["n50"]) == ("71", "5,482,113", "214,880")
    failed = page_run.row_values("KP_003", S.FAILED, model.SampleOutcome(
        name="KP_003", status=S.FAILED, note="A read file is incomplete.", next_step="Copy it again."))
    assert failed["notes"] == "A read file is incomplete. Copy it again."
    assert failed["status"] == "Failed" and failed["contigs"] == ""
    assert page_run.row_values("KP_009", S.LEFT_OUT, None, "No Read 2 file.")["notes"] == "No Read 2 file."


def test_full_note_has_the_reason_the_next_step_and_every_warning_once():
    outcome = _outcome(status=S.DONE_NOTES, note="Low depth (about 22x).",
                       notes=["Low depth (about 22x).", "Many pieces."], kept_earlier_result=True)
    assert page_run.full_note(outcome) == "Low depth (about 22x). Many pieces. " + page_run.KEPT_EARLIER
    assert page_run.full_note(None, " waiting ") == "waiting"


def test_strip_words_and_buttons_for_each_kind_of_row():
    words, buttons = page_run.strip_content("KP_001", S.DONE, _outcome())
    assert words == "KP_001: Done. 71 contigs, 5,482,113 bases in total, N50 214,880."
    assert buttons == ["open_report", "open_folder"]
    words, buttons = page_run.strip_content("KP_003", S.FAILED, model.SampleOutcome(
        name="KP_003", status=S.FAILED, note="A read file is damaged.", next_step="Copy it again."))
    assert words == "KP_003: A read file is damaged. Copy it again."
    assert buttons == ["open_log", "open_folder"]
    assert page_run.strip_content("KP_002", S.DONE_NOTES, _outcome(note="Low depth."))[1] == [
        "open_report", "open_folder"]
    assert page_run.strip_content("KP_004", S.SKIPPED)[1] == ["open_report", "open_folder"]
    for status in (S.WAITING, S.ASSEMBLING, S.STOPPED, S.NOT_STARTED, S.LEFT_OUT):
        words, buttons = page_run.strip_content("KP_005", status)
        assert buttons == [] and words == "KP_005: %s." % texts.RUN_STATUS_LABEL[status]
    assert page_run.strip_content("KP_009", S.LEFT_OUT, None, "No Read 2 file.")[0] == "KP_009: No Read 2 file."


def test_working_text_always_says_that_silence_is_normal():
    assert page_run.working_text(None) == texts.SILENCE_IS_NORMAL
    said = page_run.working_text(7.6, int(3.1 * 1024 ** 3))
    assert said == "Working: 8 processor threads busy, 3.1 GB of memory in use. " + texts.SILENCE_IS_NORMAL


def test_stage_states():
    states = page_run.stage_states
    assert states(Stage.ASSEMBLE) == {"join_lanes": "skipped", "trim": "done", "check_reads": "done",
                                      "assemble": "running", "summarise": "waiting"}
    assert states(Stage.CHECK_READS, trimmed=False, lanes=2) == {
        "join_lanes": "done", "trim": "skipped", "check_reads": "running", "assemble": "waiting",
        "summarise": "waiting"}
    assert states(Stage.CHECK_READS, final=S.FAILED)["check_reads"] == "failed"
    assert states(Stage.CHECK_READS, final=S.FAILED)["assemble"] == "waiting"
    assert set(states(Stage.SUMMARISE, final=S.DONE).values()) == {"done", "skipped"}
    assert states(Stage.ASSEMBLE, final=S.STOPPED)["assemble"] == "waiting"
    assert set(states(None).values()) == {"waiting", "skipped"}


# ------------------------------------------------------------------ the questions

def test_stop_question_names_the_sample_and_the_minutes_and_defaults_to_keep_running():
    state = run_dialogs.RunState("KP_002", S.ASSEMBLING, 14 * 60 + 20, 12)
    question = run_dialogs.stop_question(state)
    assert question.title == "Stop the run?"
    assert question.body == ("KP_002 is being assembled (14 minutes so far).\n"
                             "If you stop now, that work is thrown away and KP_002 starts from the "
                             "beginning next time.\nThe 12 samples already finished are kept.")
    assert [(c.key, c.label) for c in question.choices] == [
        ("keep", "Keep running"), ("after_current", "Stop after KP_002"), ("now", "Stop now")]
    assert question.default == "keep" and question.cancel == "keep"
    assert question.choice("now").kind == "danger" and question.choice("after_current").kind == "plain"


def test_stop_question_once_stop_after_is_pending_offers_to_wait_or_to_stop_now():
    state = run_dialogs.RunState("KP_002", S.ASSEMBLING, 14 * 60, 12)
    question = run_dialogs.stop_question(state, pending_after=True)
    assert [(c.key, c.label) for c in question.choices] == [("keep", "Wait for KP_002"), ("now", "Stop now")]
    assert question.default == "keep" and question.cancel == "keep"
    assert question.body.startswith("The run already stops when KP_002 has finished.\n")
    assert "14 minutes so far" in question.body


def test_close_question_words():
    question = run_dialogs.close_question(run_dialogs.RunState("KP_002", S.ASSEMBLING, 14 * 60, 12))
    assert question.body == ("A run is in progress. Closing the program stops it.\n"
                             "KP_002 is being assembled (14 minutes so far); that work would be lost.\n"
                             "The 12 finished samples are kept, and you can continue later.")
    assert [(c.key, c.label) for c in question.choices] == [
        ("keep", "Keep running"), ("stop_and_close", "Stop and close")]
    assert question.default == "keep" and question.cancel == "keep"
    one = run_dialogs.close_question(run_dialogs.RunState("KP_002", S.TRIMMING, 30, 1))
    assert one.body.endswith("The 1 finished sample is kept, and you can continue later.")
    assert "KP_002 is being trimmed (less than a minute so far)" in one.body
    none = run_dialogs.close_question(run_dialogs.RunState("KP_001", S.TRIMMING, 30, 0))
    assert none.body.endswith("You can continue later.")


def test_when_words_are_written_by_hand():
    assert run_dialogs.when_words("2026-11-17T03:12:40") == "17 Nov at 03:12"
    assert run_dialogs.when_words("2026-11-17T03:12:40", about=True) == "17 Nov at about 03:12"
    assert run_dialogs.when_words("2026-01-05T23:59") == "5 Jan at 23:59"
    for bad in ("", "yesterday", "2026-13-01T10:00:00", "2026-11-17", None):
        assert run_dialogs.when_words(bad) == ""


def _interrupted(cause="windows", missing=(), samples=3):
    script = demo.scenario("normal")
    config = script.config
    left = model.RunConfig(results_dir=config.results_dir, samples=config.samples[8 - samples:])
    return demo.InterruptedDemo(config=left, finished=23, total=40, when="2026-11-17T03:12:40",
                                cause=cause, missing=list(missing))


def test_interrupted_offer_words_for_each_cause():
    windows = run_dialogs.interrupted_offer(_interrupted("windows"))
    assert windows.text == ("The last run was interrupted on 17 Nov at 03:12 because Windows shut down or "
                            "restarted. 23 of 40 samples are finished. Nothing that was finished has been lost.")
    assert windows.can_continue and windows.level == "info" and windows.left_out == []
    assert [s.name for s in windows.config.samples] == ["KP_006", "KP_007", "KP_008"]
    crash = run_dialogs.interrupted_offer(_interrupted("unknown"))
    assert crash.text == ("The last run did not finish: it ended unexpectedly on 17 Nov at about 03:12. "
                          "23 of 40 samples are finished. Nothing that was finished has been lost.")
    stopped = run_dialogs.interrupted_offer(_interrupted("stopped"))
    assert stopped.text.startswith("The last run in this folder was stopped on 17 Nov at 03:12 ")
    assert stopped.can_continue


def test_interrupted_offer_when_read_files_have_gone():
    some = run_dialogs.interrupted_offer(_interrupted(missing=["KP_007"]))
    assert some.can_continue
    assert [s.name for s in some.config.samples] == ["KP_006", "KP_008"]
    assert some.left_out == [("KP_007", run_dialogs.MISSING_READS_NOTE)]
    assert some.text.endswith("1 sample will be left out, because its read files are no longer where they were.")
    every = run_dialogs.interrupted_offer(_interrupted(missing=["KP_006", "KP_007", "KP_008"]))
    assert not every.can_continue and every.config is None and every.level == "warn"
    assert every.text.endswith(run_dialogs.CANNOT_CONTINUE) and len(every.left_out) == 3
    assert run_dialogs.missing_sentence(8) == ("8 samples were left out because their read files are no "
                                               "longer where they were.")
    assert run_dialogs.missing_sentence(1).startswith("1 sample was left out because its read files")
    assert run_dialogs.missing_sentence(0) == ""


# ------------------------------------------------------------------ the stand-in, on its own

EVENT_CLASSES = (model.BatchStarted, model.SampleStatusChanged, model.StageProgress, model.Working,
                 model.LogLine, model.SampleFinished, model.BatchProgress, model.Notice, model.BatchFinished)


def _play(name, speed=200000.0, stop=None, resume=False, config=None):
    """Run a scenario to its end without any window. Returns (runner, events)."""
    script = demo.scenario(name)
    script.speed = speed
    events: "queue.Queue[model.Event]" = queue.Queue()
    runner = demo.DemoRunner(config or script.config, events, left_out=script.left_out, resume=resume,
                             script=script)
    if stop is not None:
        runner.request_stop(stop)
    runner.start()
    runner.join(30)
    assert not runner.is_alive()
    seen = []
    while True:
        try:
            seen.append(events.get_nowait())
        except queue.Empty:
            return runner, seen


def _final_states(events):
    states = {}
    for event in events:
        if isinstance(event, model.SampleStatusChanged):
            states[event.name] = event.status
    return states


def test_scenarios_are_the_ones_promised():
    assert set(demo.SCENARIOS) >= {"normal", "stopped", "vanished", "big"}
    assert len(demo.scenario("normal").config.samples) == 8
    assert len(demo.scenario("big").config.samples) == 120
    with pytest.raises(KeyError):
        demo.scenario("nope")


def test_stand_in_posts_only_model_events_with_start_first_and_finish_last():
    _runner, events = _play("normal")
    assert all(type(event) in EVENT_CLASSES for event in events)
    assert isinstance(events[0], model.BatchStarted) and events[0].total == 8
    assert events[0].names == ["KP_%03d" % n for n in range(1, 9)]
    assert isinstance(events[-1], model.BatchFinished)
    assert sum(isinstance(e, model.BatchFinished) for e in events) == 1
    end = events[-1]
    assert end.reason is EndReason.COMPLETED
    assert end.counts == {"done": 6, "failed": 1, "done_notes": 1, "left_out": 1}
    assert end.message.startswith("Finished at ")
    assert end.message.endswith("6 done, 1 done (check notes), 1 failed, 1 left out.")
    states = _final_states(events)
    assert states["KP_003"] is S.FAILED and states["KP_006"] is S.DONE_NOTES
    assert sum(1 for s in states.values() if s is S.DONE) == 6
    outcomes = {e.outcome.name: e.outcome for e in events if isinstance(e, model.SampleFinished)}
    assert len(outcomes) == 8 and outcomes["KP_003"].next_step and outcomes["KP_001"].contigs
    assert any(isinstance(e, model.Working) for e in events)
    assert any(isinstance(e, model.StageProgress) and e.text.startswith("Step 3 of 7: ") for e in events)
    assert any(isinstance(e, model.StageProgress) and e.stage is Stage.JOIN_LANES for e in events)
    etas = [e.eta_seconds for e in events if isinstance(e, model.BatchProgress)]
    assert etas[0] is None and etas[1] is None and any(eta for eta in etas)


def test_stand_in_stopped_and_vanished_batches():
    _runner, events = _play("stopped")
    assert events[-1].reason is EndReason.STOPPED
    assert events[-1].message == ("Stopped. 2 finished, 1 failed, 1 stopped, 4 not started. "
                                  "Nothing that was finished has been lost.")
    states = _final_states(events)
    assert states["KP_004"] is S.STOPPED
    assert [states["KP_%03d" % n] for n in (5, 6, 7, 8)] == [S.NOT_STARTED] * 4
    assert any(isinstance(e, model.Notice) and e.kind == "battery" for e in events)
    _runner, events = _play("vanished")
    end = events[-1]
    assert end.reason is EndReason.ENVIRONMENT
    assert "can no longer be reached" in end.message and "The 4 finished samples are safe." in end.message
    assert _final_states(events)["KP_006"] is S.STOPPED


def test_stand_in_obeys_stop_requests():
    runner, events = _play("normal", stop=StopMode.AFTER_CURRENT)
    assert runner.requests == [StopMode.AFTER_CURRENT]
    states = _final_states(events)
    assert events[-1].reason is EndReason.STOPPED
    assert states["KP_001"] is S.DONE and states["KP_002"] is S.NOT_STARTED
    runner, events = _play("normal", stop=StopMode.NOW)
    assert events[-1].reason is EndReason.STOPPED
    assert all(status is S.NOT_STARTED for status in _final_states(events).values())
    script = demo.scenario("normal")
    lone = demo.DemoRunner(script.config, queue.Queue(), script=script)
    lone.request_stop(StopMode.AFTER_CURRENT)
    assert lone.stop_pending() is StopMode.AFTER_CURRENT
    lone.cancel_stop()
    assert lone.stop_pending() is None and lone.requests == [StopMode.AFTER_CURRENT, "cancel"]
    assert lone.current() == ("", 0.0)


def test_stand_in_resume_runs_only_what_it_is_given_and_ignores_the_scripted_end():
    script = demo.scenario("stopped")
    rest = model.RunConfig(results_dir=script.config.results_dir, samples=script.config.samples[3:])
    _runner, events = _play("stopped", resume=True, config=rest)
    assert events[0].names == ["KP_004", "KP_005", "KP_006", "KP_007", "KP_008"]
    assert events[-1].reason is EndReason.COMPLETED
    assert set(_final_states(events).values()) == {S.DONE, S.DONE_NOTES}


# =========================================================================== with a display

def _display():
    wanted = os.environ.get("TESSERACT_WIN_TEST_DISPLAY")
    if wanted:
        return wanted
    if sys.platform.startswith("linux"):
        if os.path.exists("/tmp/.X11-unix/X99"):
            return ":99"
        return os.environ.get("DISPLAY") or None
    return ""                    # Windows and macOS need no display name


DISPLAY = _display()
needs_display = pytest.mark.skipif(DISPLAY is None, reason="no display (DISPLAY is not set)")
FAST = 60000.0                   # a ten-minute sample in a hundredth of a second


class Stage4:
    """A bare window with only the Run page, fed by the stand-in."""

    def __init__(self, monkeypatch, name, speed=FAST, start=True, dark=False, scale=1.0):
        import tkinter as tk
        from tesseract_win.gui import theme
        self.awake = []
        self.flashes = []
        self.opened = []
        monkeypatch.setattr(osapi, "keep_awake", lambda on: self.awake.append(bool(on)) or bool(on))
        monkeypatch.setattr(osapi, "flash_taskbar", lambda root: self.flashes.append(root) or True)
        monkeypatch.setattr(osapi, "open_path", lambda path: self.opened.append(path) or True)
        monkeypatch.setattr(osapi, "power_status", lambda: (False, None, False))
        self.root = tk.Tk(screenName=DISPLAY or None)
        self.root.geometry("1000x700+0+0")
        theme.init(self.root, is_dark=dark, scale=scale)
        # The pictures of an earlier window must be let go of here, on the main thread: if the
        # collector meets them later on the stand-in's thread, Tk makes that thread wait.
        gc.collect()
        self.app, self.page = demo.open_demo(self.root, name, speed=speed, start=start)
        self.root.update()

    # -- time
    def pump(self, seconds=0.05):
        end = time.time() + seconds
        while True:
            self.root.update()
            if time.time() >= end:
                break
            time.sleep(0.005)

    def until(self, condition, timeout=20.0):
        end = time.time() + timeout
        while not condition():
            self.root.update()
            if time.time() > end:
                raise AssertionError("timed out; phase %r, statuses %r" % (self.page.phase, self.page.statuses()))
            time.sleep(0.005)
        self.root.update()

    def until_ended(self, timeout=20.0):
        self.until(lambda: self.page.phase == "ended", timeout)

    def until_sample(self, name=None, timeout=20.0):
        self.until(lambda: self.page.phase == "running" and bool(self.page._current)
                   and (name is None or self.page._current == name), timeout)

    # -- looking
    @property
    def runner(self):
        return self.app.runners[-1]

    def shown(self, widget):
        return bool(widget.winfo_manager()) and bool(widget.winfo_ismapped())

    def answer(self, key, after_ms=250):
        """Press a button of the question that is about to open."""
        def press():
            if self.page.open_question is not None and self.page.open_question.is_open:
                self.page.open_question.invoke(key)
            else:
                self.root.after(40, press)
        self.root.after(after_ms, press)

    def calls(self, kind):
        return [call for call in self.app.calls if call[0] == kind]

    def close(self):
        try:
            if self.page.is_running():
                self.runner.request_stop(StopMode.NOW)
            for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(job)          # timers outlive a window within one process
            self.root.destroy()
        except Exception:
            pass


@pytest.fixture
def stage(monkeypatch):
    made = []

    def make(name, **options):
        window = Stage4(monkeypatch, name, **options)
        made.append(window)
        return window

    yield make
    for window in made:
        window.close()
        window.app = window.page = window.root = None
    made.clear()
    gc.collect()


def _names(*numbers):
    return ["KP_%03d" % n for n in numbers]


@needs_display
@pytest.mark.gui
def test_normal_batch_final_rows_buttons_and_housekeeping(stage):
    s = stage("normal")
    page = s.page
    assert page.is_running()
    s.until_ended()
    assert not page.is_running() and page.end_reason is EndReason.COMPLETED
    statuses = page.statuses()
    assert list(statuses) == _names(1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert statuses["KP_003"] is S.FAILED and statuses["KP_006"] is S.DONE_NOTES
    assert statuses["KP_009"] is S.LEFT_OUT
    assert [statuses[n] for n in _names(1, 2, 4, 5, 7, 8)] == [S.DONE] * 6
    # the rows carry the numbers and the notes
    table = page.table
    first = page._rows["KP_001"].row_id
    assert table.value(first, "status") == "Done" and table.value(first, "contigs") == "79"
    assert table.value(first, "total_length") == "5,414,729" and table.value(first, "n50") == "197,863"
    failed = page._rows["KP_003"].row_id
    assert "incomplete or damaged" in table.value(failed, "notes")
    assert "Copy it again" in table.value(failed, "notes")
    assert table.value(page._rows["KP_009"].row_id, "notes") == "No Read 2 file was found for this sample."
    # the result panel
    assert page.end_counts == {"done": 6, "failed": 1, "done_notes": 1, "left_out": 1}
    assert page.shown_end_actions == ["retry_failed", "new_run", "back_to_samples"]
    assert s.shown(page.end_buttons["retry_failed"]) and not s.shown(page.end_buttons["continue"])
    assert str(page.end_buttons["retry_failed"].cget("text")) == "Try the failed sample again"
    assert str(page.result_heading.cget("text")).startswith("Finished at ")
    assert str(page.result_body.cget("text")).startswith("It took ")
    assert "done" not in str(page.result_body.cget("text"))             # the counts are shown once, as dots
    assert [str(l.cget("text")) for l in page._count_labels] == [
        "6 done", "1 done (check notes)", "1 failed", "1 left out"]
    assert "(7 files, one per sample)" in str(page.result_where.cget("text"))
    assert str(page.result_hint.cget("text")).startswith("1 sample failed.")
    assert not s.shown(page.now_frame) and s.shown(page.result_frame)
    # Stop is off, the Open buttons are on
    assert page.stop_button.instate(["disabled"]) and str(page.stop_button.cget("text")) == "Stop"
    assert page.open_results_button.instate(["!disabled"]) and page.open_summary_button.instate(["!disabled"])
    # the PC was kept awake for the run and released; the shell was told; the taskbar flashed
    assert s.awake == [True, False]
    assert s.calls("run_ended") == [("run_ended",)]
    assert s.calls("title")[-1] == ("title", "Finished: 7 done, 1 failed")
    assert ("title", "Sample 1 of 8: KP_001") in s.app.calls
    assert len(s.flashes) == 1
    assert s.runner.requests == []
    assert page.progress.cget("value") in (8, 8.0, "8", "8.0")


@needs_display
@pytest.mark.gui
def test_strip_and_double_click_open_the_right_things(stage):
    s = stage("normal")
    page = s.page
    assert str(page.strip_text.cget("text")) == texts.SELECT_A_ROW
    s.until_ended()
    page.select_sample("KP_002")
    s.pump()
    assert str(page.strip_text.cget("text")).startswith("KP_002: Done. 85 contigs")
    assert s.shown(page.strip_buttons["open_report"]) and s.shown(page.strip_buttons["open_folder"])
    assert not s.shown(page.strip_buttons["open_log"])
    page.strip_buttons["open_report"].invoke()
    assert s.opened[-1].endswith(os.path.join("KP_002", "assembly", "report.html"))
    page.strip_buttons["open_folder"].invoke()
    assert s.opened[-1].endswith("KP_002")
    page.select_sample("KP_003")
    s.pump()
    assert str(page.strip_text.cget("text")) == (
        "KP_003: A read file is incomplete or damaged (KP_003_S3_L001_R2_001.fastq.gz). It may have been "
        "cut short while copying or downloading. Copy it again from the original, then try again.")
    assert s.shown(page.strip_buttons["open_log"]) and not s.shown(page.strip_buttons["open_report"])
    page.strip_buttons["open_log"].invoke()
    assert s.opened[-1].endswith(os.path.join("KP_003", "last_failed_attempt", "assembly.log"))
    # double-click is the shortcut for the first button
    page._on_activate(page._rows["KP_005"].row_id)
    assert s.opened[-1].endswith(os.path.join("KP_005", "assembly", "report.html"))
    page._on_activate(page._rows["KP_003"].row_id)
    assert s.opened[-1].endswith("assembly.log")
    before = len(s.opened)
    page._on_activate(page._rows["KP_009"].row_id)            # left out: nothing to open
    assert len(s.opened) == before
    assert not s.shown(page.strip_buttons["open_folder"])
    # the two buttons at the bottom right
    page.open_results_button.invoke()
    assert s.opened[-1] == s.app.script.config.results_dir
    page.open_summary_button.invoke()
    assert s.opened[-1] == os.path.join(s.app.script.config.results_dir, "run_summary.html")


@needs_display
@pytest.mark.gui
def test_a_file_that_will_not_open_falls_back_to_its_folder_and_says_so(stage, monkeypatch):
    s = stage("normal")
    s.until_ended()
    tried = []
    monkeypatch.setattr(osapi, "open_path", lambda path: tried.append(path) or path.endswith("KP_001"))
    s.page.select_sample("KP_001")
    s.page.strip_buttons["open_report"].invoke()
    assert len(tried) == 2 and tried[1].endswith("KP_001")
    assert "the folder that holds it was opened instead" in s.app.help_line.text
    monkeypatch.setattr(osapi, "open_path", lambda path: False)
    s.page.open_summary_button.invoke()
    assert "run_summary.html could not be opened" in s.app.help_line.text


@pytest.mark.skipif(os.name == "nt", reason="depends on keyboard focus and timing that a CI session on Windows does not give; to be checked by hand on a real PC")
@needs_display
@pytest.mark.gui
def test_while_running_the_page_shows_the_sample_the_step_and_the_clocks(stage):
    s = stage("normal", speed=240.0)                      # a sample takes about two seconds
    page = s.page
    assert str(page.title_label.cget("text")) == texts.STARTING
    assert page.stop_button.instate(["disabled"])
    assert page.open_summary_button.instate(["disabled"])
    assert s.shown(page.intro) and str(page.intro.cget("text")) == texts.PAGE_INTRO["run"]
    s.until(lambda: page.status_of("KP_001") is S.ASSEMBLING)
    s.pump(0.3)
    assert str(page.title_label.cget("text")) == "Sample 1 of 8: KP_001"
    assert page.stop_button.instate(["!disabled"]) and str(page.stop_button.cget("text")) == "Stop"
    assert page.chips.state_of(Stage.TRIM) == "done" and page.chips.state_of(Stage.ASSEMBLE) == "running"
    assert page.chips.state_of(Stage.SUMMARISE) == "waiting"
    assert "join_lanes" not in page.chips.visible_stages()
    assert str(page.step_line.words.cget("text")).startswith("Step ")
    working = str(page.working_label.cget("text"))
    assert working.startswith("Working: ") and working.endswith(texts.SILENCE_IS_NORMAL)
    clock = str(page.clock_label.cget("text"))
    assert clock.startswith("This sample ") and clock.endswith(texts.ESTIMATE_PENDING)
    assert str(page.advice_label.cget("text")) == texts.ADVICE
    assert str(page.settings_label.cget("text")).startswith("Settings: standard trimming, standard assembly, ")
    assert str(page.tally_label.cget("text")) == "0 done, 0 failed."
    assert s.awake == [True]
    assert s.shown(page.now_frame) and not s.shown(page.result_frame)
    # the clocks move
    s.pump(0.4)
    assert str(page.clock_label.cget("text")) != clock
    # after the first sample there is an estimate, the row is filled in, the Open buttons work
    s.until(lambda: page.status_of("KP_001") is S.DONE and page._current == "KP_002")
    s.pump(0.2)
    assert "left (rough estimate)." in str(page.clock_label.cget("text"))
    assert page.table.value(page._rows["KP_001"].row_id, "contigs") == "79"
    assert page.open_summary_button.instate(["!disabled"])
    assert str(page.tally_label.cget("text")) == "1 done, 0 failed."
    assert str(page.title_label.cget("text")) == "Sample 2 of 8: KP_002"
    # the log pane is fed, and shown only on request
    assert not s.shown(page.log_pane) and page.log_pane.line_count > 0
    page.log_toggle.invoke()
    s.pump()
    assert s.shown(page.log_pane) and str(page.log_toggle.cget("text")) == "Hide log"
    page.log_toggle.invoke()
    s.pump()
    assert not s.shown(page.log_pane) and str(page.log_toggle.cget("text")) == "Show log"


@needs_display
@pytest.mark.gui
def test_two_lane_sample_shows_the_join_lanes_chip(stage):
    s = stage("normal", speed=2500.0)
    s.until_sample("KP_005")
    s.pump(0.05)
    assert "join_lanes" in s.page.chips.visible_stages()
    s.until_sample("KP_006")
    s.pump(0.05)
    assert "join_lanes" not in s.page.chips.visible_stages()


@needs_display
@pytest.mark.gui
def test_stopped_batch_then_continue_runs_only_what_is_left(stage):
    s = stage("stopped")
    page = s.page
    s.until_ended()
    assert page.end_reason is EndReason.STOPPED
    statuses = page.statuses()
    assert [statuses[n] for n in _names(1, 2)] == [S.DONE, S.DONE]
    assert statuses["KP_003"] is S.FAILED and statuses["KP_004"] is S.STOPPED
    assert [statuses[n] for n in _names(5, 6, 7, 8)] == [S.NOT_STARTED] * 4
    assert page.shown_end_actions == ["continue", "retry_failed", "new_run", "back_to_samples"]
    assert all(s.shown(page.end_buttons[key]) for key in page.shown_end_actions)
    assert str(page.result_heading.cget("text")) == "Stopped."
    assert str(page.result_body.cget("text")) == texts.NOTHING_LOST      # the counts are shown once, as dots
    assert [str(l.cget("text")) for l in page._count_labels] == [
        "2 done", "1 failed", "1 stopped", "4 not started", "1 left out"]
    assert s.calls("title")[-1] == ("title", "Stopped: 2 of 8 done, 1 failed")
    assert "battery" not in page.notices.kinds()                 # it was about the run, and the run is over
    assert s.awake == [True, False]
    # Continue: nothing is asked, finished samples are left alone
    page.end_buttons["continue"].invoke()
    s.until(lambda: len(s.app.runners) == 2)
    start = s.calls("start_run")[-1]
    assert start[1] == _names(4, 5, 6, 7, 8) and start[3] is True
    assert start[2] == [("KP_009", "No Read 2 file was found for this sample.")]
    assert s.runner.resume is True
    assert page.is_running() and page.status_of("KP_001") is S.DONE and page.status_of("KP_004") is S.WAITING
    # the header, the bar and the counts go over the whole list, not over what is left of it
    assert float(str(page.progress.cget("value"))) == 3 and float(str(page.progress.cget("maximum"))) == 8
    assert page._total() == 8 and page._counts_now() == (2, 1)
    s.until_ended()
    assert ("title", "Sample 4 of 8: KP_004") in s.calls("title")
    assert ("title", "Sample 8 of 8: KP_008") in s.calls("title")
    assert not any(title[1].startswith("Sample 1 of 5") for title in s.calls("title"))
    assert float(str(page.progress.cget("value"))) == 8
    statuses = page.statuses()
    assert list(statuses) == _names(1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert statuses["KP_003"] is S.FAILED and statuses["KP_006"] is S.DONE_NOTES
    assert [statuses[n] for n in _names(1, 2, 4, 5, 7, 8)] == [S.DONE] * 6
    assert page.end_reason is EndReason.COMPLETED
    assert page.shown_end_actions == ["retry_failed", "new_run", "back_to_samples"]
    assert s.awake == [True, False, True, False]
    assert len(s.calls("run_ended")) == 2


@needs_display
@pytest.mark.gui
def test_try_the_failed_samples_again(stage):
    s = stage("normal")
    page = s.page
    s.until_ended()
    page.end_buttons["retry_failed"].invoke()
    s.until(lambda: len(s.app.runners) == 2)
    start = s.calls("start_run")[-1]
    assert start[1] == ["KP_003"] and start[3] is True
    s.until_ended()
    assert page.status_of("KP_003") is S.FAILED and page.status_of("KP_001") is S.DONE
    assert "This is the second time." in page.table.value(page._rows["KP_003"].row_id, "notes")
    assert page.end_counts == {"done": 6, "failed": 1, "done_notes": 1, "left_out": 1}


@needs_display
@pytest.mark.gui
def test_batch_ended_by_the_pc(stage):
    s = stage("vanished")
    page = s.page
    s.until_ended()
    assert page.end_reason is EndReason.ENVIRONMENT
    heading = str(page.result_heading.cget("text"))
    assert heading.startswith("Stopped: the results folder can no longer be reached (")
    assert str(page.result_body.cget("text")) == ("Reconnect the drive, then press Continue. "
                                                  "The 4 finished samples are safe.")
    statuses = page.statuses()
    assert statuses["KP_006"] is S.STOPPED and statuses["KP_007"] is S.NOT_STARTED
    assert statuses["KP_008"] is S.NOT_STARTED and statuses["KP_005"] is S.DONE
    assert page.shown_end_actions == ["continue", "retry_failed", "new_run", "back_to_samples"]
    assert s.awake == [True, False] and len(s.flashes) == 1


@needs_display
@pytest.mark.gui
def test_a_hundred_and_twenty_samples(stage):
    s = stage("big", speed=400000.0)
    page = s.page
    assert len(page.table) == 120
    s.until_ended(timeout=60)
    statuses = page.statuses()
    assert len(statuses) == 120 and all(status.is_final for status in statuses.values())
    assert page.end_counts == {"done": 115, "done_notes": 3, "failed": 2}
    assert statuses["KP_041"] is S.FAILED and statuses["KP_083"] is S.FAILED
    assert s.calls("title")[-1] == ("title", "Finished: 118 done, 2 failed")
    assert str(page.end_buttons["retry_failed"].cget("text")) == "Try the 2 failed samples again"
    assert page.log_pane.line_count <= page.log_pane.max_lines == 2000
    assert page.table.value(page._rows["KP_120"].row_id, "status") == "Done"


@needs_display
@pytest.mark.gui
def test_events_are_taken_a_bounded_number_per_tick(stage):
    s = stage("interrupted", start=False)
    page = s.page
    for number in range(3000):
        page._events.put(model.LogLine(name="KP_001", text="line %d" % number))
    assert page.process_events() <= page.MAX_EVENTS_PER_TICK
    assert page._events.qsize() >= 3000 - page.MAX_EVENTS_PER_TICK
    assert page.process_events(limit=10) == 10
    while page.process_events():
        pass
    assert page.log_pane.line_count == 2000                 # the log keeps the last 2,000 lines
    assert page.log_pane.get_text().rstrip("\n").endswith("line 2999")


@needs_display
@pytest.mark.gui
def test_stop_after_then_keep_going_then_stop_now(stage):
    s = stage("normal", speed=150.0)                       # a sample takes about four seconds
    page = s.page
    s.until_sample("KP_001")
    s.answer("after_current")
    page.stop_button.invoke()                              # returns when the question is answered
    assert s.runner.requests == [StopMode.AFTER_CURRENT]
    assert page.phase == "stopping_after" and page.open_question is None
    assert str(page.stop_button.cget("text")) == "Stop now" and page.stop_button.instate(["!disabled"])
    assert str(page.title_label.cget("text")) == "Stopping after KP_001..."
    s.pump()
    assert s.shown(page.keep_going_button)
    assert str(page.keep_going_button.cget("text")) == "Keep going after all"
    assert str(page.tally_label.cget("text")) == "Sample 1 of 8. 0 done, 0 failed."
    # keep going after all
    page.keep_going_button.invoke()
    s.pump()
    assert s.runner.requests == [StopMode.AFTER_CURRENT, "cancel"]
    assert page.phase == "running" and not s.shown(page.keep_going_button)
    assert str(page.stop_button.cget("text")) == "Stop"
    assert str(page.title_label.cget("text")) == "Sample 1 of 8: KP_001"
    # Stop again, this time at once
    s.answer("now")
    page.stop_button.invoke()
    assert s.runner.requests == [StopMode.AFTER_CURRENT, "cancel", StopMode.NOW]
    assert page.phase in ("stopping_now", "ended")
    s.until_ended()
    statuses = page.statuses()
    assert statuses["KP_001"] is S.STOPPED
    assert [statuses[n] for n in _names(2, 3, 4, 5, 6, 7, 8)] == [S.NOT_STARTED] * 7
    assert page.end_reason is EndReason.STOPPED
    assert page.shown_end_actions == ["continue", "new_run", "back_to_samples"]
    assert page.stop_button.instate(["disabled"])
    assert s.awake == [True, False]


@needs_display
@pytest.mark.gui
def test_stop_after_the_current_sample_ends_the_batch_there(stage):
    s = stage("normal", speed=600.0)
    page = s.page
    s.until_sample("KP_002")
    s.answer("after_current", after_ms=100)
    page.stop_button.invoke()
    assert s.runner.requests == [StopMode.AFTER_CURRENT]
    s.until_ended()
    statuses = page.statuses()
    assert statuses["KP_001"] is S.DONE and statuses["KP_002"] is S.DONE
    assert [statuses[n] for n in _names(3, 4, 5, 6, 7, 8)] == [S.NOT_STARTED] * 6
    assert page.end_reason is EndReason.STOPPED and "continue" in page.shown_end_actions


@needs_display
@pytest.mark.gui
def test_once_stop_after_is_pending_the_button_asks_again_with_two_choices(stage):
    s = stage("normal", speed=100.0)
    page = s.page
    s.until_sample("KP_001")
    s.answer("after_current", after_ms=100)
    page.stop_button.invoke()
    seen = {}

    def look():
        asked = page.open_question
        if asked is None or not asked.is_open:
            s.root.after(40, look)
            return
        seen["keys"] = list(asked.dialog.buttons)
        seen["labels"] = [str(b.cget("text")) for b in asked.dialog.buttons.values()]
        asked.invoke("keep")

    s.root.after(100, look)
    page.stop_button.invoke()
    assert seen == {"keys": ["keep", "now"], "labels": ["Wait for KP_001", "Stop now"]}
    assert s.runner.requests == [StopMode.AFTER_CURRENT] and page.phase == "stopping_after"


@needs_display
@pytest.mark.gui
def test_keep_running_escape_and_enter_stop_nothing(stage):
    s = stage("normal", speed=100.0)
    page = s.page
    s.until_sample("KP_001")
    seen = {}

    def look(action):
        asked = page.open_question
        if asked is None or not asked.is_open:
            s.root.after(40, lambda: look(action))
            return
        dialog = asked.dialog
        seen["default"] = dialog.default
        seen["cancel"] = dialog.cancel
        seen["body"] = str(dialog.body.cget("text"))
        seen["labels"] = [str(b.cget("text")) for b in dialog.buttons.values()]
        if action == "button":
            asked.invoke("keep")
        else:                                              # a key, with the keyboard where it opens
            target = dialog.buttons[dialog.default]
            target.focus_force()
            dialog.update()
            target.event_generate(action)

    for action in ("button", "<Escape>", "<Return>"):
        s.root.after(100, lambda a=action: look(a))
        page.stop_button.invoke()
        assert page.open_question is None
        assert s.runner.requests == [] and page.phase == "running", action
    assert seen["default"] == "keep" and seen["cancel"] == "keep"
    assert seen["labels"] == ["Keep running", "Stop after KP_001", "Stop now"]
    assert seen["body"].startswith("KP_001 is being ") and "so far)" in seen["body"]
    assert "No other sample is affected." in seen["body"]
    assert page.is_running()


@needs_display
@pytest.mark.gui
def test_the_question_follows_the_run_and_closes_itself_when_the_run_ends(stage):
    s = stage("normal", speed=3000.0)                      # the whole batch takes about 1.5 seconds
    page = s.page
    s.until_sample("KP_001")
    bodies = []

    def watch():
        asked = page.open_question
        if asked is not None and asked.is_open:
            bodies.append((str(asked.dialog.body.cget("text")).split(" ")[0],
                           str(asked.dialog.buttons["after_current"].cget("text"))))
        if page.is_running():
            s.root.after(30, watch)

    s.root.after(30, watch)
    page.stop_button.invoke()                              # nobody answers: the run ends underneath
    assert page.phase == "ended" and page.open_question is None
    assert s.runner.requests == []
    named = [name for name, _button in bodies]
    assert named[0] == "KP_001" and len(set(named)) >= 3   # the words moved on with the run
    assert all(button == "Stop after %s" % name for name, button in bodies)
    assert page.end_reason is EndReason.COMPLETED


@needs_display
@pytest.mark.gui
def test_closing_the_window_asks_only_during_a_run(stage):
    s = stage("normal", speed=100.0)
    page = s.page
    s.until_sample("KP_001")
    seen = {}

    def look():
        asked = page.open_question
        if asked is None or not asked.is_open:
            s.root.after(40, look)
            return
        seen["labels"] = [str(b.cget("text")) for b in asked.dialog.buttons.values()]
        seen["body"] = str(asked.dialog.body.cget("text"))
        seen["default"] = asked.dialog.default
        asked.invoke("keep")

    s.root.after(100, look)
    assert page.confirm_close() is False
    assert seen["labels"] == ["Keep running", "Stop and close"] and seen["default"] == "keep"
    assert seen["body"].startswith("A run is in progress. Closing the program stops it.\nKP_001 is being ")
    assert s.runner.requests == [] and page.is_running()
    # Stop and close: the run is stopped at once and the window may close
    s.answer("stop_and_close", after_ms=100)
    assert page.confirm_close() is True
    assert s.runner.requests == [StopMode.NOW]
    assert not page.is_running() and page.status_of("KP_001") is S.STOPPED
    assert s.awake == [True, False]
    # nothing runs any more: no question
    assert page.confirm_close() is True and page.open_question is None


@needs_display
@pytest.mark.gui
def test_interrupted_run_bar_continues_at_once(stage):
    s = stage("interrupted")
    page = s.page
    assert not page.is_running() and page.phase == "idle"
    assert str(page.title_label.cget("text")) == "Run"
    s.until(page.has_offer)
    text = page.notices.text("interrupted")
    assert "because Windows shut down or restarted. 5 of 8 samples are finished." in text
    assert text.endswith(texts.NOTHING_LOST)
    assert [str(page.notices.button_of("interrupted", i).cget("text")) for i in (0, 1)] == [
        "Continue the run", "Not now"]
    assert page.stop_button.instate(["disabled"])
    page.notices.button_of("interrupted", 0).invoke()
    s.until(lambda: len(s.app.runners) == 1)
    start = s.calls("start_run")[-1]
    assert start[1:] == (_names(6, 7, 8), [], True)
    assert not page.has_offer() and page.is_running()
    s.until_ended()
    assert list(page.statuses()) == _names(6, 7, 8)
    assert page.end_reason is EndReason.COMPLETED


@needs_display
@pytest.mark.gui
def test_interrupted_run_with_missing_read_files(stage):
    s = stage("missing")
    page = s.page
    s.until(page.has_offer)
    assert page.notices.text("interrupted").endswith(
        "1 sample will be left out, because its read files are no longer where they were.")
    page.notices.button_of("interrupted", 0).invoke()
    s.until(lambda: len(s.app.runners) == 1)
    start = s.calls("start_run")[-1]
    assert start[1] == _names(6, 8) and start[3] is True
    assert start[2] == [("KP_007", run_dialogs.MISSING_READS_NOTE)]
    assert page.notices.text("missing_reads") == ("1 sample was left out because its read files are no "
                                                  "longer where they were.")
    s.until_ended()
    statuses = page.statuses()
    assert list(statuses) == _names(6, 8, 7) and statuses["KP_007"] is S.LEFT_OUT
    assert page.end_counts.get("left_out") == 1


@needs_display
@pytest.mark.gui
def test_not_now_takes_the_bar_away_and_nothing_starts(stage):
    s = stage("interrupted")
    s.until(s.page.has_offer)
    s.page.notices.button_of("interrupted", 1).invoke()
    s.pump(0.1)
    assert not s.page.has_offer() and "interrupted" not in s.page.notices.kinds()
    assert s.app.runners == [] and not s.page.is_running()


@needs_display
@pytest.mark.gui
def test_notice_events_become_bars_and_an_empty_text_clears_them(stage):
    s = stage("interrupted", start=False)
    page = s.page
    page._events.put(model.Notice(kind="memory", text=texts.low_memory("1.2 GB"), level="warn"))
    page._events.put(model.Notice(kind="battery", text=texts.battery_low(18), level="bad"))
    page.process_events()
    assert page.notices.text("memory") == texts.low_memory("1.2 GB") and page.notices.level("memory") == "warn"
    assert page.notices.level("battery") == "bad" and len(s.flashes) == 1      # red: the taskbar flashes
    page._events.put(model.Notice(kind="memory", text=""))
    page.process_events()
    assert "memory" not in page.notices.kinds() and "battery" in page.notices.kinds()
    # a bar closed by the user does not come back with the same words
    page.notices.dismiss("battery", by_user=True)
    page._events.put(model.Notice(kind="battery", text=texts.battery_low(18), level="bad"))
    page.process_events()
    assert "battery" not in page.notices.kinds()
    page._events.put(model.Notice(kind="battery", text=texts.battery_low(9), level="bad"))
    page.process_events()
    assert page.notices.text("battery") == texts.battery_low(9)


@needs_display
@pytest.mark.gui
def test_write_the_summary_again(stage, monkeypatch):
    from tesseract_win import summary_report
    s = stage("normal")
    page = s.page
    s.until_ended()
    words = ("run_summary.tsv could not be updated because it is open in another program. Close it and "
             "press 'Write the summary again'.")
    page._events.put(model.Notice(kind="info", text=words, level="warn"))
    page.process_events()
    assert page.notices.text("summary") == words
    button = page.notices.button_of("summary", 0)
    assert str(button.cget("text")) == "Write the summary again"
    calls = []

    def still_open(results_dir, **given):
        calls.append((results_dir, given))
        return ["run_summary.tsv"]

    monkeypatch.setattr(summary_report, "write_run_summary", still_open)
    button.invoke()
    s.until(lambda: len(calls) == 1 and not page._busy)
    folder, given = calls[0]
    assert folder == s.app.script.config.results_dir
    assert sorted(outcome.name for outcome in given["this_run"]) == _names(1, 2, 3, 4, 5, 6, 7, 8)
    assert given["left_out"] == [("KP_009", "No Read 2 file was found for this sample.")]
    info = given["run_info"]
    assert info["end_reason"] is EndReason.COMPLETED and info["message"].startswith("Finished at ")
    assert info["settings"].startswith("Settings: ") and info["samples"] == _names(1, 2, 3, 4, 5, 6, 7, 8)
    assert info["started"] and info["finished"]
    assert "summary" in page.notices.kinds() and s.app.help_line.text == page_run.SUMMARY_STILL_OPEN
    # the file has been closed: the bar goes
    monkeypatch.setattr(summary_report, "write_run_summary", lambda results_dir, **given: [])
    page.notices.button_of("summary", 0).invoke()
    s.until(lambda: "summary" not in page.notices.kinds())
    assert s.app.help_line.text == page_run.SUMMARY_WRITTEN
    # a runner that can do it itself is asked first
    page._events.put(model.Notice(kind="info", text=words, level="warn"))
    page.process_events()
    asked = []
    page._runner.write_summary_again = lambda: asked.append(1) or []
    page.notices.button_of("summary", 0).invoke()
    s.until(lambda: "summary" not in page.notices.kinds())
    assert asked == [1]


@needs_display
@pytest.mark.gui
def test_the_runner_looks_after_battery_notices_once_it_has_sent_one(stage, monkeypatch):
    s = stage("stopped", speed=300.0)                       # this script posts a battery notice
    page = s.page
    s.until(lambda: "battery" in page.notices.kinds())
    monkeypatch.setattr(osapi, "power_status", lambda: (True, 5, True))
    page._last_battery = 0.0
    page._watch_pc()
    assert page.notices.text("battery") == texts.ON_BATTERY      # the runner's words stay
    assert s.flashes == []


@needs_display
@pytest.mark.gui
def test_battery_and_sleep_are_noticed_by_the_window(stage, monkeypatch):
    s = stage("normal", speed=100.0)
    page = s.page
    s.until_sample("KP_001")
    monkeypatch.setattr(osapi, "power_status", lambda: (True, 64, True))
    page._last_battery = 0.0
    page._watch_pc()
    assert page.notices.text("battery") == texts.ON_BATTERY and page.notices.level("battery") == "warn"
    monkeypatch.setattr(osapi, "power_status", lambda: (True, 18, True))
    page._last_battery = 0.0
    page._watch_pc()
    assert page.notices.text("battery") == texts.battery_low(18) and page.notices.level("battery") == "bad"
    assert len(s.flashes) == 1
    monkeypatch.setattr(osapi, "power_status", lambda: (False, 80, True))
    page._last_battery = 0.0
    page._watch_pc()
    assert "battery" not in page.notices.kinds()
    # the wall clock jumped: the PC was asleep
    page._last_wall = time.time() - 3 * 3600
    page._watch_pc()
    assert page.notices.text("slept").startswith("The PC was asleep from ")
    assert page.notices.text("slept").endswith("The run carried on when it woke up.")
    assert page.is_running()


@needs_display
@pytest.mark.gui
def test_a_runner_that_cannot_start_or_dies_ends_the_page_in_words(stage):
    s = stage("interrupted", start=False)
    page = s.page
    config = s.app.script.config

    def broken(*_args, **_options):
        raise RuntimeError("no runner")

    page.begin(config, [], runner_factory=broken)
    assert not page.is_running() and page.phase == "ended"
    assert str(page.result_heading.cget("text")).startswith("The run could not start")
    assert s.calls("run_ended") == [("run_ended",)] and s.awake == [False]
    assert set(page.statuses().values()) == {S.NOT_STARTED}

    class Silent:                                          # starts, posts nothing, and is gone
        def start(self):
            pass

        def is_alive(self):
            return False

        def current(self):
            return "", 0.0

    page.begin(config, [], runner_factory=lambda *a, **k: Silent())
    assert page.is_running()
    s.until_ended(timeout=10)
    assert page.end_reason is EndReason.ENVIRONMENT
    assert "ended unexpectedly" in str(page.result_heading.cget("text"))
    assert "continue" in page.shown_end_actions


@needs_display
@pytest.mark.gui
def test_between_two_samples_the_question_keeps_its_words(stage):
    s = stage("interrupted", start=False)
    page = s.page
    config = s.app.script.config

    class Quiet:                                           # alive, and says only what the test posts
        requests = []

        def start(self):
            pass

        def is_alive(self):
            return True

        def current(self):
            return "", 0.0

        def request_stop(self, mode):
            self.requests.append(mode)

    page.begin(config, [], runner_factory=lambda *a, **k: Quiet())
    post = page._events.put
    post(model.BatchStarted(total=8, names=[sample.name for sample in config.samples]))
    page.process_events()
    assert page._question_state() is None and page.stop_button.instate(["disabled"])
    post(model.SampleStatusChanged(name="KP_001", status=S.ASSEMBLING))
    page.process_events()
    state = page._question_state()
    assert (state.sample, state.status, state.finished) == ("KP_001", S.ASSEMBLING, 0)
    assert page.stop_button.instate(["!disabled"])
    post(model.SampleFinished(outcome=model.SampleOutcome(name="KP_001", status=S.DONE, contigs=70)))
    page.process_events()
    assert page.is_running() and page._question_state().sample == "KP_001"       # not None: no flicker
    assert str(page.step_line.words.cget("text")) == page_run.BETWEEN_SAMPLES
    post(model.SampleStatusChanged(name="KP_002", status=S.TRIMMING))
    page.process_events()
    state = page._question_state()
    assert (state.sample, state.status, state.finished) == ("KP_002", S.TRIMMING, 1)
    assert page.chips.state_of(Stage.TRIM) == "running"
    post(model.BatchFinished(reason=EndReason.STOPPED, message="", counts={}))
    page.process_events()
    assert page._question_state() is None and page.phase == "ended"
    assert page.status_of("KP_002") is S.STOPPED and page.status_of("KP_003") is S.NOT_STARTED
    assert str(page.result_heading.cget("text")).startswith("Stopped at ")


@needs_display
@pytest.mark.gui
def test_new_run_and_back_to_the_samples(stage):
    s = stage("normal")
    page = s.page
    s.until_ended()
    page.end_buttons["back_to_samples"].invoke()
    assert s.app.calls[-2:] == [("title", ""), ("show_step", 0)]
    assert page.phase == "ended" and len(page.table) == 9         # the results stay on the page
    page.end_buttons["new_run"].invoke()
    assert s.app.calls[-1] == ("new_run",)
    assert page.phase == "idle" and len(page.table) == 0
    assert page.open_summary_button.instate(["disabled"])
    s.pump()
    assert not s.shown(page.card) and s.shown(page.intro)


@needs_display
@pytest.mark.gui
def test_continue_is_refused_in_words_when_the_pc_is_not_ready(stage):
    s = stage("stopped")
    page = s.page
    s.until_ended()
    page.preflight = lambda config: ["The results folder cannot be written to (E:\\Results)."]
    page.end_buttons["continue"].invoke()
    s.until(lambda: "preflight" in page.notices.kinds())
    assert page.notices.text("preflight") == ("The run cannot start yet. The results folder cannot be "
                                              "written to (E:\\Results).")
    assert len(s.app.runners) == 1 and not page.is_running()
    assert page.end_buttons["continue"].instate(["!disabled"])
    page.preflight = lambda config: []
    page.end_buttons["continue"].invoke()
    s.until(lambda: len(s.app.runners) == 2)
    assert "preflight" not in page.notices.kinds()
    s.until_ended()


@needs_display
@pytest.mark.gui
def test_in_a_short_window_the_table_keeps_its_rows_and_the_rest_gives_way(stage):
    s = stage("normal", speed=100.0)
    page = s.page
    s.until_sample("KP_001")
    s.pump(0.3)
    assert page.compact == 0 and s.shown(page.intro) and s.shown(page.settings_label) and s.shown(page.strip)
    roomy = page.winfo_reqheight()                         # what the page asks for with everything
    around = s.root.winfo_height() - page.winfo_height()   # what the bare window adds
    s.root.geometry("1000x470+0+0")                         # a small laptop at 150 %
    s.pump(0.6)
    assert page.compact == 2
    assert not s.shown(page.intro) and not s.shown(page.settings_label) and not s.shown(page.strip)
    assert s.shown(page.advice_label) and s.shown(page.stop_button) and s.shown(page.open_summary_button)
    assert page.table.tree.winfo_height() >= 3 * page._look.row_height
    # a selected row brings its strip back even then
    page.select_sample("KP_009")
    s.pump(0.3)
    assert s.shown(page.strip) and str(page.strip_text.cget("text")).startswith("KP_009: ")
    # the strip took room from the table, so the next thing gives way: the advice about sleep
    s.pump(0.4)
    assert page.compact == 3 and not s.shown(page.advice_label) and not s.shown(page.now_rule)
    assert s.shown(page.chips) and s.shown(page.stop_button)
    assert "That is normal" not in str(page.working_label.cget("text")) or page._working is None
    # it settles: nothing flips while the window stays as it is
    seen = set()
    for _ in range(12):
        s.pump(0.05)
        seen.add(page.compact)
    assert seen == {3}
    # shorter still: the chips go last, and Stop is still there
    s.root.geometry("1000x350+0+0")
    s.pump(0.8)
    assert page.compact == 4 and not s.shown(page.chips)
    assert s.shown(page.stop_button) and s.shown(page.title_label) and s.shown(page.progress)
    # no room here for the table and the log together: the log takes the table's place
    page.log_toggle.invoke()
    s.pump(0.8)
    assert s.shown(page.log_pane) and not s.shown(page.table) and s.shown(page.stop_button)
    assert page.log_pane.winfo_height() >= 3 * page._look.row_height
    page.log_toggle.invoke()
    s.pump(0.8)
    assert s.shown(page.table) and not s.shown(page.log_pane) and page.compact == 4
    page.table.select([])
    s.root.geometry("1000x470+0+0")
    s.pump(0.8)
    assert page.compact == 2 and s.shown(page.advice_label) and s.shown(page.chips)
    s.root.geometry("1000x%d+0+0" % (around + roomy - 8))   # a little too short for everything
    s.pump(0.6)
    assert page.compact == 1 and not s.shown(page.intro) and s.shown(page.settings_label)
    s.root.geometry("1000x760+0+0")
    s.pump(0.6)
    assert page.compact == 0 and s.shown(page.intro) and s.shown(page.settings_label) and s.shown(page.strip)


@needs_display
@pytest.mark.gui
@pytest.mark.parametrize("dark,scale", [(True, 1.0), (False, 1.5)])
def test_page_builds_in_dark_and_at_150_percent(stage, dark, scale):
    s = stage("normal", dark=dark, scale=scale)
    s.until_ended()
    assert s.page.end_reason is EndReason.COMPLETED and len(s.page.table) == 9
