# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The whole window, end to end, with nobody at the mouse.

Each test opens the real window (the real shell, the real four pages), with the real
runner and the real engines, and does what a user does: adds read files, presses Start,
waits, reads what the window says. Nothing is stood in for except the things a test must
not do to the PC it runs on: opening a web browser, and using more than four threads.

    python -m pytest tests/test_gui_end_to_end.py -q

What is driven here:

- "Try the example", Start, trimming and assembly, the row ends Done with 2 contigs, the
  files of decision D1 are in the results folder, Open summary opens ``run_summary.html``;
- Stop now in the middle of the assembly: the row ends Stopped, nothing is left that looks
  finished, and Continue then finishes it without asking anything;
- a read file that was cut short: its row fails with a plain reason and a next step, and
  the batch goes on to the next sample;
- a second Start on the same results folder: the overwrite-or-skip question appears, Skip
  is its default button, and the skipped row says so.

The reads are the example of ``tools/make_example.py`` (a made-up 185 kb isolate), so a
run takes a few seconds. Skipped without a display or without the built engines.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tesseract_win import appdirs, osapi, results          # noqa: E402
from tesseract_win.gui import settings_store, texts        # noqa: E402
from tesseract_win.model import EndReason, RunStatus       # noqa: E402

EXAMPLE = "Example_isolate"
MOST_THREADS = 4


def _display():
    wanted = os.environ.get("TESSERACT_WIN_TEST_DISPLAY")
    if wanted:
        return wanted
    if sys.platform.startswith("linux"):
        if os.path.exists("/tmp/.X11-unix/X99"):
            return ":99"
        return os.environ.get("DISPLAY") or None
    return ""                                        # Windows and macOS always have a desktop


def _engines():
    try:
        return appdirs.find_engines()
    except appdirs.EngineMissing:
        return None


DISPLAY = _display()
pytestmark = [
    pytest.mark.gui, pytest.mark.engines, pytest.mark.slow,
    pytest.mark.skipif(DISPLAY is None, reason="no display (DISPLAY is not set)"),
    pytest.mark.skipif(_engines() is None, reason="the engines are not built (build/engines/<platform>)"),
]


# =========================================================================== fixtures

