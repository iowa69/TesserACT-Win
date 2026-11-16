#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Photograph the real window on its way through the four steps.

    python tools/ci_screenshots.py --out DIR            light palette
    python tools/ci_screenshots.py --out DIR --dark     dark palette

It opens the window of the program itself (the same shell and the same four pages a user
gets), fills it with made-up samples and plays a made-up batch with the demonstration
runner, so it needs no engines, no read files and no results folder. It saves numbered
PNG files: the empty list, the list with samples, the Trimming and Assembly steps with and
without "More options", the overwrite-or-skip question, the Run step while it trims and
while it assembles, the Stop question, "Stopping after ...", a stopped run, the question
on closing the window, the finished run with its failures, the log, and the About box.

Where it runs:

- Linux, under a virtual display (``Xvfb``, with ``DISPLAY`` set);
- a Windows GitHub runner, which has a desktop session. There the picture is taken with
  ``PIL.ImageGrab`` of the window's own rectangle only, never of the whole screen.

More, for looking at the window while it is being built:

    --scale 1.5            as on a 150 % screen (1.0 is 96 dpi; default: what the screen says)
    --screen 1366x768      pretend the screen has this size (the window sizes itself to it)
    --all                  further states: problems with the list and the results folder,
                           the red and yellow bars, an interrupted run, 120 samples
    --tag NAME             put NAME in the file names (default: light or dark, plus the
                           scale and the screen when they were given)

Pillow is needed by this tool only; the program never imports it. Exit code 0 when every
picture was saved, 1 when the walk got stuck (the message says where).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from typing import Any, Callable, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tesseract_win import osapi                                              # noqa: E402
from tesseract_win.gui import app as appmod                                  # noqa: E402
from tesseract_win.gui import demo, dialogs, run_dialogs, texts              # noqa: E402
from tesseract_win.gui import state as st                                    # noqa: E402
from tesseract_win.gui import widgets as w                                   # noqa: E402
from tesseract_win.model import (EndReason, ExistingResult, Notice, RunConfig, RunStatus,   # noqa: E402
                                 Sample, SampleState, Stage)

RESULTS = "D:\\Sequencing\\TesserACT_results"
LONG_NAME = "Klebsiella_pneumoniae_ward7_isolate_2026_0041_rerun"
# Seconds of the pretend run per real second: hurrying to a state that lasts, walking to a
# moment that passes (so that the window, which looks ten times a second, cannot miss it),
# and holding nearly still while a picture is taken.
FAST, WALK, SLOW = 400.0, 50.0, 0.4


class Stuck(Exception):
    """The walk waited for something that did not happen."""


class Clock:
    """The clock of the pretend run. Its pace can be changed while it runs, so the walk can
    hurry to a moment and then hold nearly still while the picture is taken."""

    def __init__(self, speed: float = FAST):
        self._lock = threading.Lock()
        self._speed = float(speed)
        self._virtual = 0.0
        self._real = time.monotonic()

    def _advance(self) -> None:
        real = time.monotonic()
        self._virtual += (real - self._real) * self._speed
        self._real = real

    def now(self) -> float:
        with self._lock:
            self._advance()
            return self._virtual

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, value: float) -> None:
        with self._lock:
            self._advance()
            self._speed = max(0.001, float(value))


