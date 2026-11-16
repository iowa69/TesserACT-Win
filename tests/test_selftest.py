# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""selftest and the entry point: the program checks itself, with and without a console."""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import types

import pytest

from tesseract_win import __main__ as entry
from tesseract_win import appdirs, branding, osapi, selftest, versions

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def have_engines() -> bool:
    try:
        appdirs.find_engines()
        return True
    except appdirs.EngineMissing:
        return False


needs_engines = pytest.mark.skipif(not have_engines(), reason="no built engines")


@pytest.fixture(scope="module")
def example(tmp_path_factory):
    """The example read set, made by the same script that makes the one that ships."""
    spec = importlib.util.spec_from_file_location("make_example_under_test",
                                                  os.path.join(REPO, "tools", "make_example.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = tmp_path_factory.mktemp("example")
    stdout = sys.stdout
    try:
        sys.stdout = open(os.devnull, "w")
        assert module.main(["make_example.py", str(folder)]) == 0
    finally:
        sys.stdout.close()
        sys.stdout = stdout
    return str(folder)


@pytest.fixture(autouse=True)
def private_log_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(appdirs, "log_dir", lambda: str(tmp_path / "logs"))
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_tesseract_win", False):
            root.removeHandler(handler)
            handler.close()


def lines_of(path):
    with open(path, "rb") as handle:
        data = handle.read()
    assert b"\r" not in data
    return data.decode("utf-8").splitlines()


# ============================================================================ the quick part

@needs_engines
@pytest.mark.engines
def test_the_quick_self_test_passes_and_writes_one_line_per_check(tmp_path, capsys):
    report = tmp_path / "report.txt"
    assert selftest.run_selftest(False, str(report), None) == 0
    lines = lines_of(report)
    assert lines[0] == "%s self-test" % branding.versions_line()
    checks = [line for line in lines if line.startswith(("PASS", "FAIL"))]
    assert [line.split("  ", 1)[1].split(":")[0] for line in checks] == [
        "engines found", "assembler answers", "trimmer answers", "detection sketch present", "drag and drop"]
    assert all(line.startswith("PASS  ") for line in checks)
    assert "TesserACT %s (expected version %s)" % (versions.TESSERACT_VERSION, versions.TESSERACT_VERSION) in checks[1]
    assert "scepter %s" % versions.SCEPTER_VERSION in checks[2]
    assert lines[-1] == "RESULT: PASS"
    assert capsys.readouterr().out.splitlines() == lines          # the same text on standard output


@needs_engines
@pytest.mark.engines
def test_it_works_without_a_standard_output_and_without_a_report_file(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)                       # the windowed Windows program
    monkeypatch.setattr(sys, "stderr", None)
    assert selftest.run_selftest(False, None, None) == 0


def test_missing_engines_fail_the_self_test_in_words(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_WIN_ENGINES", str(tmp_path / "nowhere"))
    monkeypatch.setattr(appdirs, "is_frozen", lambda: True)
    monkeypatch.setattr(appdirs, "app_dir", lambda: str(tmp_path / "app"))
    report = tmp_path / "sub" / "report.txt"
    assert selftest.run_selftest(True, str(report), str(tmp_path / "work")) == 1
    lines = lines_of(report)
    assert lines[2].startswith("FAIL  engines found: missing in ") and "tesseract-asm" in lines[2]
    assert any(line.startswith("FAIL  the example run ended: not tried") for line in lines)
    assert lines[-1] == "RESULT: FAIL (2)"


@needs_engines
@pytest.mark.engines
def test_a_wrong_engine_version_and_a_missing_sketch_are_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, "TESSERACT_VERSION", "9.9.9")
    real = appdirs.find_engines()
    monkeypatch.setattr(appdirs, "find_engines", lambda folder=None: type(real)(real.tesseract_asm, real.scepter, ""))
    report = tmp_path / "report.txt"
    assert selftest.run_selftest(False, str(report), None) == 1
    lines = lines_of(report)
    assert any(line.startswith("FAIL  assembler answers: TesserACT ") and "expected version 9.9.9" in line
               for line in lines)
    assert any(line.startswith("FAIL  detection sketch present") for line in lines)
    assert any(line.startswith("PASS  trimmer answers") for line in lines)


def test_a_report_that_cannot_be_written_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    blocker = tmp_path / "a file"
    blocker.write_text("x")
    monkeypatch.setattr(sys, "stdout", None)
    assert selftest.run_selftest(False, str(blocker / "report.txt"), None) == 1


def test_a_check_that_breaks_is_a_check_that_failed(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("no engines today")

    monkeypatch.setattr(appdirs, "find_engines", broken)
    report = tmp_path / "report.txt"
    assert selftest.run_selftest(False, str(report), None) == 1
    assert any(line.startswith("FAIL  the self-test ran to its end") and "no engines today" in line
               for line in lines_of(report))


# ============================================================================ the full part

@needs_engines
@pytest.mark.engines
@pytest.mark.slow
def test_the_full_self_test_passes_on_the_example(tmp_path, monkeypatch, example):
    monkeypatch.setattr(appdirs, "example_dir", lambda: example)
    report = tmp_path / "report.txt"
    work = tmp_path / "work folder"
    monkeypatch.setattr(sys, "stdout", None)
    assert selftest.run_selftest(True, str(report), str(work)) == 0, "\n".join(lines_of(report))
    lines = lines_of(report)
    assert lines[0].endswith("self-test (full)") and lines[-1] == "RESULT: PASS"
    checks = [line for line in lines if line.startswith(("PASS", "FAIL"))]
    assert len(checks) == 14 and all(line.startswith("PASS  ") for line in checks)
    assert any(line.startswith("PASS  the example sample is done: done, in ") for line in checks)
    assert "PASS  the assembly has two contigs: 2 (expected 2)" in checks
    assert "PASS  the assembly has 185,200 bases: 185,200 (expected 185,200)" in checks
    assert any(line.startswith("PASS  both contigs are circular: ") and line.count("_circular") == 2
               for line in checks)
    results_dir = work / "results"
    for name in ("run_summary.html", "run_summary.tsv", os.path.join("assemblies", "Example_isolate.fasta"),
                 os.path.join("Example_isolate", "trimming", "trimming_report.html"),
                 os.path.join("Example_isolate", "assembly", "report.html")):
        assert (results_dir / name).is_file(), name

    # A second run into the same folder replaces the result and passes again.
    assert selftest.run_selftest(True, str(report), str(work)) == 0


@needs_engines
@pytest.mark.engines
def test_the_full_self_test_fails_when_the_example_is_missing_or_is_not_the_example(tmp_path, monkeypatch):
    monkeypatch.setattr(appdirs, "example_dir", lambda: str(tmp_path / "no example"))
    report = tmp_path / "report.txt"
    assert selftest.run_selftest(True, str(report), str(tmp_path / "work")) == 1
    assert any(line.startswith("FAIL  example reads present: the example read file is missing")
               for line in lines_of(report))

    from .test_sample_run import make_reads
    other = tmp_path / "other example"
    make_reads(other, selftest.EXAMPLE_NAME, genome=30_000, depth=40)
    monkeypatch.setattr(appdirs, "example_dir", lambda: str(other))
    assert selftest.run_selftest(True, str(report), None) == 1       # a temporary work folder, deleted afterwards
    lines = lines_of(report)
    assert any(line.startswith("FAIL  the assembly has 185,200 bases: ") for line in lines)
    assert any(line.startswith("FAIL  both contigs are circular") for line in lines)
    assert any(line.startswith("PASS  run_summary.html written") for line in lines)
    assert lines[-1].startswith("RESULT: FAIL (")


# ============================================================================ the entry point

def test_version_prints_one_line(capsys):
    assert entry.main(["--version"]) == 0
    assert capsys.readouterr().out == branding.versions_line() + "\n"


def test_version_and_self_test_work_with_no_console_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    called = {}
    monkeypatch.setattr(selftest, "run_selftest", lambda full, report, work: called.update(
        full=full, report=report, work=work) or 0)
    assert entry.main(["--version"]) == 0
    assert sys.stdout is not None and sys.stderr is not None       # made safe, for whatever prints later
    assert entry.main(["--self-test", "--full", "--report", "r.txt", "--work=w dir"]) == 0
    assert called == {"full": True, "report": "r.txt", "work": "w dir"}
    assert entry.main(["--self-test", "--report"]) == 0
    assert called == {"full": False, "report": None, "work": None}


def test_the_self_test_path_never_imports_tk(tmp_path):
    code = ("import sys\n"
            "from tesseract_win.__main__ import main\n"
            "status = main(['--self-test'])\n"
            "bad = [m for m in sys.modules if m == 'tkinter' or m.startswith(('tkinter.', 'tesseract_win.gui.app'))]\n"
            "sys.exit(3 if bad else status)\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                          env=dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"),
                                   LOCALAPPDATA=str(tmp_path / "state")))
    assert done.returncode in (0, 1), done.stderr                  # 1 only where the engines are not built
    assert "RESULT: " in done.stdout and (done.returncode == 1 or "RESULT: PASS" in done.stdout)


def test_python_m_runs_the_entry_point():
    done = subprocess.run([sys.executable, "-m", "tesseract_win", "--version"], cwd=REPO, capture_output=True,
                          text=True)
    assert done.returncode == 0 and done.stdout.strip() == branding.versions_line()


@pytest.fixture
def fake_window(monkeypatch):
    """Stands in for ``tesseract_win.gui.app`` (written by another task) and for the Windows calls."""
    seen = {"order": []}
    module = types.ModuleType("tesseract_win.gui.app")

    def run_app(argv=None):
        seen["order"].append("window")
        seen["argv"] = argv
        return seen.get("status", 0)

    module.run_app = run_app
    monkeypatch.setitem(sys.modules, "tesseract_win.gui.app", module)
    import tesseract_win.gui as gui_package
    monkeypatch.setattr(gui_package, "app", module, raising=False)
    monkeypatch.setattr(osapi, "set_process_defaults", lambda: seen["order"].append("defaults") or {})
    released = []
    handle = osapi.InstanceHandle(lambda: released.append(True))
    monkeypatch.setattr(osapi, "single_instance", lambda name: seen["order"].append("single:%s" % name) or handle)
    seen["released"] = released
    hooks = (sys.excepthook, __import__("threading").excepthook, sys.unraisablehook)
    yield seen
    sys.excepthook, __import__("threading").excepthook, sys.unraisablehook = hooks


def test_the_window_path_sets_everything_up_in_order(fake_window, tmp_path):
    assert entry.main(["some", "argument"]) == 0
    assert fake_window["order"] == ["defaults", "single:%s" % branding.APP_NAME, "window"]
    assert fake_window["argv"] == ["some", "argument"]
    assert fake_window["released"] == [True]
    log_file = tmp_path / "logs" / entry.LOG_FILE_NAME
    assert "%s starting" % branding.versions_line() in log_file.read_text(encoding="utf-8")
    fake_window["status"] = 3
    assert entry.main([]) == 3


def test_errors_nobody_caught_end_in_the_log(fake_window, tmp_path):
    import threading

    def run_app(argv=None):
        thread = threading.Thread(target=lambda: 1 / 0, name="a-worker")
        thread.start()
        thread.join()
        try:
            raise ValueError("lost in a callback")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        logging.getLogger("tesseract_win.somewhere").warning("a remark with an accent: é")
        return 0

    sys.modules["tesseract_win.gui.app"].run_app = run_app
    assert entry.main([]) == 0
    text = (tmp_path / "logs" / entry.LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "ZeroDivisionError" in text and "a-worker" in text
    assert "ValueError: lost in a callback" in text and "é" in text


def test_a_window_that_cannot_open_gives_status_1_and_a_log_entry(fake_window, tmp_path):
    def run_app(argv=None):
        raise RuntimeError("no display")

    sys.modules["tesseract_win.gui.app"].run_app = run_app
    assert entry.main([]) == 1
    assert "RuntimeError: no display" in (tmp_path / "logs" / entry.LOG_FILE_NAME).read_text(encoding="utf-8")
    assert fake_window["released"] == [True]


def test_a_second_copy_brings_the_first_forward_and_ends(fake_window, monkeypatch, capsys):
    monkeypatch.setattr(osapi, "single_instance", lambda name: None)
    brought = []
    monkeypatch.setattr(osapi, "bring_other_instance_to_front", lambda title: brought.append(title) or True)
    assert entry.main([]) == 0
    assert brought == [branding.APP_NAME] and "window" not in fake_window["order"]
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(osapi, "bring_other_instance_to_front", lambda title: False)
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    assert entry.main([]) == 0
    assert capsys.readouterr().out.strip() == "%s is already open." % branding.APP_NAME


def test_a_log_folder_that_cannot_be_made_does_not_stop_the_program(fake_window, tmp_path, monkeypatch):
    blocker = tmp_path / "a file"
    blocker.write_text("x")
    monkeypatch.setattr(appdirs, "log_dir", lambda: str(blocker / "logs"))
    assert entry.main([]) == 0
    assert fake_window["order"][-1] == "window"


def test_a_broken_single_copy_check_lets_the_program_start(fake_window, monkeypatch):
    def broken(_name):
        raise OSError("no mutex")

    monkeypatch.setattr(osapi, "single_instance", broken)
    assert entry.main([]) == 0
    assert fake_window["order"][-1] == "window"


# ============================================================================ added by the review

def test_a_console_that_cannot_show_every_character_still_gets_the_report(tmp_path, monkeypatch):
    """A Windows console or a redirected output in an old code page, and a path with other letters."""
    import io
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", console)

    def no_engines():
        raise appdirs.EngineMissing(["scepter"], "D:\\Żółć 日本\\engines")

    monkeypatch.setattr(appdirs, "find_engines", no_engines)
    monkeypatch.chdir(tmp_path)
    assert selftest.run_selftest(False, "report é.txt", None) == 1        # a bare file name, with an accent
    console.flush()
    shown = raw.getvalue().decode("cp1252")
    assert "FAIL  engines found: missing in D:\\" in shown and shown.rstrip().endswith("RESULT: FAIL (1)")
    assert "\\u65e5" in shown                                              # replaced, not lost
    written = lines_of(tmp_path / "report é.txt")
    assert any("Żółć 日本" in line for line in written) and written[-1] == "RESULT: FAIL (1)"

    assert selftest.write_to_stream(None, "x") is False
    console.close()
    assert selftest.write_to_stream(console, "x") is False                 # closed: no exception

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii", errors="strict"))
    monkeypatch.setattr(branding, "versions_line", lambda: "TesserACT-Win — 1")
    assert entry.main(["--version"]) == 0
    sys.stdout.flush()
    assert raw.getvalue().decode("ascii").strip() == "TesserACT-Win \\u2014 1"


def test_the_self_test_also_switches_off_the_windows_error_boxes(monkeypatch):
    """Nobody is there to click on "the program has stopped working" when the self-test runs."""
    order = []
    monkeypatch.setattr(osapi, "set_process_defaults", lambda: order.append("defaults") or {})
    monkeypatch.setattr(selftest, "run_selftest", lambda full, report, work: order.append("self-test") or 0)
    hooks = (sys.excepthook, __import__("threading").excepthook, sys.unraisablehook)
    try:
        assert entry.main(["--self-test"]) == 0
        assert order == ["defaults", "self-test"]

        def broken():
            raise OSError("no such call")

        monkeypatch.setattr(osapi, "set_process_defaults", broken)
        assert entry.main(["--self-test"]) == 0 and order[-1] == "self-test"
    finally:
        sys.excepthook, __import__("threading").excepthook, sys.unraisablehook = hooks


def test_an_example_that_ends_with_notes_or_never_ends_fails_the_self_test(tmp_path, monkeypatch):
    """"Done (check notes)" on the example is not the known result; neither is no end at all."""
    import threading
    from tesseract_win import batch
    from tesseract_win.model import (BatchFinished, BatchStarted, EndReason, EnginePaths, RunStatus,
                                     SampleFinished, SampleOutcome)
    example = tmp_path / "example"
    example.mkdir()
    for tag in ("_R1", "_R2"):
        (example / (selftest.EXAMPLE_NAME + tag + ".fastq.gz")).write_bytes(b"x")
    monkeypatch.setattr(appdirs, "example_dir", lambda: str(example))
    monkeypatch.setattr(selftest, "_quick", lambda report: EnginePaths("asm", "scepter", "sketch"))
    seen = {}

    class Runner(threading.Thread):
        ends = True

        def __init__(self, config, events, **kwargs):
            super().__init__(daemon=True)
            self.config, self.events = config, events
            seen["config"] = config

        def request_stop(self, mode):
            seen["stopped"] = mode

        def run(self):
            self.events.put(BatchStarted(total=1, names=[selftest.EXAMPLE_NAME]))
            if self.ends:
                self.events.put(SampleFinished(outcome=SampleOutcome(
                    name=selftest.EXAMPLE_NAME, status=RunStatus.DONE_NOTES, note="Low depth (about 22x).",
                    next_step="More sequencing.")))
                self.events.put(BatchFinished(reason=EndReason.COMPLETED, message="Finished."))

    monkeypatch.setattr(batch, "BatchRunner", Runner)
    report = tmp_path / "report.txt"
    assert selftest.run_selftest(True, str(report), str(tmp_path / "work")) == 1
    lines = lines_of(report)
    assert any(line.startswith("FAIL  the example sample is done: done_notes (Low depth (about 22x). More "
                               "sequencing.)") for line in lines), lines
    assert "PASS  the example run ended: Finished." in lines
    assert seen["config"].assembly.threads <= 4 and seen["config"].results_dir == str(tmp_path / "work" / "results")

    Runner.ends = False
    monkeypatch.setattr(selftest, "_TIMEOUT", 0.5)
    assert selftest.run_selftest(True, str(report), str(tmp_path / "work")) == 1
    assert any(line.startswith("FAIL  the example run ended: no end after ") for line in lines_of(report))
    assert seen["stopped"].value == "now"
