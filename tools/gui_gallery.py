# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Widget gallery: one window with every building block of the window in every state.

    python tools/gui_gallery.py                 light palette, scale of the screen
    python tools/gui_gallery.py --dark          dark palette
    python tools/gui_gallery.py --scale 1.5     as on a 150 % screen (1.0 = 96 dpi)
    python tools/gui_gallery.py --shot OUT.png  save a picture of everything and exit
    python tools/gui_gallery.py --high-contrast native theme and system colours

Used while the look is being built and whenever it is changed: run it in both palettes
and at both scales and look at the result. The picture written by ``--shot`` holds the
whole gallery (the window is scrolled and the parts are joined) followed by the dialogs.
``--shot`` needs Pillow; the gallery itself does not.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter as tk
from typing import Any, Callable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tesseract_win import branding                                    # noqa: E402
from tesseract_win.gui import texts, theme, widgets as w              # noqa: E402
from tesseract_win.model import RunStatus, SampleState, Stage         # noqa: E402

LOG_SAMPLE = """[1/7] loading reads
  reads: 1,204,118 pairs, 361 Mb
[2/7] correcting read errors
[3/7] building the assembly graph (k = 21)
[3/7] building the assembly graph (k = 33)
[3/7] building the assembly graph (k = 55)
  graph: 8,412 nodes, 9,120 edges
[4/7] resolving repeats with the read pairs"""


# --------------------------------------------------------------------------- scrolling

class ScrollArea(tk.Frame):
    """A page that scrolls vertically. Content goes into ``inner``."""

    def __init__(self, parent: Any, look: theme.Theme):
        super().__init__(parent, background=look.palette.bg)
        self.canvas = tk.Canvas(self, background=look.palette.bg, highlightthickness=0, borderwidth=0)
        self.bar = w.ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                   style=look.style_name("Vertical.TScrollbar", "page"))
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner = w.Frame(self.canvas, ground="page")
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")
        self.canvas.bind_all("<Button-4>", lambda e: self._wheel(e, -1), add="+")
        self.canvas.bind_all("<Button-5>", lambda e: self._wheel(e, +1), add="+")

    def _wheel(self, event: Any, direction: Optional[int] = None) -> None:
        target = event.widget
        if isinstance(target, (w.ttk.Treeview, tk.Text)):
            return                                   # tables and the log scroll themselves
        if isinstance(target, tk.Label) and isinstance(getattr(target, "master", None), w.ttk.Treeview):
            return
        if direction is None:
            direction = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(direction * 3, "units")


# --------------------------------------------------------------------------- sections

def titled_card(parent: Any, title: str, intro: str = "") -> Tuple[w.Card, w.Frame]:
    card = w.Card(parent, padding=18)
    section = w.Section(card, title, intro)
    section.pack(fill="both", expand=True)
    return card, section.body


def caption(parent: Any, text: str) -> Any:
    return w.label(parent, text, "small")


def states_grid(parent: Any, look: theme.Theme, rows: List[Tuple[str, Callable[[Any], Any]]],
                states: List[Tuple[str, List[str]]]) -> w.Frame:
    """One row per control, one column per forced ttk state."""
    grid = w.Frame(parent)
    for column, (name, _flags) in enumerate(states):
        caption(grid, name).grid(row=0, column=column + 1, sticky="w", padx=(0, look.px(14)), pady=(0, look.px(4)))
    for row, (name, make) in enumerate(rows, start=1):
        caption(grid, name).grid(row=row, column=0, sticky="w", padx=(0, look.px(14)))
        for column, (_state, flags) in enumerate(states):
            made = make(grid)
            if flags:
                made.state(flags)
            made.grid(row=row, column=column + 1, sticky="w", padx=(0, look.px(14)), pady=look.px(4))
    return grid


def section_navigation(parent: Any, look: theme.Theme, help_line: w.HelpLine) -> w.Frame:
    """Two steppers: in use (the second step as under the mouse in a picture), and locked for a run."""
    box = w.Frame(parent)
    stepper = w.Stepper(box, command=lambda key: stepper.set_current(key), help_line=help_line)
    stepper.set_sub("samples", texts.step_ready(40))
    stepper.set_sub("trimming", texts.STEP_STANDARD)
    stepper.set_sub("assembly", texts.STEP_CHANGED, "accent")
    stepper.pack(fill="x")
    locked = w.Stepper(box, current="run")
    locked.set_sub("samples", texts.step_ready(40))
    locked.set_sub("trimming", texts.STEP_STANDARD)
    locked.set_sub("assembly", texts.STEP_CHANGED, "accent")
    locked.set_sub("run", texts.STEP_RUNNING, "accent")
    locked.lock()
    locked.pack(fill="x", pady=(look.px(6), 0))
    box.stepper = stepper                                              # type: ignore[attr-defined]
    return box


