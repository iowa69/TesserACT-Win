# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Step 2, Trimming (decision D5), and what the two option pages have in common.

On the page, all ticked: Remove adapters, Trim low-quality ends, Remove poor-quality
reads, Remove very short reads, Trim poly-G tails, Keep the trimmed reads. Behind
"More options", closed by default: the five numbers, Trim low-quality starts too, Trim
repeated-base tails, Cut a fixed number of bases, Use my own adapter sequences.

The page shows and changes ``app.state.trim`` (a ``model.TrimSettings``), nothing else.
Every option is explained on hover and in the help line on keyboard focus; the words come
from ``texts``. A number field corrects itself when it is left; a number whose tick is off
is greyed. "More options" carries a mark when something inside is not standard. When no
sample has Trim ticked the controls are greyed under one explaining line.

:class:`OptionsPage` is the frame both option pages are built on: the opening sentence, a
card that scrolls only if the window is too low for it, and "Reset to standard".
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional

from .. import settings_map
from ..model import TrimSettings
from . import state as st
from . import texts
from . import theme as _theme
from . import widgets as w

__all__ = ["ScrollArea", "OptionsPage", "TrimmingPage"]


class ScrollArea(ttk.Frame):
    """Content that scrolls up and down only when it does not fit. Put it in ``inner``."""

    def __init__(self, parent: Any, **options: Any):
        look = self._look = _theme.current(parent)
        self.ground = w.ground_of(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, background=look.ground_colour(self.ground), highlightthickness=0,
                                borderwidth=0, height=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 style=look.style_name("Vertical.TScrollbar", self.ground))
        self.canvas.configure(yscrollcommand=self._scrolled)
        self.inner = w.Frame(self.canvas, ground=self.ground)
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._bar_shown = False
        self.inner.bind("<Configure>", self._fit, add="+")
        self.canvas.bind("<Configure>", self._fit, add="+")
        top = self.winfo_toplevel()
        top.bind("<MouseWheel>", self._wheel, add="+")
        top.bind("<Button-4>", lambda e: self._wheel(e, -1), add="+")
        top.bind("<Button-5>", lambda e: self._wheel(e, +1), add="+")

    @property
    def scrolling(self) -> bool:
        """True while the content is taller than the room it has."""
        return self._bar_shown

    def _fit(self, _event: Any = None) -> None:
        try:
            width = self.canvas.winfo_width()
            self.canvas.itemconfigure(self._window, width=width)
            needed, room = self.inner.winfo_reqheight(), self.canvas.winfo_height()
            self.canvas.configure(scrollregion=(0, 0, width, max(needed, room)))
            wanted = needed > room + 1 and room > 1
            if wanted != self._bar_shown:
                self._bar_shown = wanted
                if wanted:
                    self.bar.grid(row=0, column=1, sticky="ns", padx=(self._look.px(4), 0))
                else:
                    self.bar.grid_remove()
                    self.canvas.yview_moveto(0)
        except tk.TclError:
            pass

    def _scrolled(self, first: str, last: str) -> None:
        self.bar.set(first, last)

    def _wheel(self, event: Any, direction: Optional[int] = None) -> None:
        if not self._bar_shown:
            return
        try:
            if not self.winfo_ismapped():
                return
            target = event.widget
            if isinstance(target, str) or not str(target).startswith(str(self)):
                return
            if direction is None:
                direction = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(direction * 3, "units")
        except tk.TclError:
            pass

    def show(self, widget: Any) -> None:
        """Scroll until ``widget`` (something inside) can be seen."""
        if not self._bar_shown:
            return
        try:
            self.update_idletasks()
            top = widget.winfo_rooty() - self.inner.winfo_rooty()
            total = max(1, self.inner.winfo_reqheight())
            self.canvas.yview_moveto(max(0.0, (top - self._look.px(40)) / float(total)))
        except tk.TclError:
            pass


