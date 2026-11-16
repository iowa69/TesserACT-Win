# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The window: its shell around the four steps (decision D4, audit part B.2).

From top to bottom: the stepper (Samples, Trimming, Assembly, Run, each with its small
line: "40 ready", "standard" or "changed"), the page of the current step, the help line,
and the bottom bar with Back and Next as plain buttons and the one Start button, in the
same place on steps 1 to 3, carrying the count ("Start (38 samples)"). A disabled Start
has the reason beside it. Start is never the window's Enter key.

Pressing Start: the settings are checked (``settings_map.validate_*``), the read files are
looked for once more, the overwrite-or-skip question is asked only if
``results.existing_results`` finds finished samples, and then :meth:`App.start_run` is
called at once. There is no other confirmation. While a run is in progress steps 1 to 3
are locked.

:func:`run_app` is what ``tesseract_win/__main__.py`` calls, after it has made sure that
this is the only copy running (the claim can be made once per process, so it is not made
again here). ``run_app`` makes sure there is a log, refuses to run from inside a zip, and
opens the window. ``--demo`` (hidden) opens it with made-up samples, for screenshots.

Only the window's own thread touches Tk. Everything slow goes through ``App.tasks``.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import faulthandler
import fnmatch
import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
from typing import Any, Callable, Optional, Sequence, Tuple

from .. import appdirs, branding, osapi, results, settings_map
from ..model import EndReason, EnginePaths, Existing, RunConfig
from . import dialogs
from . import state as st
from . import texts
from . import theme as _theme
from . import widgets as w
from .settings_store import SettingsStore

__all__ = ["App", "run_app", "STEP_KEYS", "window_geometry", "running_title", "still_in_zip",
           "setup_logging"]

log = logging.getLogger(__name__)

#: The steps in order; ``show_step`` takes the index.
STEP_KEYS: Tuple[str, ...] = tuple(key for key, _name in texts.STEPS)
SAMPLES, TRIMMING, ASSEMBLY, RUN = 0, 1, 2, 3

# ===========================================================================================
# WORDS THAT ARE NOT IN texts.py YET (the others of the shell are in state.Words)
# ===========================================================================================
START_FAULT = ("The run could not be started because of a fault in the program. Nothing was "
               "changed. Close the program and start it again; if it happens again, see the "
               "log folder (About).")
START_FAULT_TITLE = "The run did not start"
FILES_GONE = "Some read files can no longer be found. Those samples are now left out: see the notes in the list."

#: Size of a new window, and the smallest it can be made, for a 96 dpi screen.
DEFAULT_SIZE = (1120, 720)
MINIMUM_SIZE = (860, 520)
#: What Windows itself takes from the height of the screen: taskbar, title bar and a margin.
TASKBAR, TITLE_BAR, MARGIN = 48, 32, 8
#: Below this height of screen (96 dpi units) the window saves room wherever it can. A
#: full-HD laptop at 150 % has 720 and is on the saving side: its window is 632 high, and
#: "More options" of the Trimming step fits there only with the ticks in two columns.
COMPACT_BELOW = 740


# =========================================================================== pure helpers