def section_buttons(parent: Any, look: theme.Theme, wide: bool = False) -> w.Card:
    card, outer = titled_card(parent, "Buttons, tick boxes, fields",
                              "Each control in its states. Start is the only primary button of the program.")
    body = w.Frame(outer)
    body.pack(side="left", anchor="n")
    side = w.Frame(outer) if wide else body
    if wide:
        side.pack(side="left", anchor="n", padx=(look.px(48), 0))
    states = [("normal", []), ("hover", ["active"]), ("pressed", ["pressed", "active"]),
              ("focus", ["focus"]), ("disabled", ["disabled"])]
    rows: List[Tuple[str, Callable[[Any], Any]]] = [
        ("plain", lambda p: w.button(p, texts.BUTTON["add_files"])),
        ("primary", lambda p: w.button(p, texts.BUTTON["start"], kind="primary")),
        ("danger", lambda p: w.button(p, texts.BUTTON["stop_now"], kind="danger")),
        ("default", lambda p: w.button(p, texts.BUTTON["keep_running"], kind="default")),
        ("link", lambda p: w.LinkLabel(p, texts.BUTTON["try_example"])),
        ("tool", lambda p: w.button(p, texts.BUTTON["show_log"], kind="tool")),
    ]
    states_grid(body, look, rows, states).pack(anchor="w")
    if not wide:
        w.hairline(body).pack(fill="x", pady=look.px(12))

    def ticked(p: Any, on: bool, cls: Callable[..., Any]) -> Any:
        if cls is w.tick:
            return w.tick(p, "Trim poly-G", tk.BooleanVar(p, value=on))
        return w.radio(p, "Standard", tk.StringVar(p, value="a" if on else "b"), "a")

    rows = [
        ("tick, on", lambda p: ticked(p, True, w.tick)),
        ("tick, off", lambda p: ticked(p, False, w.tick)),
        ("radio, on", lambda p: ticked(p, True, w.radio)),
        ("radio, off", lambda p: ticked(p, False, w.radio)),
    ]
    states_grid(side, look, rows, states).pack(anchor="w")
    w.hairline(side).pack(fill="x", pady=look.px(12))

    def field(p: Any) -> Any:
        made = w.entry(p, width=10)
        made.insert(0, "D:\\Results")
        return made

    def number(p: Any) -> Any:
        return w.NumberField(p, value=500, minimum=1, maximum=100000)

    field_states = [("normal", []), ("hover", ["hover"]), ("focus", ["focus"]), ("not valid", ["invalid"]),
                    ("read-only", ["readonly"]), ("disabled", ["disabled"])]
    states_grid(side, look, [("text field", field), ("number field", number)], field_states).pack(anchor="w")
    return card


def sample_rows() -> List[dict]:
    folder = "D:\\Sequencing\\Run 2026-11-12 (Klebsiella)\\"
    rows = []
    plain = [("KP_001", 412, True), ("KP_002", 388, True), ("KP_003", 401, False)]
    for index, (name, size, trim) in enumerate(plain):
        r1, r2 = "%s_S%d_L001_R1_001.fastq.gz" % (name, index + 1), "%s_S%d_L001_R2_001.fastq.gz" % (name, index + 1)
        rows.append({"id": "s%d" % index, "checked": trim, "status": w.status_kind(SampleState.READY),
                     "values": {"sample": name, "read1": r1, "read2": r2, "size": "%d MB" % size,
                                "status": texts.SAMPLE_STATE_LABEL[SampleState.READY]},
                     "tips": {"read1": folder + r1, "read2": folder + r2}})
    long_name = "Klebsiella_pneumoniae_ward7_isolate_2026_0041_rerun_S14_L001_R1_001.fastq.gz"
    rows.append({"id": "long", "checked": True, "status": w.status_kind(SampleState.READY_NOTE),
                 "values": {"sample": "Klebsiella_pneumoniae_ward7_isolate_2026_0041_rerun",
                            "read1": long_name + " " + texts.MORE_LANES.format(n=3),
                            "read2": long_name.replace("_R1_", "_R2_") + " " + texts.MORE_LANES.format(n=3),
                            "size": "1.9 GB",
                            "status": "Ready. Very large for a bacterial isolate. It will be slow and may "
                                      "need more memory than this PC has."},
                 "tips": {"read1": folder + long_name}})
    rows.append({"id": "trimmed", "checked": False, "status": w.status_kind(SampleState.READY),
                 "values": {"sample": "SA_17", "read1": "SA_17_trimmed_R1.fastq.gz", "read2": "SA_17_trimmed_R2.fastq.gz",
                            "size": "251 MB", "status": "Ready. Already trimmed by %s." % branding.APP_NAME}})
    rows.append({"id": "nomate", "checked": None, "dim": True, "status": "bad",
                 "values": {"sample": "EC_09", "read1": "EC_09_R1.fastq.gz", "read2": "", "size": "198 MB",
                            "status": "Left out: no Read 2 file found. Each sample needs two files in the same "
                                      "folder, for example X_R1.fastq.gz and X_R2.fastq.gz."}})
    rows.append({"id": "dup", "checked": None, "dim": True, "status": "bad",
                 "values": {"sample": "KP_001", "read1": "KP_001_R1.fastq.gz", "read2": "KP_001_R2.fastq.gz",
                            "size": "377 MB",
                            "status": "Left out: two samples are called KP_001. Rename one of them "
                                      "(double-click the name)."}})
    for index in range(7, 12):
        name = "KP_%03d" % index
        rows.append({"id": "s%d" % index, "checked": True, "status": "ok",
                     "values": {"sample": name, "read1": name + "_R1.fastq.gz", "read2": name + "_R2.fastq.gz",
                                "size": "%d MB" % (300 + 17 * index), "status": texts.SAMPLE_STATE_LABEL[SampleState.READY]}})
    return rows