class OptionsPage(w.Frame):
    """What the Trimming and the Assembly page share.

    A subclass sets ``step_key``, builds its rows in :meth:`build` and says in
    :meth:`gates` which controls can be used right now.
    """

    step_key = ""
    reset_hover = ""

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, ground="page")
        self.app = app
        self.state: st.AppState = app.state
        self.help_line = app.help_line
        look = self.look = _theme.current(self)
        px = look.px
        self.numbers: Dict[str, w.NumberField] = {}
        self.ticks: Dict[str, w.TickRow] = {}
        self.rows: Dict[str, Any] = {}
        self._loading = False

        compact = bool(getattr(app, "compact", False))
        self.intro = w.WrapLabel(self, texts.PAGE_INTRO[self.step_key], "soft", wrap=900)
        self.intro.pack(fill="x", pady=(0, px(6 if compact else 10)))
        self.notice = w.NoticeBar(self)
        self.notice.pack(fill="x")
        self.scroll = ScrollArea(self)
        self.scroll.pack(fill="both", expand=True)
        self.card = w.Card(self.scroll.inner, padding=(20, 8 if compact else 14, 20, 10 if compact else 16))
        self.card.pack(fill="x", anchor="n")
        top = w.Frame(self.card)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        self.body = w.Frame(top)
        self.body.grid(row=0, column=0, sticky="nw")
        self.reset_button = w.button(top, texts.BUTTON["reset"], self.reset)
        self.reset_button.grid(row=0, column=1, sticky="ne")
        w.explain(self.reset_button, self.reset_hover, self.help_line)
        self.below = w.Frame(self.card)
        self.below.pack(fill="x")
        self.build(self.body, self.below)
        self.load()

    # -- for the subclass
    def build(self, parent: Any, below: Any) -> None:
        """Make the rows: those that are always in view in ``parent``, "More options" in ``below``."""
        raise NotImplementedError

    def settings(self) -> Any:
        raise NotImplementedError

    def fresh(self) -> Any:
        raise NotImplementedError

    def store(self, settings: Any) -> None:
        raise NotImplementedError

    def more_changed(self) -> bool:
        return False

    def page_enabled(self) -> bool:
        return True

    def gates(self) -> None:
        """Grey what cannot be used right now."""

    # -- shared machinery
    def set_value(self, name: str, value: Any) -> None:
        """A control was used: put the value in the settings and tell the others."""
        if self._loading:
            return
        setattr(self.settings(), name, value)
        self.changed()

    def changed(self) -> None:
        self.notice.dismiss("example")                    # the user has set this page: the note is out of date
        self.gates()
        self._mark()
        self.state.notify(self.step_key if self.step_key != "trimming" else "trim")

    def _mark(self) -> None:
        more = getattr(self, "more", None)
        if more is not None:
            more.set_changed(self.more_changed())

    def number_row(self, parent: Any, name: str, label: str, suffix: str, hover: str, *,
                   decimal: bool = False, cast: Callable[[float], Any] = int) -> w.NumberRow:
        low, high = settings_map.number_range(name)
        row = w.NumberRow(parent, label, suffix=suffix, help_text=hover, help_line=self.help_line,
                          number=dict(value=getattr(self.settings(), name), minimum=low, maximum=high,
                                      decimal=decimal, command=lambda v, n=name: self.set_value(n, cast(v))))
        self.numbers[name] = row.field
        self.rows[name] = row
        return row

    def tick_row(self, parent: Any, name: str, label: str, hover: str, **options: Any) -> w.TickRow:
        row = w.TickRow(parent, label, value=bool(getattr(self.settings(), name)), help_text=hover,
                        help_line=self.help_line, command=lambda on, n=name: self.set_value(n, bool(on)),
                        **options)
        self.ticks[name] = row
        self.rows[name] = row
        return row

    def commit(self) -> None:
        """Check every number that is still being typed (before Start reads the settings)."""
        for field in self.numbers.values():
            try:
                field.commit()
            except tk.TclError:
                pass

    def load(self) -> None:
        """Show what the settings hold."""
        self._loading = True
        try:
            self.show_settings(self.settings())
        finally:
            self._loading = False
        self.gates()
        self._mark()

    def show_settings(self, settings: Any) -> None:
        for name, row in self.ticks.items():
            row.set(bool(getattr(settings, name)))
        for name, field in self.numbers.items():
            field.set_value(getattr(settings, name))

    def reset(self) -> None:
        """Reset to standard."""
        self.store(self.fresh())
        self.notice.dismiss("example")
        self.load()
        self.state.notify(self.step_key if self.step_key != "trimming" else "trim")

    def refresh(self) -> None:
        """The list changed: grey the page or bring it back."""
        self.gates()

    def open_more(self, opened: bool = True) -> None:
        more = getattr(self, "more", None)
        if more is not None:
            (more.open if opened else more.close)()

    def focus_field(self, name: str) -> None:
        """Put the keyboard on the control of a setting (after Start found it wrong)."""
        target = self.numbers.get(name) or getattr(self, "entries", {}).get(name)
        row = self.rows.get(name)
        if target is None and row is not None:
            target = getattr(row, "tick", None)
        if name in getattr(self, "more_fields", ()):
            self.open_more(True)
        if target is not None:
            try:
                self.scroll.show(target)
                target.focus_set()
            except tk.TclError:
                pass

    @staticmethod
    def enable(widget: Any, enabled: bool) -> None:
        try:
            if hasattr(widget, "set_enabled"):
                widget.set_enabled(enabled)
            else:
                widget.state(["!disabled"] if enabled else ["disabled"])
        except (tk.TclError, AttributeError):
            pass