def window_geometry(screen_w: int, screen_h: int, scale: float,
                    saved: Optional[Tuple[int, int]] = None) -> Tuple[int, int, int, int, int, int]:
    """Size and place of the window: ``(width, height, x, y, min_width, min_height)`` in pixels.

    ``saved`` is a remembered size in 96 dpi units. The window never opens larger than the
    screen can show with the taskbar in place: on a 1366 x 768 laptop it fills the height at
    100 % and takes all there is at 150 %.
    """
    scale = max(1.0, float(scale))
    room_w = max(320, int(screen_w - 2 * MARGIN * scale))
    room_h = max(240, int(screen_h - (TASKBAR + TITLE_BAR + MARGIN) * scale))
    want_w, want_h = saved if saved else DEFAULT_SIZE
    min_w = min(int(MINIMUM_SIZE[0] * scale), room_w)
    min_h = min(int(MINIMUM_SIZE[1] * scale), room_h)
    width = max(min_w, min(int(want_w * scale), room_w))
    height = max(min_h, min(int(want_h * scale), room_h))
    x = max(0, (screen_w - width) // 2)
    usable = screen_h - int(TASKBAR * scale)
    y = max(0, (usable - height - int(TITLE_BAR * scale)) // 2)
    return width, height, x, y, min_w, min_h


def running_title(text: str = "") -> str:
    """ "Finished: 38 done, 2 failed - TesserACT-Win"; the usual title when there is no news."""
    text = (text or "").strip()
    return "%s - %s" % (text, branding.APP_NAME) if text else branding.window_title()


_ARCHIVE_FOLDERS = ("Temp1_*.zip", "7zO*", "7zS*", "Rar$*")


def still_in_zip(program: str, temp_dir: str) -> bool:
    """Audit C-16: was the program started from a temporary extraction of the zip?

    True when ``program`` lies inside the Temp folder, or inside a folder named the way
    archive tools name their temporary ones. Compared without regard to letter case and
    with short path names resolved.
    """
    try:
        exe = os.path.normcase(os.path.realpath(program))
        temp = os.path.normcase(os.path.realpath(temp_dir)) if temp_dir else ""
    except (OSError, ValueError):
        return False
    if temp and (exe == temp or exe.startswith(temp.rstrip("\\/") + os.sep)):
        return True
    parts = exe.replace("\\", "/").split("/")[:-1]
    for part in parts:
        if any(fnmatch.fnmatchcase(part, os.path.normcase(pattern)) for pattern in _ARCHIVE_FOLDERS):
            return True
    return False


_logging_ready = False
_fault_file: Any = None


def setup_logging() -> str:
    """Audit C-21: a windowed program has no console, so everything goes to a file.

    Gives ``sys.stdout`` and ``sys.stderr`` somewhere to write when they are None, adds a
    rotating log file (2 MB, 3 files, UTF-8) in ``appdirs.log_dir()`` and catches what would
    otherwise vanish: errors in any thread, and hard crashes. Safe to call twice. Returns
    the log folder ("" when no log file could be made; the program still runs).
    """
    global _logging_ready, _fault_file
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass
    folder = appdirs.log_dir()
    if _logging_ready:
        return folder
    _logging_ready = True
    package = logging.getLogger("tesseract_win")
    try:
        os.makedirs(folder, exist_ok=True)
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in package.handlers + logging.getLogger().handlers):
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(folder, "tesseract-win.log"), maxBytes=2 * 1024 * 1024, backupCount=3,
                encoding="utf-8", delay=True)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            package.addHandler(handler)
        if package.level == logging.NOTSET:
            package.setLevel(logging.INFO)
        if not faulthandler.is_enabled():
            try:
                _fault_file = open(os.path.join(folder, "crash.log"), "a", encoding="utf-8")
                faulthandler.enable(file=_fault_file)
            except (OSError, RuntimeError, ValueError):
                _fault_file = None
    except OSError:
        folder = ""

    def uncaught(kind: Any, value: Any, trace: Any) -> None:
        if issubclass(kind, KeyboardInterrupt):
            return
        log.critical("an error nobody caught", exc_info=(kind, value, trace))

    def uncaught_in_thread(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        log.critical("an error nobody caught in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    if sys.excepthook is sys.__excepthook__:               # the program's entry point may have set its own
        sys.excepthook = uncaught
    if threading.excepthook is threading.__excepthook__:
        threading.excepthook = uncaught_in_thread
    return folder


class _MissingRunPage(w.Frame):
    """Shown in place of the Run page if that part of the program cannot be loaded."""

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, ground="page")
        w.WrapLabel(self, st.Words.RUN_PAGE_MISSING, "bad", wrap=640).pack(fill="x", pady=_theme.px(24, self))
        self.app = app
        self.end_reason: Optional[EndReason] = None

    def begin(self, config: RunConfig, left_out: Sequence[Tuple[str, str]], *, resume: bool = False,
              runner_factory: Any = None) -> None:
        self.after(10, self.app.run_ended)

    def is_running(self) -> bool:
        return False

    def confirm_close(self) -> bool:
        return True

    def offer_interrupted(self, results_dir: str) -> None:
        return None


_FIND = object()


# =========================================================================== the window

class App:
    """One window. The pages receive it and use ``root``, ``theme``, ``help_line``,
    ``engines``, ``state``, ``tasks`` and ``store``.

    ``run_page_factory(parent, app)`` replaces the real Run page (tests). ``engines`` left
    out means "look for them"; ``self_test=False`` skips starting them once to see that
    Windows lets them run. ``screen=(width, height)`` pretends the screen has that size.
    """

    def __init__(self, root: Optional[tk.Tk] = None, *, dark: Optional[bool] = None,
                 high_contrast: Optional[bool] = None, scale: Optional[float] = None, demo: bool = False,
                 run_page_factory: Optional[Callable[[Any, "App"], Any]] = None,
                 store: Optional[SettingsStore] = None, engines: Any = _FIND, self_test: bool = True,
                 screen: Optional[Tuple[int, int]] = None, show: bool = True):
        self.root = root if root is not None else tk.Tk()
        self.root.withdraw()
        look = self.theme = _theme.init(
            self.root, is_dark=osapi.is_dark_mode() if dark is None else dark,
            is_high_contrast=osapi.is_high_contrast() if high_contrast is None else high_contrast, scale=scale)
        w.set_work_area_provider(osapi.work_area_at)
        self._dialog_hook = lambda window: osapi.set_dark_titlebar(window, look.dark)
        w.Dialog.created_hooks.append(self._dialog_hook)
        self.root.title(branding.window_title())
        try:
            self.root.iconphoto(True, *look.icons.window_icons())
        except tk.TclError:
            pass

        self.demo = demo
        self.store = store if store is not None else SettingsStore(remember=not demo)
        self.state = st.AppState()
        self.state.demo = demo
        self.tasks = st.TaskRunner()
        self.engines: Optional[EnginePaths] = None
        self.runner_factory: Optional[Callable[..., Any]] = None
        self.step = SAMPLES
        self.closed = False
        self._self_test = self_test and not demo
        self._run_visited = False
        self._folder_job: Optional[str] = None
        self._refresh_job: Optional[str] = None
        self._poll_job: Optional[str] = None
        self._idle_since: Optional[float] = None
        self._offer_checks = 0

        screen_w, screen_h = screen or (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        self.compact = (screen_h / look.scale) < COMPACT_BELOW
        width, height, x, y, min_w, min_h = window_geometry(screen_w, screen_h, look.scale, self.store.window_size())
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self.root.minsize(min_w, min_h)

        self._build(run_page_factory)
        self.state.subscribe(self._state_changed)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        # Windows is shutting down, restarting or logging the user off (Tk hands that notice on
        # under this name): the run notes why it is about to end (audit row 8.9)
        self.root.protocol("WM_SAVE_YOURSELF", self._session_ending)
        self.root.report_callback_exception = self._callback_error

        if demo:
            self._fill_demo()
        elif engines is _FIND:
            self.find_engines()
        else:
            self.engines = engines
        self.show_step(SAMPLES)
        self._refresh()
        self._poll()
        if show:
            self.root.deiconify()
            if self.store.settings.maximized and osapi.IS_WINDOWS:
                try:
                    self.root.state("zoomed")
                except tk.TclError:
                    pass
            self.root.update_idletasks()
            osapi.set_dark_titlebar(self.root, look.dark)
        if not demo:
            self._offer_interrupted()

    # ====================================================================== building
    def _build(self, run_page_factory: Optional[Callable[[Any, "App"], Any]]) -> None:
        root, look = self.root, self.theme
        px = look.px
        tight = self.compact
        root.configure(background=look.palette.bg)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # the help line and the bottom bar are made first: everything above explains itself there
        foot = w.Frame(root, ground="page", padding=(22, 4 if tight else 8, 22, 6 if tight else 8))
        foot.grid(row=4, column=0, sticky="ew")
        foot.columnconfigure(0, weight=1)
        self.help_line = w.HelpLine(foot, default=st.Words.HELP_DEFAULT, lines=2)
        self.help_line.grid(row=0, column=0, sticky="ew")
        self.bottom = w.BottomBar(foot, on_back=self.back, on_next=self.next, on_start=self.press_start,
                                  help_line=self.help_line)
        self.bottom.grid(row=1, column=0, sticky="ew", pady=(px(2 if tight else 6), 0))
        self.bottom.bind_keys(root)
        # on the Run step the same place holds Stop and the Open buttons of that page
        self.run_foot = w.Frame(foot, ground="page")
        self.run_foot.grid(row=1, column=0, sticky="ew", pady=(px(2 if tight else 6), 0))
        self.run_foot.grid_remove()
        self.footer = w.label(foot, branding.ATTRIBUTION, "small")
        if not tight:
            self.footer.grid(row=2, column=0, sticky="w", pady=(px(6), 0))

        head = w.Frame(root, ground="surface", padding=(18, 4 if tight else 8, 14, 4 if tight else 8))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        self.mark = w.ttk.Label(head, image=look.icons.mark(px(30)), style=w.style_for(head, "TLabel"))
        self.mark.grid(row=0, column=0, padx=(0, px(14)))
        self.stepper = w.Stepper(head, command=self._step_picked, help_line=self.help_line)
        self.stepper.grid(row=0, column=1, sticky="ew")
        self.about_button = w.button(head, texts.BUTTON["about"], self.show_about, kind="tool")
        self.about_button.grid(row=0, column=2, padx=(px(14), 0))
        w.hairline(root).grid(row=1, column=0, sticky="ew")

        body = self.body = w.Frame(root, ground="page", padding=(22, 8 if tight else 14, 22, 2 if tight else 6))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self.notices = w.NoticeBar(body)
        self.notices.grid(row=0, column=0, sticky="ew")
        self.holder = w.Frame(body)
        self.holder.grid(row=1, column=0, sticky="nsew")
        self.holder.columnconfigure(0, weight=1)
        self.holder.rowconfigure(0, weight=1)
        w.hairline(root).grid(row=3, column=0, sticky="ew")

        from .page_assembly import AssemblyPage
        from .page_samples import SamplesPage
        from .page_trimming import TrimmingPage
        self.samples_page = SamplesPage(self.holder, self)
        self.trimming_page = TrimmingPage(self.holder, self)
        self.assembly_page = AssemblyPage(self.holder, self)
        self.run_page = self._make_run_page(run_page_factory)
        self.pages = [self.samples_page, self.trimming_page, self.assembly_page, self.run_page]
        for page in self.pages:
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()

    def _make_run_page(self, factory: Optional[Callable[[Any, "App"], Any]]) -> Any:
        if factory is not None:
            return factory(self.holder, self)
        try:
            from .page_run import RunPage                 # written beside this file
            return RunPage(self.holder, self)
        except Exception:
            log.exception("the Run page could not be loaded")
            return _MissingRunPage(self.holder, self)

    def _fill_demo(self) -> None:
        """Made-up samples and a made-up results folder, for screenshots. No disk is read."""
        self.state.fixed_verdict = st.demo_verdict
        self.samples_page.add_samples(st.demo_samples())
        self.samples_page.set_results_folder("D:\\Sequencing\\%s" % branding.RESULTS_DIR_NAME, suggested=True)
        try:
            from . import demo
            clock = demo.VirtualClock(60.0)
            self.run_page.clock = clock.now
            self.run_page.preflight = lambda _config: []
            self.runner_factory = lambda config, events, **options: demo.DemoRunner(
                config, events, clock=clock, **options)
        except Exception:
            log.info("no demonstration runner; Start would use the real one")

    # ====================================================================== start-up checks
    def find_engines(self) -> None:
        """Audit 1.6 and 1.7: are the bundled programs there, and does Windows let them run?"""
        try:
            self.engines = appdirs.find_engines()
        except appdirs.EngineMissing as missing:
            self.engines = None
            names = ", ".join("engines%s%s" % ("\\" if osapi.IS_WINDOWS else os.sep, name) for name in missing.missing)
            self._engine_problem(texts.missing_part(names))
            return
        self._engine_problem("")
        if not self._self_test:
            return
        engines = self.engines

        def work(_report: Any) -> str:
            from .. import engine_proc
            for exe in (engines.tesseract_asm, engines.scepter):
                try:
                    engine_proc.engine_version(exe, timeout=10.0)
                except engine_proc.EngineStartError as problem:
                    log.warning("%s did not start: %s", exe, problem)
                    if not os.path.isfile(exe):
                        return texts.missing_part(os.path.basename(exe))
                    return texts.blocked_part(os.path.basename(exe))
            return ""

        def done(problem: Any, error: Optional[BaseException]) -> None:
            if error is None and self.engines is engines:
                self._engine_problem(str(problem or ""))

        self.tasks.submit(work, done, key="engines")

    def _engine_problem(self, words: str) -> None:
        self.state.engine_problem = words
        if words:
            self.notices.show("engines", words, "bad", dismissible=False,
                              actions=[(texts.BUTTON["check_again"], self.find_engines)])
        else:
            self.notices.dismiss("engines")
        self.state.notify("engines")

    def _offer_interrupted(self) -> None:
        """Audit 1.12: the results folder of the last run is looked at once; if that run never
        finished, the Run page offers to continue it and the window opens on that step."""
        self.offer_interrupted(self.store.recorded_folder("last_results"))

    def offer_interrupted(self, results_dir: str) -> None:
        """Look in ``results_dir`` for a run that never finished and, if there is one, let the
        Run page offer to continue it (audit rows 8.9 to 8.11 and 11.6)."""
        if not results_dir or self.state.running or self.closed:
            return
        try:
            self.run_page.offer_interrupted(results_dir)
        except Exception:
            log.exception("could not look for an interrupted run in %s", results_dir)
            return
        if self._offer_checks <= 0:
            self._offer_checks = 40
            self.root.after(250, self._watch_offer)
        else:
            self._offer_checks = 40

    def _watch_offer(self) -> None:
        if self.closed or self.state.running:
            return
        has_offer = getattr(self.run_page, "has_offer", None)
        try:
            offered = bool(has_offer()) if callable(has_offer) else False
        except Exception:
            offered = False
        if offered:
            self._offer_checks = 0
            self._run_visited = True
            if self.step == SAMPLES and not self.state.samples:
                self.show_step(RUN)
            elif self.step != RUN:                        # the offer stands on the Run step: point to it
                self.notices.show("unfinished", st.Words.UNFINISHED_RUN, "info",
                                  actions=[(texts.BUTTON["show"], self._show_offer)])
            self._refresh()
            return
        self._offer_checks -= 1
        if self._offer_checks > 0:
            self.root.after(250, self._watch_offer)
        else:
            self._offer_checks = 0

    def _show_offer(self) -> None:
        self.notices.dismiss("unfinished")
        self.show_step(RUN)

    # ====================================================================== steps
    def show_step(self, index: int) -> None:
        """0 Samples, 1 Trimming, 2 Assembly, 3 Run. Steps 1 to 3 cannot be shown during a run."""
        index = max(SAMPLES, min(RUN, int(index)))
        if self.state.running and index != RUN:
            return
        if index != self.step:
            self.commit_pages()
        w.hide_tooltips(self.root)
        self.help_line.clear()
        for number, page in enumerate(self.pages):
            if number == index:
                page.grid()
            else:
                page.grid_remove()
        self.step = index
        if index == RUN:
            self._run_visited = True
            self.notices.dismiss("unfinished")
        if self.compact:                                  # a small screen: the Run step needs the room
            self.help_line.set_lines(1 if index == RUN else 2)
        self.stepper.set_current(STEP_KEYS[index])
        if index in (TRIMMING, ASSEMBLY):
            self.pages[index].refresh()
        self._refresh()

    def _step_picked(self, key: str) -> None:
        self.show_step(STEP_KEYS.index(key))

    def back(self) -> None:
        if SAMPLES < self.step <= ASSEMBLY:
            self.show_step(self.step - 1)

    def next(self) -> None:
        if self.step < ASSEMBLY:
            self.show_step(self.step + 1)

    def commit_pages(self) -> None:
        """Take everything that is still being typed: numbers, the results folder."""
        for page in (self.samples_page, self.trimming_page, self.assembly_page):
            try:
                page.commit()
            except tk.TclError:
                pass

    def show_left_out(self) -> None:
        """The link beside Start: go to the list and select the rows that are left out."""
        if self.state.running:
            return
        self.show_step(SAMPLES)
        self.samples_page.select_left_out()

    def set_run_line(self, text: str, kind: str = "muted") -> None:
        """The small line under "Run" on the stepper ("running", "finished", "stopped")."""
        self.stepper.set_sub("run", text, kind)

    def set_running_title(self, text: str) -> None:
        """Title bar while a batch runs and after it ("Sample 3 of 40: KP_003",
        "Finished: 38 done, 2 failed"); "" restores the normal title.

        The news comes first and the name of the program after it (audit row 10.1): the
        taskbar shows only the beginning of a title, and that is where the user looks to
        see whether the run has finished without opening the window."""
        try:
            self.root.title(running_title(text))
        except tk.TclError:
            pass

    # ====================================================================== what the chrome shows
    def _state_changed(self, _what: str) -> None:
        if self._refresh_job is None and not self.closed:
            try:
                self._refresh_job = self.root.after_idle(self._refresh)
            except tk.TclError:
                pass

    def _refresh(self) -> None:
        if self._refresh_job is not None:                 # called directly while one was waiting
            try:
                self.root.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
        self._refresh_job = None
        if self.closed:
            return
        key = STEP_KEYS[self.step]
        for step, (text, kind) in st.step_lines(self.state).items():
            self.stepper.set_sub(step, text, kind)
        self.stepper.set_enabled("run", self._run_visited)
        self.stepper.lock(self.state.running)
        if self.step == RUN:
            self.bottom.grid_remove()
            self.run_foot.grid()
        else:
            self.run_foot.grid_remove()
            self.bottom.grid()
            status = self.start_status()
            self.bottom.set_back(enabled=self.step > SAMPLES, visible=self.step > SAMPLES)
            self.bottom.set_next(STEP_KEYS[self.step + 1] if self.step < ASSEMBLY else None)
            self.bottom.set_start(status.count, enabled=status.enabled, reason=status.reason)
            self.bottom.set_note("", link_text=status.link, link_command=self.show_left_out)
            self.bottom.set_hint(status.hint)
        if key == "trimming":
            self.trimming_page.refresh()

    def start_status(self) -> st.StartStatus:
        return st.start_status(self.state, STEP_KEYS[self.step])

    # ====================================================================== the results folder
    def check_folder(self, delay_ms: int = 250, force: bool = False) -> None:
        """Look at the results folder again, in the background, shortly (several calls in a
        row make one check). Nothing is done when the folder and the list are as they were
        at the last check, unless ``force``."""
        if self.state.demo or self.closed:
            return
        if force:
            self.state.facts = None if self.state.facts is None else dataclasses.replace(self.state.facts, signature=("stale",))
        if self._folder_job is not None:
            try:
                self.root.after_cancel(self._folder_job)
            except tk.TclError:
                pass
        self._folder_job = self.root.after(max(1, int(delay_ms)), self._check_folder_now)

    def _check_folder_now(self) -> None:
        self._folder_job = None
        asked = self.state.results_dir
        if not asked:
            self.state.facts = None
            return
        if self.state.running or self.state.facts_current():
            return
        samples = []
        for sample in self.state.samples:
            if sample.ready:
                twin = copy.copy(sample)
                twin.lanes = list(sample.lanes)
                samples.append(twin)
        trim, assembly = dataclasses.replace(self.state.trim), dataclasses.replace(self.state.assembly)
        self.tasks.submit(lambda _report: st.inspect_folder(asked, samples, trim, assembly),
                          self._folder_checked, key="folder")

    def _folder_checked(self, facts: Any, error: Optional[BaseException]) -> None:
        if error is not None or facts is None or self.closed:
            return
        if st.clean_folder_text(facts.asked) != self.state.results_dir:
            return                                        # the user has chosen another folder since
        if facts.appended and facts.valid:
            self.state.results_dir = facts.path
            self.state.results_appended = True
            self.samples_page.show_results_folder()
        self.state.facts = facts
        self.state.notify("results")
        if not self.state.facts_current():                # the list changed while the folder was read
            self.check_folder(50)

    # ====================================================================== Start
    def press_start(self) -> bool:
        """What the Start button does. True when a run was started."""
        if self.state.running or self.closed:
            return False
        self.commit_pages()
        status = self.start_status()
        if not status.enabled:
            self._refresh()
            return False
        if not self.state.demo:
            if st.recheck_files(self.state, osapi.is_online_only):      # audit 2.21
                self.samples_page.list_changed()
                self.help_line.say(FILES_GONE, "warn", seconds=12)
                if not self.state.ready():
                    self._refresh()
                    return False
        problems = st.settings_problems(self.state)
        if problems:
            step, name, message = problems[0]
            self.show_step(STEP_KEYS.index(step))
            self.pages[STEP_KEYS.index(step)].focus_field(name)
            self.help_line.say(message, "bad", seconds=0)
            return False

        choice = Existing.SKIP
        if not self.state.demo:
            ready = self.state.ready()
            trim, assembly = self.state.trim, self.state.assembly
            try:
                found = results.existing_results(
                    self.state.results_dir, ready,
                    lambda sample: settings_map.settings_fingerprint(trim, assembly, sample.trim))
            except results.NewerFormat:
                self.check_folder(0, force=True)
                return False
            except Exception:
                log.exception("the results folder could not be read when Start was pressed")
                found = {}
            other_reads = {name: item for name, item in found.items() if not item.same_reads}
            if other_reads and self.state.facts is not None:      # decision D3: never skipped, never overwritten
                for name, item in other_reads.items():
                    self.state.facts.clashes.setdefault(name, item.finished_at)
                self.samples_page.refresh()
                self._refresh()
                ready = self.state.ready()
                if not ready:
                    return False
            names = {sample.name for sample in ready}
            found = {name: item for name, item in found.items() if name in names and item.same_reads}
            if found:
                answer = dialogs.ask_overwrite(self.root, found, len(ready))      # audit A-5
                if answer is None:
                    return False
                choice = answer
        if self.state.running:                            # the question was open; something else began
            return False
        config = st.build_config(self.state, choice)
        if not config.samples:
            self._refresh()
            return False
        self.start_run(config, st.left_out_rows(self.state))
        return True

    def start_run(self, config: RunConfig, left_out: Sequence[Tuple[str, str]], *, resume: bool = False) -> None:
        """Switch to the Run page and begin. Also called by the Run page itself for Continue
        and "Try the failed samples again"."""
        self.state.running = True
        self._idle_since = None
        self._run_visited = True
        self.store.set_folder("last_results", config.results_dir)
        self.store.save()
        self.notices.dismiss("start_fault")
        self.notices.dismiss("unfinished")
        self.show_step(RUN)
        self.set_run_line(texts.STEP_RUNNING, "accent")
        self.state.notify("running")
        self._refresh()
        options: dict = {"resume": resume}
        if self.runner_factory is not None:
            options["runner_factory"] = self.runner_factory
        try:
            self.run_page.begin(config, list(left_out), **options)
        except Exception:
            log.exception("the Run page could not begin")
            self.run_ended()
            self.show_step(SAMPLES)
            self.notices.show("start_fault", START_FAULT, "bad")

    def run_ended(self) -> None:
        """Called by the Run page when a batch has ended: steps 1 to 3 are unlocked."""
        was_running = self.state.running
        self.state.running = False
        self._idle_since = None
        reason = getattr(self.run_page, "end_reason", None)
        if reason is EndReason.COMPLETED:
            self.set_run_line(texts.STEP_FINISHED, "ok")
        elif reason in (EndReason.STOPPED, EndReason.ENVIRONMENT):
            self.set_run_line(texts.STEP_STOPPED, "warn")
        elif self.stepper.sub("run") == texts.STEP_RUNNING:
            self.set_run_line("")
        self.state.notify("running")
        self._refresh()
        if was_running:
            self.check_folder(0, force=True)              # what is in the results folder has changed

    def new_run(self) -> None:
        """"New run" on the Run page: empty the list and go back to the Samples step."""
        if self.state.running:
            return
        self.samples_page.clear_list()
        self.set_run_line("")                             # "finished" was about the run that is put away
        self.show_step(SAMPLES)

    # ====================================================================== boxes, closing
    def show_about(self) -> None:
        folder = (getattr(self.run_page, "results_dir", "") or self.state.results_dir
                  or self.store.recorded_folder("last_results") or "")
        dialogs.show_about(self.root, folder)

    def close(self) -> bool:
        """The close box. During a run the Run page asks first. True when the window closed."""
        if self.closed:
            return True
        try:
            allowed = bool(self.run_page.confirm_close())
        except Exception:
            log.exception("the Run page could not answer the close box")
            allowed = True
        if not allowed:
            return False
        self.closed = True
        self._remember_window()
        self.store.save()
        self.tasks.close()
        osapi.keep_awake(False)
        try:
            w.Dialog.created_hooks.remove(self._dialog_hook)
        except ValueError:
            pass
        for job in (self._folder_job, self._refresh_job, self._poll_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        return True

    def _remember_window(self) -> None:
        try:
            maximized = osapi.IS_WINDOWS and self.root.state() == "zoomed"
            scale = self.theme.scale or 1.0
            self.store.set_window(int(round(self.root.winfo_width() / scale)),
                                  int(round(self.root.winfo_height() / scale)), bool(maximized))
        except tk.TclError:
            pass

    def _session_ending(self) -> None:
        """Windows gave notice that the session ends. Nothing is asked and nothing is stopped
        here (Windows ends the engines with the program): the run only records the cause, so
        that the next launch can say why the last run was interrupted."""
        note = getattr(self.run_page, "note_interruption", None)
        try:
            if callable(note):
                note()
            self._remember_window()
            self.store.save()
        except Exception:
            log.exception("the end of the Windows session could not be noted")

    def _callback_error(self, kind: Any, value: Any, trace: Any) -> None:
        """An error in a button or a timer (audit row 9.14): it goes to the log, and a bar
        says so in plain words. Nothing stops: the run goes on if it can."""
        log.error("an error in the window", exc_info=(kind, value, trace))
        try:
            if not self.closed:
                words = st.Words.PROGRAM_FAULT_RUNNING if self.state.running else st.Words.PROGRAM_FAULT
                self.notices.show("fault", words, "warn")
        except Exception:
            log.exception("the fault could not be shown")

    # ====================================================================== the heartbeat
    def _poll(self) -> None:
        self._poll_job = None
        if self.closed:
            return
        try:
            self.tasks.poll()
            self._watch_run()
            self._poll_job = self.root.after(40 if self.tasks.busy else 120, self._poll)
        except tk.TclError:
            pass

    def _watch_run(self) -> None:
        """A safety net: if the Run page is no longer running but never said so, unlock."""
        if not self.state.running:
            return
        try:
            running = bool(self.run_page.is_running())
        except Exception:
            running = True
        if running:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.monotonic()
        elif time.monotonic() - self._idle_since > 3.0:
            log.warning("the Run page stopped without saying so; the steps are unlocked")
            self.run_ended()

    @property
    def busy(self) -> bool:
        """True while background work or a pending redraw could still change what is shown."""
        return bool(self.tasks.busy or self._folder_job is not None or self._refresh_job is not None
                    or self.samples_page.settling)

    def settle(self, timeout: float = 8.0) -> bool:
        """Run the window until nothing is pending (tests, screenshots). False on a timeout."""
        end = time.monotonic() + timeout
        calm = 0
        while time.monotonic() < end:
            self.root.update()
            self.tasks.poll()
            calm = calm + 1 if not self.busy else 0
            if calm >= 3:
                self.root.update_idletasks()
                return True
            time.sleep(0.01)
        return False

    def run(self) -> None:
        self.root.mainloop()


# =========================================================================== starting

def _lonely_message(words: str) -> None:
    """A message when there is no window to stand on (run from a zip, already open)."""
    try:
        root = tk.Tk()
        root.withdraw()
        _theme.init(root, is_dark=osapi.is_dark_mode(), is_high_contrast=osapi.is_high_contrast())
        root.title(branding.APP_NAME)
        dialogs.show_message(root, branding.APP_NAME, words, level="info", button=texts.BUTTON["close"])
        root.destroy()
    except Exception:
        log.exception("could not show: %s", words)


def _parse(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=branding.APP_NAME, add_help=False)
    parser.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dark", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--light", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scale", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--screen", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--step", type=int, default=0, help=argparse.SUPPRESS)
    args, _unknown = parser.parse_known_args(list(argv) if argv is not None else sys.argv[1:])
    return args


def run_app(argv: Optional[Sequence[str]] = None) -> int:
    """Open the window and run it until it is closed. Returns the exit code of the program."""
    args = _parse(argv)
    setup_logging()
    osapi.set_process_defaults()
    log.info("%s starting", branding.versions_line())
    if appdirs.is_frozen() and still_in_zip(sys.executable, tempfile.gettempdir()):
        _lonely_message(texts.STILL_IN_ZIP)                # audit 1.3
        return 2
    screen = None
    if args.screen:
        try:
            wide, high = (int(part) for part in str(args.screen).lower().split("x", 1))
            screen = (wide, high)
        except ValueError:
            screen = None
    dark: Optional[bool] = True if args.dark else (False if args.light else None)
    try:
        app = App(dark=dark, scale=args.scale, demo=args.demo, screen=screen)
        if args.step:
            app.show_step(args.step)
        app.run()
    except Exception:
        log.critical("the window could not be opened or stopped with an error", exc_info=True)
        raise
    return 0