def plans() -> dict:
    """How each made-up sample of ``state.demo_samples`` goes: five finish, one of them with
    a note; one has a damaged read file; one has too little data."""
    return {
        "KP_001": demo.Plan(contigs=64, total_length=5_412_208, largest=612_480, n50=221_480, depth=84),
        "KP_002": demo.Plan(contigs=71, total_length=5_388_917, largest=540_112, n50=198_204, depth=96),
        "KP_003": demo.Plan(
            status=RunStatus.DONE_NOTES, contigs=312, total_length=5_391_027, largest=141_208, n50=38_412, depth=22,
            note="Low depth (about 22x): expect a fragmented assembly.",
            notes=("Low depth (about 22x): expect a fragmented assembly.",)),
        LONG_NAME: demo.Plan(contigs=58, total_length=5_602_331, largest=701_554, n50=262_190, depth=131),
        "SA_17": demo.Plan(contigs=39, total_length=2_806_114, largest=498_730, n50=186_902, depth=74),
        "KP_007": demo.Plan(
            status=RunStatus.FAILED, fail_at=Stage.CHECK_READS, trim_seconds=60.0, check_seconds=30.0,
            note="A read file is incomplete or damaged (KP_007_S7_L001_R2_001.fastq.gz). It may have been "
                 "cut short while copying or downloading.",
            next_step="Copy it again from the original, then try again."),
        "PA_12": demo.Plan(
            status=RunStatus.FAILED, fail_at=Stage.ASSEMBLE, trim_seconds=20.0, check_seconds=12.0,
            assemble_seconds=90.0, note="Too little data to assemble: no contigs were produced.",
            next_step="This sample needs more sequencing."),
    }


# =========================================================================== the camera