class TrimmingPage(OptionsPage):
    """Step 2: the options of the trimmer."""

    step_key = "trimming"
    reset_hover = texts.HOVER["reset_trimming"]
    more_fields = st.TRIM_MORE_FIELDS
    ON_PAGE = ("remove_adapters", "trim_low_quality_ends", "remove_poor_reads", "remove_short_reads",
               "trim_polyg", "keep_trimmed_reads")

    def settings(self) -> TrimSettings:
        return self.state.trim

    def fresh(self) -> TrimSettings:
        return TrimSettings()

    def store(self, settings: TrimSettings) -> None:
        self.state.trim = settings

    def more_changed(self) -> bool:
        return st.trim_more_changed(self.state.trim)

    def page_enabled(self) -> bool:
        return not self.state.trimming_off()

    # -- building
    def build(self, parent: Any, below: Any) -> None:
        px = self.look.px
        label, hover, suffix = texts.TRIM_LABEL, texts.TRIM_HOVER, texts.TRIM_SUFFIX
        if getattr(self.app, "compact", False):          # a low screen: the six ticks in two columns
            half = (len(self.ON_PAGE) + 1) // 2
            for index, name in enumerate(self.ON_PAGE):
                self.tick_row(parent, name, label[name], hover[name]).grid(
                    row=index % half, column=index // half, sticky="w", padx=(0, px(40)))
        else:
            for name in self.ON_PAGE:
                self.tick_row(parent, name, label[name], hover[name]).pack(anchor="w")

        self.more = w.Disclosure(below, help_line=self.help_line)
        self.more.pack(fill="x", pady=(px(6), 0))
        inner = self.more.body
        self.left = w.Frame(inner)
        self.right = w.Frame(inner)
        self._two_columns: Optional[bool] = None

        # the numbers behind the ticks above
        self.number_row(self.left, "end_quality", label["end_quality"], "", hover["end_quality"]).pack(anchor="w")
        self.number_row(self.left, "min_mean_quality", label["min_mean_quality"], "",
                        hover["min_mean_quality"]).pack(anchor="w")
        low, high = settings_map.number_range("max_low_percent")
        qlow, qhigh = settings_map.number_range("base_quality")
        share = w.OptionRow(self.left, help_text=hover["max_low_percent"], help_line=self.help_line)
        self.share_label = share.add_text(label["max_low_percent"])
        self.numbers["max_low_percent"] = share.add_number(
            value=self.state.trim.max_low_percent, minimum=low, maximum=high,
            command=lambda v: self.set_value("max_low_percent", int(v)))
        self.share_label2 = share.add_text(label["base_quality"])
        self.numbers["base_quality"] = share.add_number(
            value=self.state.trim.base_quality, minimum=qlow, maximum=qhigh,
            command=lambda v: self.set_value("base_quality", int(v)))
        share.finish().pack(anchor="w")
        self.rows["max_low_percent"] = self.rows["base_quality"] = share
        self.number_row(self.left, "min_length", label["min_length"], suffix["min_length"],
                        hover["min_length"]).pack(anchor="w")

        # off by default
        self.tick_row(self.right, "trim_low_quality_starts", label["trim_low_quality_starts"],
                      hover["trim_low_quality_starts"]).pack(anchor="w")
        self.tick_row(self.right, "trim_polyx", label["trim_polyx"], hover["trim_polyx"]).pack(anchor="w")

        self.fixed_var = tk.BooleanVar(self, value=self.state.trim.fixed_trim)
        cut = w.OptionRow(self.right, help_text=hover["fixed_trim"], help_line=self.help_line)
        self.fixed_tick = cut.add_tick(label["fixed_trim"], self.fixed_var,
                                       lambda: self.set_value("fixed_trim", bool(self.fixed_var.get())))
        flow, fhigh = settings_map.number_range("trim_front")
        self.fixed_parts: List[Any] = [cut.add_text(label["trim_front"])]
        self.numbers["trim_front"] = cut.add_number(value=self.state.trim.trim_front, minimum=flow, maximum=fhigh,
                                                    command=lambda v: self.set_value("trim_front", int(v)))
        self.fixed_parts.append(cut.add_text(label["trim_tail"]))
        self.numbers["trim_tail"] = cut.add_number(value=self.state.trim.trim_tail, minimum=flow, maximum=fhigh,
                                                   command=lambda v: self.set_value("trim_tail", int(v)))
        cut.finish().pack(anchor="w")
        self.rows["fixed_trim"] = self.rows["trim_front"] = self.rows["trim_tail"] = cut

        self.own_var = tk.BooleanVar(self, value=self.state.trim.own_adapters)
        own = w.OptionRow(self.right, help_text=hover["own_adapters"], help_line=self.help_line)
        self.own_tick = own.add_tick(label["own_adapters"], self.own_var,
                                     lambda: self.set_value("own_adapters", bool(self.own_var.get())))
        own.finish().pack(anchor="w")
        self.rows["own_adapters"] = own
        self.adapter_vars = {"adapter_r1": tk.StringVar(self, value=self.state.trim.adapter_r1),
                             "adapter_r2": tk.StringVar(self, value=self.state.trim.adapter_r2)}
        self.entries: Dict[str, ttk.Entry] = {}
        self.adapter_labels: List[Any] = []
        adapters = w.OptionRow(self.right, help_line=self.help_line, indent=26)
        for name in ("adapter_r1", "adapter_r2"):
            self.adapter_labels.append(adapters.add_text(label[name], gap=None if name == "adapter_r1" else 14))
            made = adapters.add_entry(self.adapter_vars[name], width=20)
            w.explain(made, hover[name], self.help_line)
            self.entries[name] = made
            self.adapter_vars[name].trace_add("write", lambda *_a, n=name: self._adapter_typed(n))
        adapters.pack(anchor="w")
        self.rows["adapter_r1"] = self.rows["adapter_r2"] = adapters

        self._lay_out_more(False)
        self.scroll.canvas.bind("<Configure>", self._on_width, add="+")

    def _on_width(self, event: Any) -> None:
        px = self.look.px
        room = int(event.width) - px(2 * 20 + 22 + 8)
        needed = self.left.winfo_reqwidth() + self.right.winfo_reqwidth() + px(48)
        self._lay_out_more(room >= needed)

    def _lay_out_more(self, two_columns: bool) -> None:
        """The numbers on the left and the rest beside them when the window is wide enough."""
        if two_columns == self._two_columns:
            return
        self._two_columns = two_columns
        px = self.look.px
        self.left.grid_forget()
        self.right.grid_forget()
        self.left.grid(row=0, column=0, sticky="nw")
        if two_columns:
            self.right.grid(row=0, column=1, sticky="nw", padx=(px(48), 0))
        else:
            self.right.grid(row=1, column=0, sticky="nw")

    def _adapter_typed(self, name: str) -> None:
        if self._loading:
            return
        setattr(self.state.trim, name, self.adapter_vars[name].get().strip())
        self._mark()
        self.state.notify("trim")

    # -- showing
    def show_settings(self, settings: TrimSettings) -> None:
        super().show_settings(settings)
        self.fixed_var.set(bool(settings.fixed_trim))
        self.own_var.set(bool(settings.own_adapters))
        for name, var in self.adapter_vars.items():
            var.set(getattr(settings, name))

    def gates(self) -> None:
        trim = self.state.trim
        on = self.page_enabled()
        if on:
            self.notice.dismiss("no_trimming")
        elif "no_trimming" not in self.notice.kinds():
            self.notice.show("no_trimming", texts.NO_TRIMMING_NOTICE, "info", dismissible=False)
        for row in self.ticks.values():
            row.set_enabled(on)
        self.enable(self.reset_button, on)                # "More options" itself still opens and closes
        quality = on and (trim.trim_low_quality_ends or trim.trim_low_quality_starts)
        self.rows["end_quality"].set_enabled(quality)
        self.rows["min_mean_quality"].set_enabled(on and trim.remove_poor_reads)
        self.rows["max_low_percent"].set_enabled(on and trim.remove_poor_reads)
        self.rows["min_length"].set_enabled(on and trim.remove_short_reads)
        self.enable(self.fixed_tick, on)
        for part in self.fixed_parts + [self.numbers["trim_front"], self.numbers["trim_tail"]]:
            self.enable(part, on and trim.fixed_trim)
        self.enable(self.own_tick, on and trim.remove_adapters)
        own = on and trim.own_adapters and trim.remove_adapters
        for part in self.adapter_labels + list(self.entries.values()):
            self.enable(part, own)
