# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Step 1, Samples: the list of read files and the results folder (audit B.3, A-2 to A-4).

Add files, Add folder, Remove, Clear list; a drop zone and an empty state with "Try the
example"; the table (Trim, Sample, Read 1, Read 2, Size, Status with the reason in words);
Trim all and Trim none; renaming in place; "Make the names different" when names clash;
the counter "38 ready. 2 left out: why?"; the results folder with Browse and the lines
under it.

Nothing slow happens in the window's thread. Searching folders (``pairing.scan``), looking
into files (``pairing.check_sample``), suggesting and checking the results folder all run
through ``app.tasks`` (a ``state.TaskRunner``) and come back as answers. Rows show
"Checking..." until their files have been looked at. Drag and drop works through
``tkinterdnd2`` when that package loads; when it does not, nothing is said and the Add
buttons do the same job.
"""

from __future__ import annotations

import copy
import logging
import ntpath
import os
import tempfile
import threading
import tkinter as tk
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import appdirs, branding, fsutil, osapi, pairing
from ..model import AssemblySettings, Sample, SampleState
from . import dialogs
from . import state as st
from . import texts
from . import theme as _theme
from . import widgets as w

__all__ = ["SamplesPage", "sample_row"]

log = logging.getLogger(__name__)

RECHECK_MS = 6000            # left-out rows are looked at again this often (a copy that has finished)
RECHECK_MOST = 24
_NOTE_STYLES = {"small": "Small.TLabel", "small_warn": "SmallWarn.TLabel", "small_bad": "SmallBad.TLabel"}
#: The made-up isolate of ``tools/make_example.py``: a 180 kb chromosome and a small plasmid.
#: Judged against the 5 Mb the program assumes, its depth would look far too low and the
#: practice run would end "Done (check notes)"; so "Try the example" ticks the genome size.
EXAMPLE_GENOME_MB = 0.2


def _lanes_suffix(lanes: int) -> str:
    if lanes <= 1:
        return ""
    return " " + texts.more_lanes(lanes - 1)


def _file_name(path: str) -> str:
    """The name of a read file without its folder (also for a Windows path seen elsewhere)."""
    name = os.path.basename(path)
    return ntpath.basename(path) if name == path and "\\" in path else name


def sample_row(state: st.AppState, sample: Sample) -> Dict[str, Any]:
    """The table row of a sample: what each cell says, its tick and its colour."""
    shown, note = state.view(sample)
    row_id = st.row_id_of(sample)
    left_out = shown is SampleState.LEFT_OUT
    checking = row_id in state.checking and not left_out
    lanes = [pair for pair in sample.lanes if pair]
    first = lanes[0] if lanes else ("", "")
    suffix = _lanes_suffix(len(lanes))
    status = st.Words.CHECKING_ROW if checking else st.status_text(shown, note)
    kind = "accent" if checking else ("bad" if left_out else w.status_kind(shown))
    return {
        "id": row_id,
        "checked": None if left_out else bool(sample.trim),
        "status": kind,
        "dim": left_out,
        "values": {
            "sample": sample.name,
            "read1": (_file_name(first[0]) + suffix) if first[0] else "",
            "read2": (_file_name(first[1]) + suffix) if first[1] else "",
            "size": fsutil.format_bytes(sample.size_bytes) if sample.size_bytes else "",
            "status": status,
        },
        "tips": {
            "read1": "\n".join(pair[0] for pair in lanes if pair[0]),
            "read2": "\n".join(pair[1] for pair in lanes if pair[1]),
            "status": status,
        },
    }


class SamplesPage(w.Frame):
    """Step 1 of the window."""

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, ground="page")
        self.app = app
        self.state: st.AppState = app.state
        self.tasks: st.TaskRunner = app.tasks
        self.help_line = app.help_line
        look = self.look = _theme.current(self)
        px = look.px
        compact = bool(getattr(app, "compact", False))
        self._searches = 0
        self._stop_search: Optional[threading.Event] = None
        self._not_added: List[str] = []
        self._refresh_job: Optional[str] = None
        self._suggesting = False
        self._last_folder_text = ""
        self._example_size_set = False

        self.columnconfigure(0, weight=1)
        row = 0
        if not compact:
            self.intro = w.WrapLabel(self, texts.PAGE_INTRO["samples"], "soft", wrap=900)
            self.intro.grid(row=row, column=0, sticky="ew", pady=(0, px(10)))
            row += 1
        self.notices = w.NoticeBar(self)
        self.notices.grid(row=row, column=0, sticky="ew")
        row += 1

        # ---- the buttons above the list
        bar = w.Frame(self)
        bar.grid(row=row, column=0, sticky="ew", pady=(0, px(8)))
        row += 1
        self.buttons: Dict[str, Any] = {}
        commands = {"add_files": self.choose_files, "add_folder": self.choose_folder,
                    "remove": self.remove_selected, "clear_list": self.clear_list}
        for index, key in enumerate(("add_files", "add_folder", "remove", "clear_list")):
            made = w.button(bar, texts.BUTTON[key], commands[key])
            made.pack(side="left", padx=(0 if index == 0 else px(8), 0))
            w.explain(made, texts.HOVER[key], self.help_line)
            self.buttons[key] = made
        self.names_button = w.button(bar, texts.BUTTON["make_names_different"], self.make_names_different)
        w.explain(self.names_button, texts.HOVER["make_names_different"], self.help_line)

        # ---- the list
        names = texts.SAMPLES_COLUMNS
        columns = [
            w.Column("sample", names["sample"], width=150, editable=True, shorten="middle",
                     hover=texts.HOVER["sample_column"]),
            w.Column("read1", names["read1"], width=176, shorten="middle", hover=texts.HOVER["read1_column"]),
            w.Column("read2", names["read2"], width=176, shorten="middle", hover=texts.HOVER["read2_column"]),
            w.Column("size", names["size"], width=74, anchor="e", hover=texts.HOVER["size_column"]),
            w.Column("status", names["status"], width=300, stretch=True, status=True,
                     hover=texts.HOVER["status_column"]),
        ]
        self.table = w.CheckTable(
            self, columns, check_title=names["trim"], check_hover=texts.HOVER["trim_column"],
            height=3 if compact else 6, help_line=self.help_line, on_toggle=self._toggled,
            on_remove=self._removed, on_rename=self._renamed, on_select=self._selected,
            validate_rename=self._name_problem)
        self.table.grid(row=row, column=0, sticky="nsew")
        self.rowconfigure(row, weight=1)
        row += 1
        self.drop_enabled = self._enable_drop()
        self._set_empty_state()
        self.table.tree.bind("<FocusOut>", lambda _e: self.help_line.clear_focus(owner=self.table.tree), add="+")
        self.table.tree.bind("<FocusIn>", lambda _e: self._explain_selection(), add="+")

        # ---- under the list
        under = w.Frame(self)
        under.grid(row=row, column=0, sticky="ew", pady=(px(8), 0))
        row += 1
        under.columnconfigure(3, weight=1)
        w.label(under, texts.TRIMMING_LABEL, "soft").grid(row=0, column=0, sticky="w")
        self.trim_all_button = w.button(under, texts.BUTTON["trim_all"], lambda: self.trim_all(True))
        self.trim_all_button.grid(row=0, column=1, padx=(px(8), 0))
        w.explain(self.trim_all_button, texts.HOVER["trim_all"], self.help_line)
        self.trim_none_button = w.button(under, texts.BUTTON["trim_none"], lambda: self.trim_all(False))
        self.trim_none_button.grid(row=0, column=2, padx=(px(8), 0))
        w.explain(self.trim_none_button, texts.HOVER["trim_none"], self.help_line)
        self.activity = w.WrapLabel(under, "", "small", wrap=420)
        self.activity.grid(row=0, column=3, sticky="ew", padx=(px(16), px(4)))
        self.activity_link = w.LinkLabel(under, "", small=True)
        self.activity_link.grid(row=0, column=4, padx=(0, px(12)))
        self.activity_link.grid_remove()
        self.counter = w.label(under, "", "soft")
        self.counter.grid(row=0, column=5, sticky="e")
        self.counter_link = w.LinkLabel(under, "", self.select_left_out)
        self.counter_link.grid(row=0, column=6, sticky="e", padx=(px(4), 0))
        self.counter_link.grid_remove()
        w.explain(self.counter_link, texts.HOVER["left_out_link"], self.help_line)

        # ---- the results folder
        folder = w.Frame(self)
        folder.grid(row=row, column=0, sticky="ew", pady=(px(8 if compact else 12), 0))
        folder.columnconfigure(1, weight=1)
        w.label(folder, texts.RESULTS_FOLDER_LABEL, "bold").grid(row=0, column=0, sticky="w", padx=(0, px(10)))
        self.folder_var = tk.StringVar(self, value=self.state.results_dir)
        self.folder_entry = w.entry(folder, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew")
        self.folder_entry.bind("<Return>", lambda _e: self.commit_folder(), add="+")
        self.folder_entry.bind("<KP_Enter>", lambda _e: self.commit_folder(), add="+")
        self.folder_entry.bind("<FocusOut>", lambda _e: self.commit_folder(), add="+")
        w.explain(self.folder_entry, texts.HOVER["results_folder"], self.help_line)
        self.browse_button = w.button(folder, texts.BUTTON["browse"], self.browse)
        self.browse_button.grid(row=0, column=2, padx=(px(8), 0))
        w.explain(self.browse_button, texts.HOVER["browse"], self.help_line)
        self.folder_info = w.WrapLabel(folder, texts.RESULTS_FOLDER_NOTE, "small", wrap=760)
        self.folder_info.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(px(4), 0))
        self.folder_notes = w.WrapLabel(folder, "", "small", wrap=760)
        self.folder_notes.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(px(1), 0))
        self.folder_notes.grid_remove()
        self._notes_shown = False

        self.state.subscribe(self._state_changed)
        self.refresh()
        self._recheck_job: Optional[str] = self.after(RECHECK_MS, self._recheck_tick)
        self.bind("<Destroy>", self._destroyed, add="+")

    # ====================================================================== adding
    def choose_files(self) -> None:
        if self.state.running:
            return
        chosen = dialogs.choose_files(self.winfo_toplevel(), self.app.store.folder("reads"))
        if chosen:
            self.app.store.set_folder("reads", os.path.dirname(chosen[0]))
            self.add_paths(chosen)

    def choose_folder(self) -> None:
        if self.state.running:
            return
        chosen = dialogs.choose_folder(self.winfo_toplevel(), st.Words.CHOOSE_READS_FOLDER,
                                       self.app.store.folder("reads"))
        if chosen:
            self.app.store.set_folder("reads", chosen)
            self.add_paths([chosen])

    def add_paths(self, paths: Iterable[str], *, example: bool = False) -> None:
        """Search files and folders for read pairs, in the background, and add what is found."""
        wanted = [str(path) for path in paths if str(path).strip()]
        if not wanted or self.state.running:
            return
        stop = threading.Event()
        self._stop_search = stop
        self._searches += 1
        self.state.searching = True
        self._say(st.Words.LOOKING.format(n=texts.number(0)), texts.BUTTON["stop_looking"], stop.set)
        self.state.notify("samples")

        def work(report: Any) -> Tuple[Any, str]:
            found = pairing.scan(wanted, progress=report, should_stop=stop.is_set)
            results_dir = ""
            if example:
                base = appdirs.example_dir()
                results_dir = (os.path.join(base, "results") if fsutil.is_writable_dir(base) else
                               os.path.join(tempfile.gettempdir(), "%s_example_results" % branding.APP_NAME))
            return found, results_dir

        def progress(count: Any) -> None:
            if not stop.is_set():
                self._say(st.Words.LOOKING.format(n=texts.number(int(count))),
                          texts.BUTTON["stop_looking"], stop.set)

        def done(answer: Any, error: Optional[BaseException]) -> None:
            self._searches = max(0, self._searches - 1)
            self.state.searching = self._searches > 0
            if error is not None or answer is None:
                self._say("")
                self.state.notify("samples")
                return
            found, results_dir = answer
            self._found(found, example=example, results_dir=results_dir)

        self.tasks.submit(work, done, progress=progress)

    def _found(self, found: Any, *, example: bool = False, results_dir: str = "") -> None:
        """A search has ended: merge what it found into the list and look into the new files."""
        usable = [s for s in found.samples if not pairing.is_undetermined(s)]
        merged = pairing.merge(self.state.samples, usable)
        fresh = [s for s in merged.samples if not st.row_id_of(s)]
        self.state.set_samples(merged.samples)

        self._not_added = [item.reason for item in found.ignored if item.reason]
        self._not_added += ["%s: %s" % (s.name, s.note) for s in found.samples if pairing.is_undetermined(s)]
        only = found.ignored[0].reason if (len(found.ignored) == 1 and not found.samples) else ""
        summary = found.summary()
        if summary.startswith("Found ") and fresh and not merged.note:
            summary = "Added " + summary[len("Found "):]          # audit 2.1: all of them are new to the list
        words = only or " ".join(part for part in (summary, merged.note) if part)
        if example and usable:
            self._not_added = []                         # the note beside the practice reads is not news
            self._say("")
        elif self._not_added and not only:
            self._say(words, texts.BUTTON["show"], self.show_not_added)
        else:
            self._say(words)
        if found.none_paired:
            self.notices.show("none_paired", st.Words.NONE_PAIRED, "warn")
        elif fresh:
            self.notices.dismiss("none_paired")
        if example and usable:
            self.state.example_loaded = True
            only_example = all(self._is_example(s) for s in self.state.samples)
            self._example_settings(only_example)
            self.notices.show("example", texts.EXAMPLE_NOTE, "info")
            if self._example_size_set:
                self.help_line.say(texts.EXAMPLE_SIZE_NOTE, "info", seconds=20)
            if results_dir and (not self.state.results_dir or not self.state.results_by_user):
                self.set_results_folder(results_dir, suggested=True)
        elif fresh and self.state.example_loaded:
            self._leave_example()                        # the user's own samples have joined the list
        self._look_into(fresh)
        self.list_changed()

    def add_samples(self, samples: Sequence[Sample], *, look_inside: bool = False) -> None:
        """Put ready-made samples in the list (demonstration, tests)."""
        merged = pairing.merge(self.state.samples, list(samples))
        fresh = [s for s in merged.samples if not st.row_id_of(s)]
        self.state.set_samples(merged.samples)
        if look_inside:
            self._look_into(fresh)
        self.list_changed()

    def _look_into(self, samples: Sequence[Sample], quiet: bool = False) -> None:
        """Peek into the files of rows, in the background. ``quiet``: a second look at rows
        that are left out, which shows nothing unless something changed."""
        jobs = []
        for sample in samples:
            row_id = self.state.adopt(sample)
            twin = copy.copy(sample)
            twin.lanes = list(sample.lanes)
            jobs.append((row_id, twin))
            if not quiet:
                self.state.checking.add(row_id)
        if not jobs:
            return

        def work(report: Any) -> None:
            for row_id, twin in jobs:
                try:
                    pairing.check_sample(twin, osapi.is_online_only)
                except Exception:
                    log.exception("a look into the files of %s failed", twin.name)
                    report((row_id, None))
                    continue
                report((row_id, twin))

        def done(_answer: Any, _error: Optional[BaseException]) -> None:
            for row_id, _twin in jobs:
                self.state.checking.discard(row_id)
            self._after_checks()

        self.tasks.submit(work, done, progress=lambda item: self._looked(item, quiet))

    def _looked(self, item: Tuple[str, Optional[Sample]], quiet: bool) -> None:
        row_id, twin = item
        self.state.checking.discard(row_id)
        live = self.state.sample(row_id)
        if live is None or twin is None:
            return
        if quiet and (twin.state, twin.note) == (live.state, live.note):
            return
        if quiet and pairing.is_name_clash(live):
            return
        if twin.note == pairing.TINY and self._is_example(twin):
            twin.state, twin.note = SampleState.READY, ""        # small on purpose: nothing to warn about
        name_now, twin.trim = live.name, live.trim
        index = self.state.samples.index(live)
        self.state.samples[index] = twin
        if twin.name != name_now and pairing.rename(self.state.samples, twin, name_now):
            twin.name = name_now
        self._after_checks()

    def _after_checks(self) -> None:
        """Rows changed under the list: tidy up once, however many answers came in."""
        if self._refresh_job is None:
            self._refresh_job = self.after(30, self._settle_list)

    def _settle_list(self) -> None:
        self._refresh_job = None
        if self.table.editing is not None:              # do not pull a name away from under the user
            self._refresh_job = self.after(300, self._settle_list)
            return
        pairing.mark_name_clashes(self.state.samples)
        self.list_changed()

    @property
    def settling(self) -> bool:
        return self._refresh_job is not None or self._suggesting

    # ====================================================================== the list
    def list_changed(self) -> None:
        """Call after any change of the samples: redraw, tell the shell, look at the folder."""
        self.refresh()
        self._suggest_folder()
        self.state.notify("samples")
        self.app.check_folder()

    def refresh(self) -> None:
        """Draw the list and what stands around it from the state."""
        if self.table.editing is None:
            self.table.set_rows([sample_row(self.state, s) for s in self.state.samples])
        self._set_empty_state()
        total = len(self.state.samples)
        ready = len(self.state.ready())
        plain, link = texts.ready_counter(ready, total - ready)
        self.counter.configure(text=plain if total else "")
        if link:
            self.counter_link.configure(text=link)
            self.counter_link.grid()
        else:
            self.counter_link.grid_remove()
        if self.state.has_name_clash():
            self.names_button.pack(side="right")
        else:
            self.names_button.pack_forget()
        self._refresh_buttons()
        self._refresh_folder_lines()

    def _refresh_buttons(self) -> None:
        locked = self.state.running
        some = bool(self.state.samples)
        flags = {
            "add_files": not locked, "add_folder": not locked,
            "remove": not locked and bool(self.table.selection()),
            "clear_list": not locked and some,
        }
        for key, usable in flags.items():
            self.buttons[key].state(["!disabled"] if usable else ["disabled"])
        for made in (self.trim_all_button, self.trim_none_button, self.names_button):
            made.state(["!disabled"] if (some and not locked) else ["disabled"])
        for made in (self.folder_entry, self.browse_button):
            made.state(["!disabled"] if not locked else ["disabled"])
        self.table.set_enabled(not locked)

    def _set_empty_state(self) -> None:
        if self.state.samples:
            return
        words = texts.EMPTY_STATE if self.drop_enabled else texts.EMPTY_STATE_NO_DROP
        spec = (words, self._example_available())
        if getattr(self, "_empty_spec", None) == spec:
            return
        self._empty_spec = spec
        if spec[1]:
            self.table.set_empty(words, texts.BUTTON["try_example"], self.try_example, texts.EMPTY_STATE_LINK_LEAD)
        else:
            self.table.set_empty(words)

    def _example_available(self) -> bool:
        if self.state.demo:
            return True
        try:
            return os.path.isdir(appdirs.example_dir())
        except OSError:
            return False

    def _selected(self, _ids: List[str]) -> None:
        self._refresh_buttons()
        self._explain_selection()

    def _explain_selection(self) -> None:
        """The note of the selected row in full, in the help line: a reason must be readable
        without resting the mouse on it."""
        tree = self.table.tree
        try:
            focused = self.focus_get() is tree
        except (KeyError, tk.TclError):
            focused = False
        chosen = self.table.selection()
        sample = self.state.sample(chosen[0]) if chosen else None
        if not focused or sample is None:
            self.help_line.clear_focus(owner=tree)
            return
        shown, note = self.state.view(sample)
        if note:
            self.help_line.focus_text("%s: %s" % (sample.name, st.status_text(shown, note)), owner=tree)
        else:
            self.help_line.focus_text(texts.HOVER["sample_column"], owner=tree)

    def _toggled(self, ids: List[str], checked: bool) -> None:
        for row_id in ids:
            sample = self.state.sample(row_id)
            if sample is not None:
                sample.trim = bool(checked)
        self._refresh_folder_lines()
        self.state.notify("samples")

    def trim_all(self, checked: bool) -> None:
        """Trim all / Trim none: every row, also those that are left out for now."""
        if self.state.running:
            return
        for sample in self.state.samples:
            sample.trim = bool(checked)
        self.refresh()
        self.state.notify("samples")

    def _removed(self, ids: List[str]) -> None:
        gone = set(ids)
        self.state.set_samples([s for s in self.state.samples if st.row_id_of(s) not in gone])
        pairing.mark_name_clashes(self.state.samples)
        if not self.state.samples:
            self._list_emptied()
        elif self.state.example_loaded and not any(self._is_example(s) for s in self.state.samples):
            self._leave_example()
        self.list_changed()

    def remove_selected(self) -> None:
        if not self.state.running:
            self.table.remove_selected()

    def clear_list(self) -> None:
        if self.state.running:
            return
        self.state.set_samples([])
        self._list_emptied()
        self.list_changed()

    def _list_emptied(self) -> None:
        self._leave_example()
        self.notices.dismiss("none_paired")
        self._say("")
        if not self.state.results_by_user:               # a folder the program suggested for those reads
            self.set_results_folder("")

    def _name_problem(self, row_id: str, text: str) -> str:
        sample = self.state.sample(row_id)
        return pairing.name_problem(self.state.samples, sample, text) if sample is not None else ""

    def _renamed(self, row_id: str, old: str, new: str) -> None:
        sample = self.state.sample(row_id)
        if sample is None:
            return
        problem = pairing.rename(self.state.samples, sample, new)
        if problem:
            self.help_line.say(problem, "warn")
        self.after_idle(self.list_changed)

    def rename(self, row_id: str, new_name: str) -> str:
        """Rename a row from the program. Returns "" or the reason why not."""
        sample = self.state.sample(row_id)
        if sample is None:
            return ""
        problem = pairing.rename(self.state.samples, sample, new_name)
        if not problem:
            self.list_changed()
        return problem

    def make_names_different(self) -> None:
        if self.state.running:
            return
        pairing.make_names_different(self.state.samples)
        self.list_changed()

    def select_left_out(self) -> None:
        """The link "2 left out: why?": select those rows so that their reasons can be read."""
        ids = [st.row_id_of(s) for s in self.state.left_out()]
        self.table.select(ids)
        self.table.tree.focus_set()

    def show_not_added(self) -> None:
        if self._not_added:
            dialogs.show_list(self.winfo_toplevel(), st.Words.NOT_ADDED_TITLE, st.Words.NOT_ADDED_BODY,
                              self._not_added)

    # ====================================================================== the example
    def try_example(self) -> None:
        folder = appdirs.example_dir()
        if not self._example_available():
            self.help_line.say(st.Words.EXAMPLE_MISSING, "warn")
            return
        self.add_paths([folder], example=True)

    def _is_example(self, sample: Sample) -> bool:
        """Is this row the practice sample that comes with the program?"""
        if not sample.read1:
            return False
        try:
            return st.is_inside(sample.read1, appdirs.example_dir())
        except (OSError, ValueError):
            return False

    def _example_settings(self, on: bool) -> None:
        """The practice isolate has a tiny genome: tick "I know the genome size" for it, in
        plain sight (the Assembly step then says "changed", and a note there and in the help
        line says why), and put the standard back when the practice sample leaves the list or
        others join it."""
        assembly = self.state.assembly
        if on:
            if not assembly.know_genome_size:
                assembly.know_genome_size, assembly.genome_size_mb = True, EXAMPLE_GENOME_MB
                self._example_size_set = True
        elif self._example_size_set:
            self._example_size_set = False
            if assembly.know_genome_size and abs(assembly.genome_size_mb - EXAMPLE_GENOME_MB) < 1e-6:
                standard = AssemblySettings()
                assembly.know_genome_size = standard.know_genome_size
                assembly.genome_size_mb = standard.genome_size_mb
        else:
            return
        page = getattr(self.app, "assembly_page", None)
        if page is not None:
            page.load()
            if self._example_size_set:                    # said where the setting is
                page.notice.show("example", texts.EXAMPLE_SIZE_NOTE, "info")
            else:
                page.notice.dismiss("example")
        self.state.notify("assembly")

    def _leave_example(self) -> None:
        """The list is no longer the practice sample alone: its note and its genome size go."""
        self.state.example_loaded = False
        self.notices.dismiss("example")
        self._example_settings(False)

    # ====================================================================== drag and drop
    def _enable_drop(self) -> bool:
        """Accept files dropped on the window, if ``tkinterdnd2`` is there. Silent otherwise."""
        if self.state.demo:
            return True                                  # screenshots show the usual first screen
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            root = self.winfo_toplevel()
            TkinterDnD._require(root)
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", self._dropped)
            return True
        except Exception:
            log.info("drag and drop is not available; the Add buttons do the same")
            return False

    def _dropped(self, event: Any) -> str:
        action = getattr(event, "action", "copy") or "copy"
        if self.state.running or not self.winfo_ismapped():
            return action
        try:
            items = [str(item) for item in self.tk.splitlist(getattr(event, "data", "") or "")]
        except tk.TclError:
            items = []
        self.drop(items)
        return action

    def drop(self, items: Sequence[str]) -> None:
        """What a drop does with the things that were dropped."""
        real = []
        for item in items:
            try:
                if item and os.path.exists(item):
                    real.append(item)
            except (OSError, ValueError):
                pass
        if not real:
            if items:
                self._say(st.Words.NOT_ON_DISK)
            return
        self.app.store.set_folder("reads", real[0] if os.path.isdir(real[0]) else os.path.dirname(real[0]))
        self.add_paths(real)

    # ====================================================================== the results folder
    def browse(self) -> None:
        if self.state.running:
            return
        start = (_nearest_folder(self.state.results_dir) or self.app.store.folder("results_browse")
                 or self.app.store.folder("reads"))
        chosen = dialogs.choose_folder(self.winfo_toplevel(), st.Words.CHOOSE_RESULTS_FOLDER, start,
                                       must_exist=False)
        if chosen:
            self.app.store.set_folder("results_browse", chosen)
            self.set_results_folder(chosen, by_user=True)

    def commit_folder(self) -> None:
        """The field was left, or Enter was pressed in it: take what was typed."""
        typed = self.folder_var.get()
        if typed == self._last_folder_text:
            return
        self.set_results_folder(typed, by_user=True)

    def set_results_folder(self, path: str, *, suggested: bool = False, by_user: bool = False) -> None:
        cleaned = st.clean_folder_text(path)
        self.state.set_results_dir(cleaned, suggested=suggested, by_user=by_user)
        self.show_results_folder()
        self.state.notify("results")
        self.app.check_folder(0)
        self.app.notices.dismiss("unfinished")
        if cleaned and not self.state.demo:               # audit 11.6: a run that never finished there?
            offer = getattr(self.app, "offer_interrupted", None)
            if callable(offer):
                offer(cleaned)

    def show_results_folder(self) -> None:
        """Put the folder of the state in the field (the check may have added a sub-folder)."""
        self._last_folder_text = self.state.results_dir
        if self.folder_var.get() != self.state.results_dir:
            self.folder_var.set(self.state.results_dir)
            try:
                self.folder_entry.xview_moveto(1.0)
            except tk.TclError:
                pass
        self._refresh_folder_lines()

    def _suggest_folder(self) -> None:
        """Audit 4.1: the first samples are in and no folder is chosen: suggest one beside the reads."""
        if (self.state.results_dir or self.state.results_by_user or self._suggesting or self.state.demo
                or not self.state.samples):
            return
        snapshot = [copy.copy(s) for s in self.state.samples]
        self._suggesting = True

        def done(path: Any, _error: Optional[BaseException]) -> None:
            self._suggesting = False
            if path and not self.state.results_dir and self.state.samples:
                self.set_results_folder(str(path), suggested=True)
            else:
                self.state.notify("results")

        self.tasks.submit(lambda _report: st.suggest_results_dir(snapshot), done)

    def _refresh_folder_lines(self) -> None:
        verdict = st.judge_folder(self.state)
        self.folder_info.configure(text=verdict.info or texts.RESULTS_FOLDER_NOTE)
        if verdict.problem:
            words, kind = verdict.problem, "small_bad"
        else:
            parts = list(verdict.notes) + ([verdict.existing] if verdict.existing else [])
            words = " ".join(parts)
            warned = any(note != texts.RESULTS_FOLDER_SUGGESTED and not note.startswith(
                st.Words.RESULTS_NEW_FOLDER.split("{")[0]) for note in verdict.notes)
            kind = "small_warn" if warned else "small"
        if words:
            self.folder_notes.configure(text=words, style=w.style_for(self.folder_notes.master, _NOTE_STYLES[kind]))
            if not self._notes_shown:
                self.folder_notes.grid()
                self._notes_shown = True
        elif self._notes_shown:
            self.folder_notes.grid_remove()
            self._notes_shown = False

    # ====================================================================== plumbing
    def commit(self) -> None:
        """Before Start reads the state: take a folder that is still being typed."""
        self.commit_folder()

    def _say(self, words: str, link: str = "", command: Any = None) -> None:
        """The line under the list: what a search is doing, or what it found."""
        self.activity.configure(text=words)
        if link and command is not None:
            self.activity_link.configure(text=link, command=command)
            self.activity_link.grid()
        else:
            self.activity_link.grid_remove()

    @property
    def activity_text(self) -> str:
        return str(self.activity.cget("text"))

    def _state_changed(self, what: str) -> None:
        if what in ("results", "running", "trim"):
            if what == "results" and self.folder_var.get() != self.state.results_dir and not self._typing_folder():
                self.show_results_folder()
            if what == "results":
                self.refresh()                           # the folder can leave rows out, or bring them back
            else:
                self._refresh_buttons()
                self._refresh_folder_lines()

    def _typing_folder(self) -> bool:
        try:
            return self.focus_get() is self.folder_entry
        except (KeyError, tk.TclError):
            return False

    def _recheck_tick(self) -> None:
        """Audit 2.18: a file that was still being copied becomes Ready by itself."""
        self._recheck_job = None
        try:
            if (self.winfo_ismapped() and not self.state.running and not self.state.demo
                    and not self.state.checking and not self.state.searching and self.table.editing is None):
                again = [s for s in self.state.samples
                         if not s.ready and not pairing.is_name_clash(s) and s.lanes
                         and all(a and b for a, b in s.lanes)]
                if again:
                    self._look_into(again[:RECHECK_MOST], quiet=True)
            self._recheck_job = self.after(RECHECK_MS, self._recheck_tick)
        except tk.TclError:
            pass

    def _destroyed(self, event: Any) -> None:
        if event.widget is not self:
            return
        for job in (self._recheck_job, self._refresh_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        if self._stop_search is not None:
            self._stop_search.set()


def _nearest_folder(path: str) -> str:
    """The folder itself, or the nearest one above it that exists (where Browse opens)."""
    current = path
    for _ in range(64):
        if not current:
            return ""
        try:
            if os.path.isdir(current):
                return current
        except (OSError, ValueError):
            return ""
        parent = os.path.dirname(current)
        if parent == current:
            return ""
        current = parent
    return ""