def samples_columns() -> List[w.Column]:
    names = texts.SAMPLES_COLUMNS
    return [
        w.Column("sample", names["sample"], width=150, editable=True, shorten="middle", hover=texts.HOVER["sample_column"]),
        w.Column("read1", names["read1"], width=210, stretch=True, shorten="middle", hover=texts.HOVER["read1_column"]),
        w.Column("read2", names["read2"], width=210, stretch=True, shorten="middle", hover=texts.HOVER["read2_column"]),
        w.Column("size", names["size"], width=80, anchor="e", hover=texts.HOVER["size_column"]),
        w.Column("status", names["status"], width=250, stretch=True, status=True, hover=texts.HOVER["status_column"]),
    ]


def section_samples(parent: Any, look: theme.Theme, help_line: w.HelpLine) -> Tuple[w.Card, w.CheckTable]:
    card, body = titled_card(parent, "Table with tick boxes",
                             "Ticked, unticked and not available; a selected row; names shortened in the middle; "
                             "coloured status words. Double-click a name to change it.")
    bar = w.Frame(body)
    bar.pack(fill="x", pady=(0, look.px(8)))
    for index, key in enumerate(("add_files", "add_folder", "remove", "clear_list")):
        made = w.button(bar, texts.BUTTON[key])
        made.pack(side="left", padx=(0 if index == 0 else look.px(8), 0))
        w.explain(made, texts.HOVER[key], help_line)

    def taken(row_id: str, text: str) -> str:
        if not text.strip():
            return texts.RENAME_EMPTY
        if len(text) > 60:
            return texts.RENAME_TOO_LONG
        if not all(ch.isalnum() or ch in "-_." for ch in text):
            return texts.RENAME_BAD_CHARACTERS
        return ""

    table = w.CheckTable(body, samples_columns(), check_title=texts.SAMPLES_COLUMNS["trim"],
                         check_hover=texts.HOVER["trim_column"], height=9, help_line=help_line,
                         validate_rename=taken)
    table.set_rows(sample_rows())
    table.select(["s1"])
    table.pack(fill="both", expand=True)
    under = w.Frame(body)
    under.pack(fill="x", pady=(look.px(8), 0))
    w.label(under, texts.TRIMMING_LABEL, "soft").pack(side="left")
    w.button(under, texts.BUTTON["trim_all"], lambda: table.check_all(True)).pack(side="left", padx=(look.px(8), 0))
    w.button(under, texts.BUTTON["trim_none"], lambda: table.check_all(False)).pack(side="left", padx=(look.px(8), 0))
    plain, link = texts.ready_counter(10, 2)
    w.LinkLabel(under, link, lambda: table.select(["nomate", "dup"])).pack(side="right")
    w.label(under, plain, "soft").pack(side="right", padx=(0, look.px(4)))

    folder = w.Frame(body)
    folder.pack(fill="x", pady=(look.px(14), 0))
    folder.columnconfigure(1, weight=1)
    w.label(folder, texts.RESULTS_FOLDER_LABEL, "bold").grid(row=0, column=0, sticky="w", padx=(0, look.px(10)))
    path = w.entry(folder)
    path.insert(0, "D:\\Sequencing\\TesserACT_results")
    path.grid(row=0, column=1, sticky="ew")
    w.button(folder, texts.BUTTON["browse"]).grid(row=0, column=2, padx=(look.px(8), 0))
    w.label(folder, texts.RESULTS_FOLDER_NOTE + " " + texts.space_line("61 GB", "D:", "212 GB"), "small").grid(
        row=1, column=1, sticky="w", pady=(look.px(4), 0))
    return card, table