def _frame_rectangle(window: Any) -> Optional[Tuple[int, int, int, int]]:
    """On Windows: the window with its title bar, as the screen shows it. None elsewhere."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        handle = int(window.wm_frame(), 16)
        rect = wintypes.RECT()
        dwm_extended_frame_bounds = 9
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(handle), dwm_extended_frame_bounds,
                                                      ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect)):
                return None
        if rect.right - rect.left < 50 or rect.bottom - rect.top < 50:
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return None


def rectangle(window: Any) -> Tuple[int, int, int, int]:
    """Left, top, right and bottom of a window on the screen, in pixels."""
    framed = _frame_rectangle(window)
    if framed is not None:
        return framed
    left, top = window.winfo_rootx(), window.winfo_rooty()
    return left, top, left + window.winfo_width(), top + window.winfo_height()


def grab(box: Tuple[int, int, int, int]) -> Any:
    """A picture of that rectangle of the screen and of nothing else."""
    from PIL import ImageGrab                       # a tool-only dependency
    left, top, right, bottom = box
    if sys.platform.startswith("linux"):
        full = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY") or None)
        return full.crop((max(0, left), max(0, top), min(full.width, right), min(full.height, bottom)))
    return ImageGrab.grab(bbox=(max(0, left), max(0, top), right, bottom))


# =========================================================================== the walk

class Walk:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out = os.path.abspath(args.out)
        os.makedirs(self.out, exist_ok=True)
        self.number = 0
        self.saved: List[str] = []
        self.clock = Clock(FAST)
        screen = None
        if args.screen:
            wide, high = (int(part) for part in args.screen.lower().split("x", 1))
            screen = (wide, high)
        parts = ["dark" if args.dark else "light"]
        if args.scale:
            parts.append("%d" % round(args.scale * 100))
        if screen:
            parts.append("%dx%d" % screen)
        self.tag = args.tag if args.tag is not None else "_".join(parts)
        self.app = appmod.App(tk.Tk(), dark=bool(args.dark), scale=args.scale, demo=True, screen=screen,
                              high_contrast=False if args.scale else None)
        self.root = self.app.root
        self.page = self.app.run_page
        self.page.clock = self.clock.now
        self.page.preflight = lambda _config: []
        script = demo.Scenario("pictures", RunConfig(results_dir=RESULTS, samples=[]), plans())
        self.script = script
        self.app.runner_factory = lambda config, events, **options: demo.DemoRunner(
            config, events, script=self.script, clock=self.clock, **options)
        self.faults: List[Any] = []
        self.root.report_callback_exception = lambda *error: self.faults.append(error)
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)   # nothing of the desktop may cover it in a picture
            self.root.focus_force()
        except tk.TclError:
            pass
        self.settle()

    # -- waiting
    def settle(self, seconds: float = 0.25) -> None:
        self.app.settle(4)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def wait(self, until: Callable[[], Any], what: str, timeout: float = 90.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.root.update()
            if until():
                return
            time.sleep(0.01)
        raise Stuck("waited %d seconds for: %s" % (timeout, what))

    def calm(self) -> None:
        """Mouse out of the way, no tooltip, the help line at rest."""
        self.settle(0.1)
        try:
            self.root.event_generate("<Motion>", warp=True, x=3, y=3)
        except tk.TclError:
            pass
        w.hide_tooltips(self.root)
        self.app.help_line.clear()
        self.settle(0.15)

    # -- pictures
    def shoot(self, name: str, *others: Any) -> str:
        """Save a picture of the window (and of the question standing on it)."""
        self.settle(0.3)
        boxes = [rectangle(self.root)] + [rectangle(other) for other in others if other is not None]
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        self.number += 1
        path = os.path.join(self.out, "%02d_%s_%s.png" % (self.number, name, self.tag))
        grab(box).save(path)
        self.saved.append(path)
        print("saved %s" % path, flush=True)
        return path

    def question(self, name: str, made: Any) -> None:
        """Open a question on the window, photograph it, close it."""
        made.open()
        self.shoot(name, made)
        made.destroy()
        self.settle(0.1)

    # -- the run
    def status(self, sample: str) -> RunStatus:
        return self.page.status_of(sample)

    def step_text(self) -> str:
        return str(self.page.step_line.words.cget("text"))

    def hurry_to(self, until: Callable[[], Any], what: str, speed: float = FAST) -> None:
        self.clock.speed = speed
        self.wait(until, what)
        self.clock.speed = SLOW
        self.settle(0.3)

    def assembling(self, sample: str, words: str) -> None:
        """Go to the moment at which ``sample`` is being assembled and the step line holds ``words``."""
        self.clock.speed = FAST
        self.wait(lambda: self.status(sample) is RunStatus.ASSEMBLING, "%s to be assembled" % sample)
        self.hurry_to(lambda: self.status(sample) is not RunStatus.ASSEMBLING or words in self.step_text(),
                      "%s in the step line of %s" % (words, sample), WALK)
        if words not in self.step_text():
            raise Stuck("%s was assembled without ever showing: %s" % (sample, words))

    def answer_stop(self, name: str, key: str) -> None:
        """Press Stop; when the question stands, photograph it and press ``key``."""
        def answer() -> None:
            asked = self.page.open_question
            if asked is None or not asked.is_open:
                self.root.after(40, answer)
                return
            self.shoot(name, asked.dialog)
            asked.invoke(key)

        self.root.after(60, answer)
        self.page.stop_button.invoke()               # waits here until the question is answered
        self.settle(0.2)

    # ------------------------------------------------------------------ steps 1 to 3
    def samples_step(self) -> None:
        app = self.app
        app.samples_page.clear_list()
        self.calm()
        self.shoot("samples_empty")
        app.samples_page.add_samples(st.demo_samples())
        app.samples_page.set_results_folder(RESULTS, suggested=True)
        self.calm()
        self.shoot("samples")
        app.show_left_out()                          # the link beside Start: "1 left out: why?"
        app.samples_page.table.tree.focus_force()
        self.settle(0.3)
        self.shoot("samples_left_out")
        app.samples_page.table.select([])
        self.calm()

    def options_steps(self) -> None:
        app = self.app
        app.show_step(1)
        self.calm()
        self.shoot("trimming")
        app.trimming_page.open_more()
        self.calm()
        self.shoot("trimming_more_options")
        app.trimming_page.open_more(False)
        app.show_step(2)
        self.calm()
        self.shoot("assembly")
        app.assembly_page.open_more()
        self.calm()
        self.shoot("assembly_more_options")
        app.assembly_page.open_more(False)
        app.show_step(0)
        self.calm()

    def overwrite_question(self) -> None:
        when = "2026-11-03T10:15:00"
        existing = {"KP_001": ExistingResult("KP_001", when, True, False, "not trimmed; mode Careful"),
                    "KP_003": ExistingResult("KP_003", when, True, True),
                    "SA_17": ExistingResult("SA_17", when, True, True)}
        asked = dialogs.overwrite_question(existing, len(self.app.state.ready()))
        self.question("overwrite_question", w.Dialog(
            self.root, title=asked.title, body=asked.body, choices=asked.choices, default=asked.default,
            cancel=asked.cancel, details=asked.details, details_label=asked.details_label, level="warn"))

    # ------------------------------------------------------------------ step 4
    def run_step(self) -> None:
        app, page = self.app, self.page
        self.clock.speed = SLOW
        app.bottom.start.invoke()                    # the one Start button
        if app.step != 3 or not page.is_running():
            raise Stuck("Start did not begin the run: %s" % app.bottom.message_text)
        self.calm()
        self.shoot("run_starting")
        self.hurry_to(lambda: self.status("KP_001") is RunStatus.TRIMMING and "cleaning" in self.step_text(),
                      "the first sample to be trimmed", WALK / 2)
        self.shoot("run_trimming")
        self.assembling("KP_002", "k = 55")
        page.select_sample("KP_001")                 # a finished row: its note, Open report, Open folder
        self.settle(0.3)
        self.shoot("run_assembling")
        self.answer_stop("stop_question", "after_current")
        self.shoot("run_stopping_after")
        page.keep_going_button.invoke()              # "Keep going after all"
        page.table.select([])
        self.assembling("SA_17", "Step 4")
        page.select_sample("KP_003")                 # "Done (check notes)"
        self.settle(0.3)
        self.shoot("run_check_notes")
        page.table.select([])
        self.answer_stop("stop_now_question", "now")
        self.clock.speed = FAST
        self.wait(lambda: not page.is_running(), "Stop now to end the run")
        self.calm()
        self.shoot("run_stopped")
        self.clock.speed = FAST
        page.end_buttons["continue"].invoke()
        self.wait(page.is_running, "Continue to begin")
        self.assembling("SA_17", "Step 2")

        def close_asked() -> None:
            asked = page.open_question
            if asked is None or not asked.is_open:
                self.root.after(40, close_asked)
                return
            self.shoot("close_question", asked.dialog)
            asked.invoke("keep")

        self.root.after(60, close_asked)
        if app.close():                              # the close box, during a run: it asks first
            raise Stuck("the window closed during a run without asking")
        self.clock.speed = FAST
        self.wait(lambda: not page.is_running(), "the batch to finish")
        self.calm()
        self.shoot("run_finished")
        page.log_toggle.invoke()
        self.calm()
        self.shoot("run_finished_log")
        page.log_toggle.invoke()
        self.settle(0.1)

    def about_box(self) -> None:
        self.question("about", w.Dialog(
            self.root, title=st.Words.ABOUT_TITLE, body=dialogs.about_body(), wrap=460,
            choices=dialogs.about_choices(), default="close", cancel="close"))

    # ------------------------------------------------------------------ further states (--all)
    def further_states(self) -> None:
        app, page = self.app, self.page
        samples = app.samples_page
        page.end_buttons["new_run"].invoke()
        self.calm()

        # ---- the list: names that clash, a name being changed, nothing paired
        folder = "D:\\Sequencing\\"
        mb = 1024 ** 2
        from tesseract_win import pairing
        rows = [
            Sample("KP_001", [(folder + "Run A\\KP_001_R1.fastq.gz", folder + "Run A\\KP_001_R2.fastq.gz")], True, 412 * mb),
            Sample("KP_001", [(folder + "Run B\\KP_001_R1.fastq.gz", folder + "Run B\\KP_001_R2.fastq.gz")], True, 398 * mb),
            Sample("KP_004", [(folder + "Run A\\KP_004_R1.fastq.gz", folder + "Run A\\KP_004_R2.fastq.gz")], True, 455 * mb),
            Sample("EC_11", [(folder + "Run A\\EC_11_R1.fastq.gz", "")], True, 201 * mb, SampleState.LEFT_OUT,
                   pairing.NO_READ2),
        ]
        app.state.set_samples(rows)
        pairing.mark_name_clashes(app.state.samples)
        samples.list_changed()
        samples.set_results_folder(RESULTS, suggested=True)
        self.calm()
        self.shoot("samples_same_names")
        row = st.row_id_of(app.state.samples[2])
        samples.table.select([row])
        if samples.table.begin_rename(row):
            editor = samples.table.editor
            editor = editor() if callable(editor) else editor
            if editor is not None:
                editor.delete(0, "end")
                editor.insert(0, "KP 004 (rerun)")
                editor.event_generate("<KeyRelease>")
            self.settle(0.4)
            self.shoot("samples_renaming")
            samples.table._end_rename(False)         # Esc: the old name stays
        samples.make_names_different()
        samples.notices.show("none_paired", st.Words.NONE_PAIRED, "warn")
        self.calm()
        self.shoot("samples_names_made_different")
        samples.notices.dismiss("none_paired")

        # ---- the results folder: not enough room; a note; the red bar of a missing part
        verdict = st.demo_verdict
        gb = 1024 ** 3
        problem = (st.Words.RESULTS_NO_SPACE.format(need="61 GB", drive="drive D:", free="38 GB")
                   + st.Words.RESULTS_NO_SPACE_ADVICE_KEEP.format(less="6.0 GB"))
        app.state.fixed_verdict = lambda state: st.FolderVerdict(
            problem=problem, short=st.first_sentence(problem),
            info="%s %s" % (texts.RESULTS_FOLDER_NOTE, texts.space_line("61 GB", "D:", "38 GB")),
            need=61 * gb, free=38 * gb)
        app.state.notify("results")
        self.calm()
        self.shoot("samples_not_enough_space")
        app.state.fixed_verdict = lambda state: st.FolderVerdict(
            info="%s %s" % (texts.RESULTS_FOLDER_NOTE, texts.space_line("61 GB", "C:", "212 GB")),
            notes=[st.Words.RESULTS_SYNCED.format(name="OneDrive")], existing=texts.existing_line(12, 2),
            need=61 * gb, free=212 * gb)
        app.state.notify("results")
        app._engine_problem(texts.missing_part("engines\\tesseract-asm.exe"))
        self.calm()
        self.shoot("samples_part_missing")
        app._engine_problem("")
        app.state.fixed_verdict = verdict
        app.state.notify("results")

        # ---- the practice sample
        samples.clear_list()
        example = "C:\\Users\\Giovanni\\Downloads\\TesserACT-Win\\example\\"
        samples.add_samples([Sample("Example_isolate", [(example + "Example_isolate_R1.fastq.gz",
                                                         example + "Example_isolate_R2.fastq.gz")], True, 4 * mb + 300000)])
        samples.set_results_folder(example + "results", suggested=True)
        samples.notices.show("example", texts.EXAMPLE_NOTE, "info")
        app.state.assembly.know_genome_size, app.state.assembly.genome_size_mb = True, 0.2
        app.assembly_page.load()
        app.assembly_page.notice.show("example", texts.EXAMPLE_SIZE_NOTE, "info")
        app.state.notify("assembly")
        self.calm()
        self.shoot("samples_example")
        app.show_step(2)
        self.calm()
        self.shoot("assembly_changed")
        app.assembly_page.reset()
        app.show_step(0)
        samples.notices.dismiss("example")

        # ---- trimming: a number set back, own adapters, and no sample to trim
        samples.clear_list()
        samples.add_samples(st.demo_samples())
        samples.set_results_folder(RESULTS, suggested=True)
        app.show_step(1)
        trimming = app.trimming_page
        trimming.open_more()
        self.settle(0.2)
        trimming.set_value("own_adapters", True)
        trimming.load()
        field = trimming.numbers["end_quality"]
        field.focus_force()
        field.delete(0, "end")
        field.insert(0, "99")
        field.commit()
        self.settle(0.3)
        self.shoot("trimming_number_set_back")
        trimming.reset()
        trimming.open_more(False)
        app.show_step(0)
        samples.trim_all(False)
        app.show_step(1)
        self.calm()
        self.shoot("trimming_not_used")
        app.show_step(0)
        samples.trim_all(True)
        self.calm()

        # ---- the run: bars, the end forced by the PC, an interrupted run, many samples
        self.script = demo.Scenario(
            "bars", RunConfig(results_dir=RESULTS, samples=[]), plans(),
            ending=("environment", 2, 0.4, "Stopped: the results folder can no longer be reached (%s). "
                    "Reconnect the drive, then press Continue. The {finished} are safe." % "E:\\Results"),
            notices={0: Notice(kind="battery", text=texts.ON_BATTERY, level="warn"),
                     1: Notice(kind="memory", text=texts.low_memory("2.9 GB"), level="warn")})
        self.clock.speed = FAST
        app.bottom.start.invoke()
        self.assembling("KP_002", "Step 2")
        self.shoot("run_bars")
        self.clock.speed = FAST
        self.wait(lambda: not page.is_running(), "the PC to end the run")
        if page.end_reason is not EndReason.ENVIRONMENT:
            raise Stuck("the run did not end because of the PC")
        self.calm()
        self.shoot("run_ended_by_the_pc")

        page.end_buttons["new_run"].invoke()          # the list is empty, as at a new launch
        self.settle(0.2)
        left = [sample for sample in st.demo_samples() if sample.ready][3:]
        page.interrupted_finder = lambda _folder: demo.InterruptedDemo(
            config=RunConfig(results_dir=RESULTS, samples=left), finished=3, total=7,
            when=time.strftime("%Y-%m-%dT03:12:40"), cause="windows", missing=[])
        app.offer_interrupted(RESULTS)
        self.wait(lambda: page.has_offer() and app.step == 3, "the offer to continue an interrupted run")
        self.calm()
        self.shoot("run_interrupted_offer")
        page.notices.dismiss(run_dialogs.INTERRUPTED_KIND)

        app.show_step(0)
        samples.clear_list()
        many = [Sample("KP_%03d" % number, [(folder + "KP_%03d_R1.fastq.gz" % number,
                                             folder + "KP_%03d_R2.fastq.gz" % number)], True, 400 * mb)
                for number in range(1, 121)]
        samples.add_samples(many)
        samples.set_results_folder(RESULTS, suggested=True)
        self.calm()
        self.shoot("samples_many")
        self.script = demo.Scenario("many", RunConfig(results_dir=RESULTS, samples=[]), {
            "KP_041": plans()["KP_007"], "KP_017": plans()["KP_003"]})
        self.clock.speed = 4000.0
        app.bottom.start.invoke()
        self.wait(lambda: self.status("KP_055").is_final, "55 of 120 samples", 180)
        self.assembling("KP_057", "Step 3")
        self.shoot("run_many")
        self.clock.speed = 20000.0
        self.wait(lambda: not page.is_running(), "120 samples to finish", 240)
        self.calm()
        self.shoot("run_many_finished")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Photograph the window on its way through the four steps.")
    parser.add_argument("--out", required=True, metavar="DIR", help="folder for the PNG files (made if missing)")
    parser.add_argument("--dark", action="store_true", help="the dark palette")
    parser.add_argument("--scale", type=float, default=None, help="screen scaling: 1.0 is 96 dpi, 1.5 is 150 %%")
    parser.add_argument("--screen", default=None, metavar="WxH", help="pretend the screen has this size")
    parser.add_argument("--all", action="store_true", help="further states of every step")
    parser.add_argument("--tag", default=None, help="name put in the file names")
    args = parser.parse_args(argv)

    try:
        from PIL import ImageGrab                   # noqa: F401 - said early, in words
    except ImportError:
        print("ci_screenshots needs Pillow (pip install pillow). The program itself does not.", file=sys.stderr)
        return 1
    osapi.set_process_defaults()                    # as the program does: the screen's own scaling
    walk = Walk(args)
    code = 0
    try:
        walk.samples_step()
        walk.options_steps()
        walk.overwrite_question()
        walk.run_step()
        walk.about_box()
        if args.all:
            walk.further_states()
    except Stuck as stuck:
        print("ci_screenshots got stuck: %s" % stuck, file=sys.stderr)
        code = 1
    if walk.faults:
        print("ci_screenshots: an error in a button or a timer of the window: %r" % (walk.faults[0],),
              file=sys.stderr)
        code = 1
    print("%d pictures in %s" % (len(walk.saved), walk.out), flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)                                  # the pretend run is a thread: leave without waiting for it


if __name__ == "__main__":
    main()