@pytest.fixture(scope="module")
def example_reads(tmp_path_factory):
    """The folder ``example/`` as it ships: made once, by the tool that makes the real one."""
    spec = importlib.util.spec_from_file_location("make_example", os.path.join(ROOT, "tools", "make_example.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = str(tmp_path_factory.mktemp("shipped") / "example")
    assert module.main(["make_example.py", folder]) == 0
    return folder


@pytest.fixture
def window(tmp_path, monkeypatch, caplog):
    """``window()`` opens the real window. Afterwards any run is stopped and the window is
    closed, and no button, timer or page may have raised an error on the way."""
    import tkinter as tk
    from tesseract_win.gui import app as appmod

    real_count = os.cpu_count() or 1
    monkeypatch.setattr(os, "cpu_count", lambda: min(MOST_THREADS, real_count))
    opened = []
    monkeypatch.setattr(osapi, "open_path", lambda path: bool(opened.append(path)) or os.path.exists(path))
    caplog.set_level(logging.WARNING, logger="tesseract_win.gui")
    made, faults = [], []

    def make(**options):
        root = tk.Tk(screenName=DISPLAY or None)
        options.setdefault("store", settings_store.SettingsStore(str(tmp_path / "settings" / "settings.json")))
        options.setdefault("scale", 1.0)
        options.setdefault("dark", False)
        options.setdefault("high_contrast", False)
        shell = appmod.App(root, **options)
        root.report_callback_exception = lambda *error: faults.append(error)
        shell.opened = opened
        made.append(shell)
        assert shell.settle(20), "the window did not come to rest after opening"
        return shell

    yield make
    for shell in made:
        if shell.closed:
            continue
        page = shell.run_page
        if page.is_running():                        # a test failed half-way: end the engines, ask nothing
            from tesseract_win.model import StopMode
            page._request_stop(StopMode.NOW)
            page._wait_for_end(30)
        shell.close()
    assert not faults, "an error in a button or a timer of the window: %r" % (faults[:1],)
    errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert not errors, "the window logged an error: %r" % (errors[:3],)


# =========================================================================== helpers

def pump(shell, until, timeout, what):
    """Run the window's event loop until ``until()`` is true."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        shell.root.update()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError("after %d seconds: %s (statuses: %r)" % (timeout, what, shell.run_page.statuses()))


def wait_for_end(shell, timeout=240):
    pump(shell, lambda: shell.state.running, 10, "the run did not begin")
    pump(shell, lambda: not shell.run_page.is_running() and not shell.state.running, timeout,
         "the batch did not end")
    assert shell.settle(20)                          # the look at the results folder that follows a run


def add(shell, *paths):
    shell.samples_page.add_paths(paths)
    assert shell.settle(30), "the window did not come to rest after adding files"


def start(shell):
    status = shell.start_status()
    assert status.enabled, "Start is disabled: %s" % status.reason
    shell.bottom.start.invoke()                      # the one Start button, pressed
    assert shell.step == 3, "Start did not switch to the Run step"


def outcome(shell, name):
    return shell.run_page._rows[name].outcome


def answer_question(shell, key=None, seen=None):
    """When a question opens on the window, note what it says and press ``key`` (None: the
    Enter key, which is its default button)."""
    from tesseract_win.gui import widgets as w

    def look():
        for child in shell.root.winfo_children():
            if isinstance(child, w.Dialog) and child.winfo_exists() and child.winfo_viewable():
                if seen is not None:
                    seen.append({"title": child.title(), "body": str(child.body.cget("text")),
                                 "default": child.default, "cancel": child.cancel,
                                 "buttons": {k: str(b.cget("text")) for k, b in child.buttons.items()},
                                 "focus": next((k for k, b in child.buttons.items() if b is child.focus_lastfor()), None)})
                if key is None:
                    child.invoke(child.default)
                else:
                    child.invoke(key)
                return
        shell.root.after(40, look)

    shell.root.after(40, look)


def broken_copy(example_reads, folder, name="Cut_short"):
    """The example's two files under another name, the second cut off at two thirds: what a
    copy looks like that was interrupted. The start of the file is intact, so the quick
    look at adding time finds nothing; the full check before trimming must."""
    os.makedirs(folder, exist_ok=True)
    first = os.path.join(folder, "%s_R1.fastq.gz" % name)
    second = os.path.join(folder, "%s_R2.fastq.gz" % name)
    shutil.copyfile(os.path.join(example_reads, EXAMPLE + "_R1.fastq.gz"), first)
    source = os.path.join(example_reads, EXAMPLE + "_R2.fastq.gz")
    with open(source, "rb") as handle:
        data = handle.read()
    with open(second, "wb") as handle:
        handle.write(data[:len(data) * 2 // 3])
    return first, second


def d1_files(results_dir, name, trimmed=True):
    """The files decision D1 promises for a finished sample, as paths that must exist."""
    sample = os.path.join(results_dir, name)
    wanted = [
        os.path.join(results_dir, "run_summary.html"), os.path.join(results_dir, "run_summary.tsv"),
        os.path.join(results_dir, "run_log.txt"), os.path.join(results_dir, "last_run.json"),
        os.path.join(results_dir, "assemblies", name + ".fasta"),
        os.path.join(sample, "sample_status.json"),
        os.path.join(sample, "assembly", "contigs.fasta"), os.path.join(sample, "assembly", "report.html"),
        os.path.join(sample, "assembly", "SUMMARY.txt"), os.path.join(sample, "assembly", "assembly_stats.tsv"),
        os.path.join(sample, "assembly", "preflight.txt"), os.path.join(sample, "assembly", "assembly.log"),
    ]
    if trimmed:
        wanted += [os.path.join(sample, "trimming", leaf) for leaf in (
            name + "_trimmed_R1.fastq.gz", name + "_trimmed_R2.fastq.gz", "trimming_report.html",
            "trimming_report.json", "trimming.log")]
    return wanted


# =========================================================================== the example, start to finish

def test_try_the_example_start_and_see_the_result(window, example_reads, monkeypatch):
    monkeypatch.setattr(appdirs, "example_dir", lambda: example_reads)
    shell = window()
    assert shell.state.engine_problem == ""          # both engines are there and start
    assert shell.bottom.start.instate(["disabled"]) and shell.bottom.message_text == "Add samples to begin."

    shell.samples_page.try_example()                 # the link in the empty list
    assert shell.settle(30)
    sample = shell.state.samples[0]
    assert [s.name for s in shell.state.samples] == [EXAMPLE]
    assert shell.state.view(sample)[1] == ""         # no warning on the practice sample
    assert shell.samples_page.notices.text("example").startswith("This is a small practice sample.")
    results_dir = shell.state.results_dir
    assert results_dir == os.path.join(example_reads, "results")
    assert not os.path.exists(results_dir)           # looking at a folder does not make it
    assert str(shell.bottom.start.cget("text")) == "Start (1 sample)"
    assert shell.stepper.sub("samples") == "1 ready"
    # the practice isolate is tiny: the genome size is ticked for it, where the user can see it
    assert shell.state.assembly.know_genome_size and shell.state.assembly.genome_size_mb == 0.2
    assert shell.stepper.sub("assembly") == "changed"

    start(shell)
    assert shell.stepper.locked                      # steps 1 to 3 cannot be reached during a run
    seen = set()
    pump(shell, lambda: seen.add(shell.run_page.status_of(EXAMPLE)) or not shell.run_page.is_running(), 240,
         "the example did not finish")
    assert shell.settle(20)
    assert RunStatus.TRIMMING in seen and RunStatus.ASSEMBLING in seen      # it was watched doing both

    page = shell.run_page
    assert page.end_reason is EndReason.COMPLETED
    assert page.statuses() == {EXAMPLE: RunStatus.DONE}
    result = outcome(shell, EXAMPLE)
    assert result.contigs == 2 and result.total_length == 185200 and result.trimmed
    assert page.end_counts == {"done": 1}
    assert str(page.result_heading.cget("text")).startswith("Finished at ")
    assert "assemblies" in str(page.result_where.cget("text")) and "1 file" in str(page.result_where.cget("text"))
    assert page.shown_end_actions == ["new_run", "back_to_samples"]
    assert shell.root.title() == "Finished: 1 done - TesserACT-Win"
    assert shell.stepper.sub("run") == "finished" and not shell.stepper.locked
    assert not shell.state.running

    missing = [path for path in d1_files(results_dir, EXAMPLE) if not os.path.isfile(path)]
    assert not missing, "decision D1 promises these files: %r" % missing
    assert not os.path.exists(os.path.join(results_dir, EXAMPLE, "_in_progress"))
    with open(os.path.join(results_dir, "assemblies", EXAMPLE + ".fasta")) as handle:
        assert sum(1 for line in handle if line.startswith(">")) == 2

    # the row can be read and opened without knowing about double-clicks
    page.select_sample(EXAMPLE)
    assert page.strip_buttons["open_report"].winfo_manager() and page.strip_buttons["open_folder"].winfo_manager()
    page.strip_buttons["open_report"].invoke()
    page.open_summary_button.invoke()
    page.open_results_button.invoke()
    assert shell.opened == [results.report_html_path(results_dir, EXAMPLE),
                            results.summary_html_path(results_dir), results_dir]
    with open(results.summary_html_path(results_dir), encoding="utf-8") as handle:
        assert EXAMPLE in handle.read()

    # "New run" empties the list, and the practice settings go with the practice sample
    page.end_buttons["new_run"].invoke()
    assert shell.settle(10)
    assert shell.step == 0 and shell.state.samples == []
    assert not shell.state.assembly.know_genome_size and shell.stepper.sub("assembly") == "standard"
    assert shell.root.title().startswith("TesserACT-Win")


# =========================================================================== Stop now, then Continue

def test_stop_now_in_mid_assembly_then_continue(window, example_reads, tmp_path):
    shell = window()
    add(shell, example_reads)
    shell.samples_page.set_results_folder(str(tmp_path / "results"), by_user=True)
    assert shell.settle(20)
    results_dir = shell.state.results_dir
    start(shell)
    page = shell.run_page
    pump(shell, lambda: page.status_of(EXAMPLE) is RunStatus.ASSEMBLING, 120, "the assembly did not begin")

    asked = []

    def answer():
        question = page.open_question
        if question is None or not question.is_open:
            shell.root.after(30, answer)
            return
        dialog = question.dialog
        asked.append({"title": dialog.title(), "body": str(dialog.body.cget("text")), "default": dialog.default,
                      "buttons": [str(dialog.buttons[key].cget("text")) for key in dialog.buttons]})
        question.invoke("now")

    shell.root.after(30, answer)
    page.stop_button.invoke()                        # asks first; the run goes on underneath
    assert asked, "the Stop question did not open"
    assert asked[0]["title"] == "Stop the run?" and asked[0]["default"] == "keep"
    assert asked[0]["buttons"] == ["Keep running", "Stop after %s" % EXAMPLE, "Stop now"]
    assert "%s is being assembled" % EXAMPLE in asked[0]["body"]
    assert "that work is thrown away" in asked[0]["body"]

    pump(shell, lambda: not page.is_running(), 60, "Stop now did not end the run")
    assert shell.settle(20)
    assert page.end_reason is EndReason.STOPPED
    assert page.statuses() == {EXAMPLE: RunStatus.STOPPED}
    assert page.shown_end_actions[0] == "continue"
    assert texts.NOTHING_LOST in (str(page.result_heading.cget("text")) + " " + str(page.result_body.cget("text")))
    assert shell.root.title() == "Stopped: 0 of 1 done - TesserACT-Win"
    assert shell.stepper.sub("run") == "stopped" and not shell.stepper.locked
    # nothing that looks finished: no status file, no assembly, no file among the assemblies
    assert results.read_status(results_dir, EXAMPLE) is None or not results.read_status(results_dir, EXAMPLE).assembled
    assert not os.path.exists(os.path.join(results_dir, EXAMPLE, "assembly", "contigs.fasta"))
    assert not os.path.exists(os.path.join(results_dir, "assemblies", EXAMPLE + ".fasta"))

    # Continue asks nothing and finishes the sample
    page.end_buttons["continue"].invoke()
    wait_for_end(shell)
    assert page.end_reason is EndReason.COMPLETED
    assert page.status_of(EXAMPLE) in (RunStatus.DONE, RunStatus.DONE_NOTES)
    assert outcome(shell, EXAMPLE).contigs == 2
    missing = [path for path in d1_files(results_dir, EXAMPLE) if not os.path.isfile(path)]
    assert not missing, "decision D1 promises these files: %r" % missing
    assert not os.path.exists(os.path.join(results_dir, EXAMPLE, "_in_progress"))


# =========================================================================== a broken file, then Start again

def test_a_broken_read_file_fails_alone_and_a_second_start_asks(window, example_reads, tmp_path):
    reads = str(tmp_path / "reads")
    broken_copy(example_reads, reads, "Cut_short")
    for mate in ("_R1.fastq.gz", "_R2.fastq.gz"):
        shutil.copyfile(os.path.join(example_reads, EXAMPLE + mate), os.path.join(reads, "Good_one" + mate))

    shell = window()
    add(shell, reads)
    names = [s.name for s in shell.state.samples]
    assert names == ["Cut_short", "Good_one"]
    assert all(s.ready for s in shell.state.samples)              # the quick look cannot see the cut
    shell.samples_page.set_results_folder(str(tmp_path / "results"), by_user=True)
    shell.state.assembly.know_genome_size, shell.state.assembly.genome_size_mb = True, 0.2
    assert shell.settle(20)
    results_dir = shell.state.results_dir
    assert str(shell.bottom.start.cget("text")) == "Start (2 samples)"

    start(shell)
    wait_for_end(shell)
    page = shell.run_page
    assert page.end_reason is EndReason.COMPLETED                  # one sample's failure does not stop the batch
    assert page.statuses() == {"Cut_short": RunStatus.FAILED, "Good_one": RunStatus.DONE}
    failed = outcome(shell, "Cut_short")
    assert "incomplete or damaged" in failed.note and "Cut_short_R2.fastq.gz" in failed.note
    assert "Copy it again" in (failed.note + " " + failed.next_step)
    assert outcome(shell, "Good_one").contigs == 2
    assert page.end_counts == {"failed": 1, "done": 1}
    assert page.shown_end_actions == ["retry_failed", "new_run", "back_to_samples"]
    assert str(page.end_buttons["retry_failed"].cget("text")) == "Try the failed sample again"
    assert str(page.result_hint.cget("text")) == "1 sample failed. Select its row to see why and what to do."
    assert shell.root.title() == "Finished: 1 done, 1 failed - TesserACT-Win"
    # the failed row is selected, its reason stands in the strip, and the buttons are Open log, Open folder
    assert page._selected_name() == "Cut_short"
    assert "incomplete or damaged" in str(page.strip_text.cget("text"))
    assert page.strip_buttons["open_log"].winfo_manager() and not page.strip_buttons["open_report"].winfo_manager()
    assert not os.path.exists(os.path.join(results_dir, "Cut_short", "assembly"))
    assert not os.path.exists(os.path.join(results_dir, "assemblies", "Cut_short.fasta"))
    assert os.path.isfile(os.path.join(results_dir, "assemblies", "Good_one.fasta"))

    # ---- back to the samples; the folder now says what it holds
    page.end_buttons["back_to_samples"].invoke()
    assert shell.settle(20)
    assert shell.step == 0 and len(shell.state.samples) == 2
    notes = str(shell.samples_page.folder_notes.cget("text"))
    assert "1 of these samples already has results here." in notes
    assert "1 failed last time and will be tried again." in notes

    # ---- a second Start: the question, once, with Skip as the default button
    seen = []
    answer_question(shell, None, seen)                             # Enter: whatever the default is
    start(shell)
    assert len(seen) == 1, "the overwrite-or-skip question did not open"
    question = seen[0]
    assert question["title"] == "Some samples are already assembled"
    assert question["body"].startswith("1 of the 2 samples already has an assembly in this results folder.")
    assert question["default"] == "skip" and question["cancel"] == "cancel" and question["focus"] == "skip"
    assert list(question["buttons"]) == ["skip", "overwrite", "cancel"]
    wait_for_end(shell)
    assert page.statuses() == {"Cut_short": RunStatus.FAILED, "Good_one": RunStatus.SKIPPED}
    assert page.table.value(page._rows["Good_one"].row_id, "status") == "Skipped (already assembled)"
    assert page._rows["Good_one"].note.startswith("Already assembled on ")      # never passes for "done now"
    again = outcome(shell, "Cut_short")
    assert "incomplete or damaged" in again.note
    assert "second time" in (again.note + " " + again.next_step)   # audit row 9.12
    assert os.path.isfile(os.path.join(results_dir, "assemblies", "Good_one.fasta"))

    # ---- and Cancel starts nothing
    page.end_buttons["back_to_samples"].invoke()
    assert shell.settle(20)
    seen = []
    answer_question(shell, "cancel", seen)
    shell.bottom.start.invoke()
    assert len(seen) == 1 and shell.step == 0 and not shell.state.running