def section_empty(parent: Any, look: theme.Theme, help_line: w.HelpLine) -> Tuple[w.Card, w.CheckTable]:
    card, body = titled_card(parent, "Empty list, and a name being changed",
                             "Double-click a name or press F2. Enter accepts, Esc cancels; a name that "
                             "cannot be used is marked and explained in the help line.")
    table = w.CheckTable(body, samples_columns(), check_title=texts.SAMPLES_COLUMNS["trim"], height=5)
    table.set_empty(texts.EMPTY_STATE, texts.BUTTON["try_example"], lambda: None, texts.EMPTY_STATE_LINK_LEAD)
    table.pack(fill="both", expand=True)

    def acceptable(_row_id: str, text: str) -> str:
        if not text.strip():
            return texts.RENAME_EMPTY
        if len(text) > 60:
            return texts.RENAME_TOO_LONG
        return "" if all(ch.isalnum() or ch in "-_." for ch in text) else texts.RENAME_BAD_CHARACTERS

    names = w.CheckTable(body, samples_columns(), check_title=texts.SAMPLES_COLUMNS["trim"], height=3,
                         validate_rename=acceptable, help_line=help_line)
    names.set_rows(sample_rows()[:3])
    names.pack(fill="both", expand=True, pady=(look.px(10), 0))
    return card, names


def section_options(parent: Any, look: theme.Theme, help_line: w.HelpLine) -> w.Card:
    card, body = titled_card(parent, "Option rows", texts.PAGE_INTRO["trimming"])
    for key in ("remove_adapters", "trim_polyg"):
        w.TickRow(body, texts.TRIM_LABEL[key], value=True, help_text=texts.TRIM_HOVER[key],
                  help_line=help_line).pack(anchor="w")
    w.TickRow(body, texts.TRIM_LABEL["keep_trimmed_reads"], value=False,
              help_text=texts.TRIM_HOVER["keep_trimmed_reads"], help_line=help_line).pack(anchor="w")
    off = w.TickRow(body, texts.TRIM_LABEL["remove_short_reads"], value=True)
    off.set_enabled(False)
    off.pack(anchor="w")
    w.TickRow(body, texts.ASSEMBLY_LABEL["know_genome_size"], value=True,
              number=dict(value=5.5, minimum=0.1, maximum=20, decimal=True),
              suffix=texts.ASSEMBLY_SUFFIX["genome_size_mb"],
              help_text=texts.ASSEMBLY_HOVER["know_genome_size"], help_line=help_line).pack(anchor="w")
    w.TickRow(body, texts.ASSEMBLY_LABEL["drop_short_contigs"], value=False,
              number=dict(value=500, minimum=1, maximum=100000),
              suffix=texts.ASSEMBLY_SUFFIX["min_contig"],
              help_text=texts.ASSEMBLY_HOVER["drop_short_contigs"], help_line=help_line).pack(anchor="w")

    closed = w.Disclosure(body, help_line=help_line)
    closed.set_changed(True)
    closed.pack(fill="x", pady=(look.px(8), 0))
    w.label(closed.body, "(content)", "muted").pack(anchor="w")

    more = w.Disclosure(body, opened=True, help_line=help_line)
    more.pack(fill="x", pady=(look.px(4), 0))
    inner = more.body
    w.NumberRow(inner, texts.TRIM_LABEL["end_quality"], number=dict(value=10, minimum=1, maximum=36),
                help_text=texts.TRIM_HOVER["end_quality"], help_line=help_line).pack(anchor="w")
    w.NumberRow(inner, texts.TRIM_LABEL["min_length"], number=dict(value=50, minimum=0, maximum=10000),
                suffix=texts.TRIM_SUFFIX["min_length"], help_text=texts.TRIM_HOVER["min_length"],
                help_line=help_line).pack(anchor="w")
    two = w.OptionRow(inner, help_text=texts.TRIM_HOVER["max_low_percent"], help_line=help_line)
    two.add_text(texts.TRIM_LABEL["max_low_percent"])
    two.add_number(value=40, minimum=0, maximum=100)
    two.add_text(texts.TRIM_LABEL["base_quality"])
    two.add_number(value=15, minimum=0, maximum=60)
    two.finish().pack(anchor="w")
    cut = w.OptionRow(inner, help_text=texts.TRIM_HOVER["fixed_trim"], help_line=help_line)
    cut.add_tick(texts.TRIM_LABEL["fixed_trim"], tk.BooleanVar(cut, value=False))
    cut.add_text(texts.TRIM_LABEL["trim_front"])
    cut.add_number(value=0, minimum=0, maximum=100).set_enabled(False)
    cut.add_text(texts.TRIM_LABEL["trim_tail"])
    cut.add_number(value=0, minimum=0, maximum=100).set_enabled(False)
    cut.finish().pack(anchor="w")
    own = w.OptionRow(inner, help_text=texts.TRIM_HOVER["own_adapters"], help_line=help_line)
    own.add_tick(texts.TRIM_LABEL["own_adapters"], tk.BooleanVar(own, value=True))
    own.add_text(texts.TRIM_LABEL["adapter_r1"])
    own.add_entry(width=20).insert(0, "AGATCGGAAGAGCACACG")
    own.add_text(texts.TRIM_LABEL["adapter_r2"])
    own.add_entry(width=20)
    own.finish().pack(anchor="w")
    w.ChoiceRow(inner, texts.ASSEMBLY_LABEL["mode"],
                [(mode, texts.MODE_LABEL[mode], texts.MODE_HOVER[mode]) for mode in texts.MODE_LABEL],
                value="standard", help_text=texts.ASSEMBLY_HOVER["mode"], help_line=help_line).pack(anchor="w")
    threads = w.NumberRow(inner, texts.ASSEMBLY_LABEL["threads"], number=dict(value=8, minimum=1, maximum=8),
                          suffix=texts.ASSEMBLY_SUFFIX["threads"].format(n=8),
                          help_text=texts.ASSEMBLY_HOVER["threads"], help_line=help_line)
    threads.pack(anchor="w")
    foot = w.Frame(body)
    foot.pack(fill="x", pady=(look.px(10), 0))
    reset = w.button(foot, texts.BUTTON["reset"])
    reset.pack(side="right")
    w.explain(reset, texts.HOVER["reset_trimming"], help_line)
    return card


def section_notices(parent: Any, look: theme.Theme) -> w.Frame:
    box = w.Frame(parent)
    caption(box, "Notice bars").pack(anchor="w", pady=(0, look.px(6)))
    bars = w.NoticeBar(box)
    bars.pack(fill="x")
    bars.show("resume", "The last run was interrupted on 17 Nov at 03:12 because Windows shut down or "
              "restarted. 23 of 40 samples are finished. " + texts.NOTHING_LOST, "info",
              actions=[(texts.BUTTON["continue_run"], lambda: None), (texts.BUTTON["not_now"], lambda: bars.dismiss("resume"))],
              dismissible=False)
    bars.show("battery", texts.ON_BATTERY, "warn")
    bars.show("missing", texts.missing_part("engines\\tesseract-asm.exe"), "bad",
              actions=[(texts.BUTTON["check_again"], lambda: None)], dismissible=False)
    bars.show("done", "Finished at 03:42. It took 9 h 14 min. 36 done, 2 done (check notes), 2 failed.", "ok")
    return box


def run_rows() -> List[dict]:
    rows = []
    data = [
        ("KP_001", RunStatus.DONE, "71", "5,482,113", "214,880", ""),
        ("KP_002", RunStatus.DONE_NOTES, "312", "5,391,027", "38,412", "Low depth (about 22x): expect a fragmented assembly."),
        ("KP_003", RunStatus.FAILED, "", "", "", "A read file is incomplete or damaged (KP_003_R2.fastq.gz). Copy it again from the original, then try again."),
        ("KP_004", RunStatus.SKIPPED, "64", "5,501,870", "240,113", "Already assembled on 3 Nov 2026."),
        ("KP_005", RunStatus.ASSEMBLING, "", "", "", ""),
        ("KP_006", RunStatus.WAITING, "", "", "", ""),
        ("KP_007", RunStatus.STOPPED, "", "", "", "Stopped. The earlier assembly was kept."),
        ("KP_008", RunStatus.NOT_STARTED, "", "", "", ""),
    ]
    for name, status, contigs, total, n50, note in data:
        rows.append({"id": name, "status": w.status_kind(status), "dim": status in (RunStatus.WAITING, RunStatus.NOT_STARTED),
                     "values": {"sample": name, "status": texts.RUN_STATUS_LABEL[status], "contigs": contigs,
                                "total_length": total, "n50": n50, "notes": note}})
    return rows


def section_run(parent: Any, look: theme.Theme, help_line: w.HelpLine) -> Tuple[w.Card, List[Callable[[], None]]]:
    card, body = titled_card(parent, texts.sample_header(13, 40, "KP_013"))
    px = look.px
    actions: List[Callable[[], None]] = []
    bar = w.ttk.Progressbar(body, maximum=40, value=12)
    bar.pack(fill="x")
    line = w.Frame(body)
    line.pack(fill="x", pady=(px(8), 0))
    w.label(line, texts.clocks("06:41", "2 h 14 min") + " " + texts.time_left(4 * 3600), "soft").pack(side="left")

    chips = w.StageChips(body)
    chips.show_stage(Stage.JOIN_LANES)
    for stage, state in ((Stage.JOIN_LANES, "skipped"), (Stage.TRIM, "done"), (Stage.CHECK_READS, "done"),
                         (Stage.ASSEMBLE, "running"), (Stage.SUMMARISE, "waiting")):
        chips.set(stage, state)
    chips.pack(anchor="w", pady=(px(12), 0))
    failed = w.StageChips(body)
    for stage, state in ((Stage.TRIM, "done"), (Stage.CHECK_READS, "failed"), (Stage.ASSEMBLE, "waiting"),
                         (Stage.SUMMARISE, "waiting")):
        failed.set(stage, state)
    failed.pack(anchor="w", pady=(px(8), 0))

    working = w.WorkingIndicator(body, "Step 3 of 7: building the assembly graph (k = 55)", kind="body")
    working.start()
    working.pack(anchor="w", pady=(px(12), 0))
    w.WrapLabel(body, texts.working_line(7.6, "3.1 GB") + ". " + texts.SILENCE_IS_NORMAL, "soft", wrap=700).pack(
        fill="x", pady=(px(4), 0))
    idle = w.WorkingIndicator(body, "Stopped: not turning", kind="muted")
    idle.pack(anchor="w", pady=(px(4), 0))
    w.WrapLabel(body, texts.ADVICE, "small", wrap=700).pack(fill="x", pady=(px(8), px(10)))

    names = texts.RUN_COLUMNS
    table = w.CheckTable(body, [
        w.Column("sample", names["sample"], width=120, hover=texts.HOVER["run_sample_column"]),
        w.Column("status", names["status"], width=200, status=True, hover=texts.HOVER["run_status_column"]),
        w.Column("contigs", names["contigs"], width=76, anchor="e", hover=texts.HOVER["contigs_column"]),
        w.Column("total_length", names["total_length"], width=106, anchor="e", hover=texts.HOVER["total_length_column"]),
        w.Column("n50", names["n50"], width=86, anchor="e", hover=texts.HOVER["n50_column"]),
        w.Column("notes", names["notes"], width=260, stretch=True, hover=texts.HOVER["notes_column"]),
    ], checks=False, height=8, removable=False, help_line=help_line)
    table.set_rows(run_rows())
    table.select(["KP_003"])
    table.pack(fill="both", expand=True)

    strip = w.Well(body, padding=(12, 10))
    strip.pack(fill="x", pady=(px(8), 0))
    strip.columnconfigure(0, weight=1)
    w.WrapLabel(strip, "KP_003: " + run_rows()[2]["values"]["notes"], "body").grid(row=0, column=0, sticky="ew")
    w.button(strip, texts.BUTTON["open_log"]).grid(row=0, column=1, padx=(px(10), 0))
    w.button(strip, texts.BUTTON["open_folder"]).grid(row=0, column=2, padx=(px(8), 0))

    log = w.LogPane(body, height=7)
    log.append(LOG_SAMPLE)
    log.pack(fill="both", expand=True, pady=(px(10), 0))
    actions.append(lambda: log.append("[%s] one more line" % time.strftime("%H:%M:%S")))

    foot = w.Frame(body)
    foot.pack(fill="x", pady=(px(12), 0))
    stop = w.button(foot, texts.BUTTON["stop"])
    stop.pack(side="left")
    w.explain(stop, texts.HOVER["stop"], help_line)
    w.button(foot, texts.BUTTON["hide_log"], kind="tool").pack(side="left", padx=(px(8), 0))
    w.button(foot, texts.BUTTON["open_summary"]).pack(side="right")
    w.button(foot, texts.BUTTON["open_results"]).pack(side="right", padx=(0, px(8)))
    return card, actions


def section_bottom(parent: Any, look: theme.Theme, help_line: w.HelpLine) -> w.Frame:
    box = w.Frame(parent)
    px = look.px
    caption(box, "Help line: the control in use, and a field that was set back").pack(anchor="w")
    first = w.HelpLine(box, default=texts.TRIM_HOVER["remove_adapters"])
    first.pack(fill="x", pady=(px(4), 0))
    second = w.HelpLine(box)
    second.say(texts.number_reset(0, 40, 10), "warn", seconds=0)
    second.pack(fill="x", pady=(px(2), px(8)))

    caption(box, "Bottom bar: ready, with a note; not ready, with the reason; first time, with the hint").pack(anchor="w")
    one = w.BottomBar(box, help_line=help_line)
    one.set_next("trimming")
    one.set_back(enabled=False)
    one.set_start(38)
    plain, link = texts.ready_counter(38, 2)
    one.set_note("", link_text=link)
    one.pack(fill="x", pady=(px(6), px(12)))
    two = w.BottomBar(box)
    two.set_next("assembly")
    two.set_start(0, enabled=False, reason=texts.start_reason(0, 0, False))
    two.pack(fill="x", pady=(0, px(12)))
    three = w.BottomBar(box)
    three.set_next("trimming")
    three.set_back(visible=False)
    three.set_start(1)
    three.set_hint(texts.FIRST_TIME_HINT)
    three.pack(fill="x")
    return box


def section_type(parent: Any, look: theme.Theme) -> w.Card:
    card, body = titled_card(parent, "Type, colour and the mark")
    px = look.px
    w.PageHeader(body, texts.PAGE_TITLE["assembly"], texts.PAGE_INTRO["assembly"]).pack(fill="x")
    samples = w.Frame(body)
    samples.pack(fill="x", pady=(px(10), 0))
    for index, (kind, text) in enumerate((("heading", "Heading 11"), ("body", "Body 10"), ("soft", "Soft"),
                                          ("muted", "Muted"), ("small", "Small 9"), ("bold", "Bold"),
                                          ("accent", "Accent"), ("ok", "Done"), ("warn", "Check notes"),
                                          ("bad", "Failed"), ("mono", "mono 0123"))):
        w.label(samples, text, kind).grid(row=0, column=index, padx=(0, px(14)), sticky="w")
    swatches = w.Frame(body)
    swatches.pack(fill="x", pady=(px(10), 0))
    names = ("bg", "surface", "surface_alt", "surface_sunk", "border", "border_strong", "text", "text_soft",
             "muted", "accent", "accent_soft", "ok", "warn", "bad", "ok_soft", "warn_soft", "bad_soft", "row_alt")
    for index, name in enumerate(names):
        cell = tk.Frame(swatches, background=look.palette.border, highlightthickness=0)
        cell.grid(row=index // 6 * 2, column=index % 6, padx=(0, px(10)), pady=(px(4), 0), sticky="w")
        tk.Frame(cell, background=look.colour(name), width=px(96), height=px(20)).pack(padx=look.hair, pady=look.hair)
        caption(swatches, name).grid(row=index // 6 * 2 + 1, column=index % 6, sticky="w")
    marks = w.Frame(body)
    marks.pack(fill="x", pady=(px(12), 0))
    for size in (16, 24, 32, 48):
        w.ttk.Label(marks, image=look.icons.mark(look.px(size)), style=w.style_for(marks, "TLabel")).pack(
            side="left", padx=(0, px(12)), anchor="s")
    w.label(marks, branding.ATTRIBUTION, "small").pack(side="left", padx=(px(8), 0), anchor="s")
    return card


def dialog_specs() -> List[texts.Question]:
    names = ["KP_%03d" % index for index in range(1, 13)]
    return [
        texts.stop_question("KP_002", RunStatus.ASSEMBLING, 14 * 60, 12),
        texts.overwrite_question(12, 40, 8, "not trimmed; mode Careful", names),
        texts.overwrite_question(40, 40),
        texts.close_question("KP_002", RunStatus.ASSEMBLING, 14 * 60, 12),
    ]


def section_dialogs(parent: Any, look: theme.Theme, answers: tk.StringVar) -> w.Card:
    card, body = titled_card(parent, "Dialogs", "Our own windows, so the buttons can carry words. Esc and the "
                                                "close box mean the cancel choice; Enter means the marked one.")
    row = w.Frame(body)
    row.pack(fill="x")
    captions = ("Stop", "Skip or overwrite", "Overwrite all", "Close during a run")
    for index, question in enumerate(dialog_specs()):
        w.button(row, captions[index], lambda q=question: answers.set("Answer: " + w.Dialog.ask(card, q))).pack(
            side="left", padx=(0 if index == 0 else look.px(8), 0))
    w.label(body, "", "soft", textvariable=answers).pack(anchor="w", pady=(look.px(8), 0))
    return card


# --------------------------------------------------------------------------- the window

def build(root: tk.Tk, look: theme.Theme, columns: int) -> Tuple[ScrollArea, dict]:
    px = look.px
    area = ScrollArea(root, look)
    area.pack(fill="both", expand=True)
    page = w.Frame(area.inner, ground="page", padding=(20, 16, 20, 20))
    page.pack(fill="both", expand=True)
    head = w.Frame(page)
    head.pack(fill="x", pady=(0, px(10)))
    w.label(head, "%s widget gallery" % branding.APP_NAME, "title").pack(side="left")
    mode = "high contrast" if look.high_contrast else look.palette.name
    w.label(head, "%s palette, scale %.2f, %s" % (mode, look.scale, look.families["text"]), "muted").pack(
        side="left", padx=(px(14), 0), pady=(px(8), 0))

    help_line = w.HelpLine(page, default="Rest the mouse on a control, or Tab to it: its explanation appears here.")
    navigation = section_navigation(page, look, help_line)
    navigation.pack(fill="x", pady=(0, px(12)))

    grid = w.Frame(page)
    grid.pack(fill="both", expand=True)
    stacks = []
    for column in range(columns):
        grid.columnconfigure(column, weight=1, uniform="gallery")
        stack = w.Frame(grid)
        stack.grid(row=0, column=column, sticky="new", padx=(0 if column == 0 else px(16), 0))
        stacks.append(stack)
    left, right = stacks[0], stacks[-1]
    gap = (0, px(16))

    samples_card, table = section_samples(left, look, help_line)
    samples_card.pack(fill="x", pady=gap)
    section_options(left, look, help_line).pack(fill="x", pady=gap)
    empty_card, names_table = section_empty(left, look, help_line)
    empty_card.pack(fill="x", pady=gap)
    section_notices(right, look).pack(fill="x", pady=(0, px(8)))
    run_card, actions = section_run(right, look, help_line)
    run_card.pack(fill="x", pady=gap)
    section_type(right, look).pack(fill="x", pady=gap)
    answers = tk.StringVar(root, value="")
    section_dialogs(right, look, answers).pack(fill="x", pady=gap)
    section_buttons(page, look, wide=columns > 1).pack(fill="x", pady=gap)

    w.hairline(page).pack(fill="x", pady=(px(4), px(10)))
    help_line.pack(fill="x")
    section_bottom(page, look, help_line).pack(fill="x", pady=(px(8), 0))
    return area, {"table": table, "names": names_table, "help_line": help_line, "actions": actions,
                  "stepper": navigation.stepper}


def settle(root: tk.Tk, seconds: float = 0.25) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)
    root.update_idletasks()
    root.update()


def shoot(root: tk.Tk, look: theme.Theme, area: ScrollArea, parts: dict, path: str) -> None:
    """Scroll through the gallery, join the pictures, add the dialogs, save."""
    from PIL import Image, ImageGrab

    display = os.environ.get("DISPLAY") or None

    def grab(left: int, top: int, width: int, height: int) -> Any:
        full = ImageGrab.grab(xdisplay=display) if sys.platform.startswith("linux") else ImageGrab.grab()
        return full.crop((left, top, left + width, top + height))

    settle(root, 0.6)
    canvas = area.canvas
    view_w, view_h = canvas.winfo_width(), canvas.winfo_height()
    total = max(view_h, area.inner.winfo_reqheight())
    pieces = []
    # a tooltip and a hover state, as the mouse would give them
    table = parts["table"]
    target = table.tree
    parts["stepper"]._set_hover("trimming")              # as with the mouse on the second step
    target.event_generate("<Motion>", warp=True, x=look.px(250), y=int(look.row_height * 4.6))
    settle(root, 0.9)
    y = 0
    while True:
        canvas.yview_moveto(y / float(total))
        settle(root, 0.3)
        top = int(round(canvas.canvasy(0)))
        pieces.append((top, grab(canvas.winfo_rootx(), canvas.winfo_rooty(), view_w, view_h)))
        if y == 0:
            w.hide_tooltips(root)
            root.event_generate("<Motion>", warp=True, x=2, y=2)
            names = parts["names"]                       # a name being changed to one that cannot be used
            if names.begin_rename("s1"):
                names.editor.delete(0, "end")
                names.editor.insert(0, "KP 002 (rerun)")
                names.editor.icursor("end")
                names._check_rename()
        if top + view_h >= total:
            break
        y += view_h
    canvas.yview_moveto(0)
    settle(root, 0.2)

    shots = []
    for question in dialog_specs():
        dialog = w.Dialog(root, title=question.title, body=question.body, choices=question.choices,
                          default=question.default, cancel=question.cancel, details=question.details,
                          details_label=question.details_label)
        dialog.open()
        settle(root, 0.35)
        shots.append(grab(dialog.winfo_rootx(), dialog.winfo_rooty(), dialog.winfo_width(), dialog.winfo_height()))
        if dialog.details is not None:                      # and once more with the details open
            dialog.details.open()
            settle(root, 0.35)
            shots.append(grab(dialog.winfo_rootx(), dialog.winfo_rooty(), dialog.winfo_width(), dialog.winfo_height()))
        dialog.destroy()
        settle(root, 0.1)

    margin = look.px(20)
    rows: List[List[Any]] = [[]]
    used = margin
    for shot in shots:
        if rows[-1] and used + shot.width + margin > view_w:
            rows.append([])
            used = margin
        rows[-1].append(shot)
        used += shot.width + margin
    dialogs_height = sum(max(s.height for s in row) + margin for row in rows if row) + margin
    sheet = Image.new("RGB", (view_w, total + dialogs_height), look.palette.bg)
    for top, piece in pieces:
        sheet.paste(piece, (0, top))
    y = total + margin
    for row in rows:
        x = margin
        for shot in row:
            sheet.paste(shot, (x, y))
            x += shot.width + margin
        if row:
            y += max(s.height for s in row) + margin
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    sheet.save(path)
    print("saved %s (%d x %d)" % (path, sheet.width, sheet.height))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dark", action="store_true", help="the dark palette")
    parser.add_argument("--high-contrast", action="store_true", help="native theme and system colours")
    parser.add_argument("--scale", type=float, default=None, help="screen scaling, 1.0 = 96 dpi (e.g. 1.5)")
    parser.add_argument("--shot", metavar="PATH", default=None, help="save a picture of the whole gallery and exit")
    parser.add_argument("--size", default=None, help="window size in pixels, WIDTHxHEIGHT")
    args = parser.parse_args(argv)

    root = tk.Tk()
    look = theme.init(root, is_dark=args.dark, is_high_contrast=args.high_contrast, scale=args.scale)
    root.title(branding.APP_NAME + " widget gallery")
    try:
        root.iconphoto(True, *look.icons.window_icons())
    except tk.TclError:
        pass
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    if args.size:
        width, height = (int(v) for v in args.size.lower().split("x"))
    else:
        width, height = min(screen_w, 1600), min(screen_h - 40, 1000)
        if args.shot:
            width, height = screen_w, screen_h
    root.geometry("%dx%d+0+0" % (width, height))
    columns = 2 if width / look.scale >= 1500 else 1
    area, parts = build(root, look, columns)

    if args.shot:
        root.update()
        shoot(root, look, area, parts, args.shot)
        root.destroy()
        return 0

    def tick() -> None:
        for action in parts["actions"]:
            action()
        root.after(1500, tick)

    root.after(1500, tick)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
