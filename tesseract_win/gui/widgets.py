# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The building blocks of the window.

Each widget can be used on its own, is reachable with the keyboard, and takes every pixel
number through the theme's scale. None of them does any work of the program: they show
things and call back.

**Grounds.** A ttk widget paints its own background, so it has to know what it stands on:
the page ground, a white card, a coloured notice bar. Containers made here carry a
``ground`` attribute (:class:`Frame`, :class:`Card`, :class:`Well`, the bars of
:class:`NoticeBar`), and everything made here looks it up with :func:`ground_of` and picks
the matching style. Widgets made directly with ``ttk`` need ``style_for(parent, "TLabel")``,
or simply the factories :func:`label`, :func:`button`, :func:`tick`, :func:`radio`,
:func:`entry`.

**Hover text.** ``explain(widget, text, help_line)`` attaches both forms at once: a tooltip
after about half a second of hover, and the same words in the page's :class:`HelpLine`
while the control has keyboard focus (or the mouse over it).

**Words.** Every default word comes from ``texts``.

Hooks for the application (both optional):

- ``set_work_area_provider(func)``: ``func(x, y)`` returns the usable rectangle
  ``(left, top, right, bottom)`` of the monitor at that screen point, so that tooltips and
  dialogs stay on the right monitor of a multi-screen PC. Tk itself knows the main screen only.
- ``Dialog.created_hooks``: callables given every dialog window as it opens (dark title
  bar, icon), for example ``lambda window: osapi.set_dark_titlebar(window, look.dark)``.
"""

from __future__ import annotations

import math
import re
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from . import texts
from . import theme as _theme

__all__ = [
    # grounds and factories
    "ground_of", "style_for", "Frame", "Card", "Well", "Section", "PageHeader",
    "label", "button", "tick", "radio", "entry", "hairline", "WrapLabel", "LinkLabel",
    # hover text
    "Tooltip", "HelpLine", "explain", "hide_tooltips", "set_work_area_provider", "place_popup",
    # navigation
    "Stepper", "BottomBar",
    # tables
    "Column", "CheckTable", "shorten_middle", "shorten_to_width", "shorten_end", "status_kind",
    # options
    "Disclosure", "NumberField", "parse_number", "settle_number", "format_number",
    "OptionRow", "TickRow", "ChoiceRow", "NumberRow",
    # run page
    "NoticeBar", "StageChips", "WorkingIndicator", "LogPane",
    # questions
    "Dialog",
]

ELLIPSIS = "…"
TextSource = Union[str, Callable[[], str]]


# =========================================================================== grounds

def ground_of(widget: Any) -> str:
    """The ground ``widget`` stands on: the nearest ``ground`` attribute up the tree.

    Anything that is not inside a :class:`Card`, :class:`Well` or other container made
    here stands on the page ground.
    """
    current = widget
    while current is not None:
        ground = getattr(current, "ground", None)
        if isinstance(ground, str) and ground in _theme.GROUNDS:
            return ground
        current = getattr(current, "master", None)
    return "page"


def style_for(widget: Any, base: str) -> str:
    """The ttk style name for ``base`` on the ground of ``widget`` (usually the parent)."""
    return _theme.current(widget).style_name(base, ground_of(widget))


_LABEL_KINDS = {
    "body": "TLabel", "soft": "Soft.TLabel", "muted": "Muted.TLabel", "small": "Small.TLabel",
    "small_soft": "SmallSoft.TLabel", "bold": "Bold.TLabel", "heading": "Heading.TLabel",
    "title": "Title.TLabel", "mono": "Mono.TLabel", "accent": "Accent.TLabel", "ok": "Ok.TLabel",
    "warn": "Warn.TLabel", "bad": "Bad.TLabel", "small_accent": "SmallAccent.TLabel",
    "small_ok": "SmallOk.TLabel", "small_warn": "SmallWarn.TLabel", "small_bad": "SmallBad.TLabel",
}
_BUTTON_KINDS = {
    "plain": "TButton", "primary": "Primary.TButton", "danger": "Danger.TButton",
    "default": "Default.TButton", "link": "Link.TButton", "small_link": "Small.Link.TButton",
    "tool": "Tool.TButton",
}


def label(parent: Any, text: str = "", kind: str = "body", **options: Any) -> ttk.Label:
    """A label on the ground of ``parent``. ``kind``: body, soft, muted, small, small_soft,
    bold, heading, title, mono, accent, ok, warn, bad (and small_accent, small_ok, ...)."""
    options.setdefault("style", style_for(parent, _LABEL_KINDS[kind]))
    return ttk.Label(parent, text=text, **options)


def button(parent: Any, text: str = "", command: Optional[Callable[[], Any]] = None,
           kind: str = "plain", **options: Any) -> ttk.Button:
    """A button on the ground of ``parent``. ``kind``: plain, primary, danger, default
    (the default button of a dialog), link, small_link, tool (borderless).

    The program has exactly one primary button: Start, made by :class:`BottomBar`.
    """
    options.setdefault("style", style_for(parent, _BUTTON_KINDS[kind]))
    if kind in ("link", "small_link"):
        options.setdefault("cursor", "hand2")
    made = ttk.Button(parent, text=text, **options)
    if command is not None:
        made.configure(command=command)
    return made


def tick(parent: Any, text: str = "", variable: Optional[tk.Variable] = None,
         command: Optional[Callable[[], Any]] = None, **options: Any) -> ttk.Checkbutton:
    """A tick box on the ground of ``parent``."""
    options.setdefault("style", style_for(parent, "TCheckbutton"))
    made = ttk.Checkbutton(parent, text=text, **options)
    if variable is not None:
        made.configure(variable=variable)
    if command is not None:
        made.configure(command=command)
    return made


def radio(parent: Any, text: str, variable: tk.Variable, value: Any,
          command: Optional[Callable[[], Any]] = None, **options: Any) -> ttk.Radiobutton:
    """A radio button on the ground of ``parent``."""
    options.setdefault("style", style_for(parent, "TRadiobutton"))
    made = ttk.Radiobutton(parent, text=text, variable=variable, value=value, **options)
    if command is not None:
        made.configure(command=command)
    return made


def entry(parent: Any, **options: Any) -> ttk.Entry:
    """A one-line text field on the ground of ``parent``."""
    options.setdefault("style", style_for(parent, "TEntry"))
    return ttk.Entry(parent, **options)


def hairline(parent: Any, orient: str = "horizontal") -> ttk.Separator:
    """A one-hairline divider. Stretch it with ``fill="x"`` or ``sticky="ew"``."""
    return ttk.Separator(parent, orient=orient, style=style_for(parent, "TSeparator"))


def _pad(look: _theme.Theme, padding: Any) -> Any:
    """Scale a padding given for a 96 dpi screen: a number or a tuple of numbers."""
    if isinstance(padding, (tuple, list)):
        return tuple(look.px(v) for v in padding)
    return look.px(padding)


class Frame(ttk.Frame):
    """A plain container that knows its ground.

    ``ground`` defaults to the ground of the parent; ``Frame(root, ground="page")`` is the
    usual outermost container of a page. ``padding`` is given for a 96 dpi screen.
    """

    def __init__(self, parent: Any, ground: Optional[str] = None, padding: Any = 0, **options: Any):
        self.ground = ground or ground_of(parent)
        look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, padding=_pad(look, padding), **options)


class Card(ttk.Frame):
    """A white card with a hairline border and round corners. Put the content straight in."""

    def __init__(self, parent: Any, padding: Any = 16, **options: Any):
        look = _theme.current(parent)
        outside = ground_of(parent)
        prefix = _theme.GROUNDS[outside] or "OnSurface."
        options.setdefault("style", "Card.TFrame" if outside == "page" else prefix + "Card.TFrame")
        super().__init__(parent, padding=_pad(look, padding), **options)
        self.ground = "surface"


class Well(ttk.Frame):
    """A slightly sunk panel with a hairline border: the log, the strip for the selected row."""

    def __init__(self, parent: Any, padding: Any = 10, **options: Any):
        look = _theme.current(parent)
        options.setdefault("style", look.style_name("Well.TFrame", ground_of(parent)))
        super().__init__(parent, padding=_pad(look, padding), **options)
        self.ground = "sunk"


class WrapLabel(ttk.Label):
    """A label whose text wraps to the width it is given.

    It must be stretched by its geometry manager (``fill="x"`` or ``sticky="ew"``).
    ``wrap`` is the width assumed before it has been laid out, for a 96 dpi screen.
    """

    def __init__(self, parent: Any, text: str = "", kind: str = "body", *, wrap: int = 460,
                 **options: Any):
        look = _theme.current(parent)
        options.setdefault("style", style_for(parent, _LABEL_KINDS[kind]))
        options.setdefault("justify", "left")
        options.setdefault("anchor", "nw")
        self._wrap = look.px(wrap)
        super().__init__(parent, text=text, wraplength=self._wrap, **options)
        self.bind("<Configure>", self._follow_width, add="+")

    def _follow_width(self, event: Any) -> None:
        width = int(event.width)
        if width > 20 and abs(width - self._wrap) > 1:
            self._wrap = width
            self.configure(wraplength=width)


class LinkLabel(ttk.Button):
    """Words that act as a link: accent colour, underlined under the mouse, Tab reaches it,
    Space or Enter follows it."""

    def __init__(self, parent: Any, text: str = "", command: Optional[Callable[[], Any]] = None,
                 *, small: bool = False, **options: Any):
        options.setdefault("style", style_for(parent, "Small.Link.TButton" if small else "Link.TButton"))
        options.setdefault("cursor", "hand2")
        super().__init__(parent, text=text, **options)
        if command is not None:
            self.configure(command=command)
        self.bind("<Return>", lambda _e: self.invoke(), add="+")


class Section(ttk.Frame):
    """A titled group: a heading, an optional sentence, and ``body`` for the content."""

    def __init__(self, parent: Any, title: str = "", intro: str = "", **options: Any):
        self.ground = ground_of(parent)
        look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self.columnconfigure(0, weight=1)
        self.title = label(self, title, "heading")
        self.title.grid(row=0, column=0, sticky="w")
        self.intro: Optional[WrapLabel] = None
        if intro:
            self.intro = WrapLabel(self, intro, "soft")
            self.intro.grid(row=1, column=0, sticky="ew", pady=(look.px(2), 0))
        self.body = Frame(self)
        self.body.grid(row=2, column=0, sticky="nsew", pady=(look.px(8), 0))
        self.rowconfigure(2, weight=1)


class PageHeader(ttk.Frame):
    """The title of a page and its opening sentence."""

    def __init__(self, parent: Any, title: str, intro: str = "", **options: Any):
        self.ground = ground_of(parent)
        look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self.columnconfigure(0, weight=1)
        self.title = label(self, title, "title")
        self.title.grid(row=0, column=0, sticky="w")
        self.intro = WrapLabel(self, intro, "soft", wrap=640)
        if intro:
            self.intro.grid(row=1, column=0, sticky="ew", pady=(look.px(4), 0))

    def set(self, title: Optional[str] = None, intro: Optional[str] = None) -> None:
        if title is not None:
            self.title.configure(text=title)
        if intro is not None:
            self.intro.configure(text=intro)
            if intro:
                self.intro.grid(row=1, column=0, sticky="ew", pady=(_theme.current(self).px(4), 0))
            else:
                self.intro.grid_remove()


# =========================================================================== shared machinery

def _root_of(widget: Any) -> Any:
    return widget.nametowidget(".")


def _alive(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def _is_inside(widget: Any, ancestor: Any) -> bool:
    current = widget
    while current is not None:
        if current is ancestor:
            return True
        current = getattr(current, "master", None)
    return False


class _Animator:
    """One clock per window for everything that moves (the "working" rings)."""

    INTERVAL_MS = 90

    def __init__(self, root: Any):
        self.root = root
        self.frame = 0
        self._clients: Dict[Any, Callable[[int], None]] = {}
        self._job: Optional[str] = None

    def add(self, key: Any, callback: Callable[[int], None]) -> None:
        self._clients[key] = callback
        if self._job is None:
            self._job = self.root.after(self.INTERVAL_MS, self._tick)

    def remove(self, key: Any) -> None:
        self._clients.pop(key, None)

    def _tick(self) -> None:
        self._job = None
        if not self._clients:
            return
        self.frame += 1
        for key, callback in list(self._clients.items()):
            try:
                callback(self.frame)
            except tk.TclError:                 # the widget went away
                self._clients.pop(key, None)
        if self._clients:
            self._job = self.root.after(self.INTERVAL_MS, self._tick)


def _animator(widget: Any) -> _Animator:
    root = _root_of(widget)
    found = getattr(root, "_tesseract_win_animator", None)
    if found is None:
        found = _Animator(root)
        root._tesseract_win_animator = found
    return found


# =========================================================================== tooltips

_work_area_provider: Optional[Callable[[int, int], Optional[Tuple[int, int, int, int]]]] = None


def set_work_area_provider(provider: Optional[Callable[[int, int], Optional[Tuple[int, int, int, int]]]]) -> None:
    """Tell tooltips and dialogs how to find the monitor under a screen point (see the
    module text). ``None`` goes back to what Tk knows."""
    global _work_area_provider
    _work_area_provider = provider


def _work_area(widget: Any, x: int, y: int) -> Tuple[int, int, int, int]:
    """The rectangle a pop-up at screen point (x, y) must stay inside."""
    if _work_area_provider is not None:
        try:
            area = _work_area_provider(x, y)
            if area and area[2] > area[0] and area[3] > area[1]:
                return int(area[0]), int(area[1]), int(area[2]), int(area[3])
        except Exception:
            pass
    width, height = widget.winfo_screenwidth(), widget.winfo_screenheight()
    if 0 <= x < width and 0 <= y < height:
        return 0, 0, width, height
    # On another monitor Tk cannot tell its size: stay inside our own window, which is there.
    top = widget.winfo_toplevel()
    left, upper = top.winfo_rootx(), top.winfo_rooty()
    return left, upper, left + max(1, top.winfo_width()), upper + max(1, top.winfo_height())


def place_popup(widget: Any, width: int, height: int, x: int, y: int,
                pointer: Optional[Tuple[int, int]] = None, gap: int = 0) -> Tuple[int, int]:
    """Where to put a ``width`` x ``height`` pop-up wanted at (x, y) so it stays on screen.

    With ``pointer`` the pop-up never ends up under the mouse: if it does not fit below,
    it goes above. Pure arithmetic apart from the screen size.
    """
    left, top, right, bottom = _work_area(widget, pointer[0] if pointer else x, pointer[1] if pointer else y)
    if x + width > right:
        x = right - width
    if x < left:
        x = left
    if y + height > bottom:
        y = (pointer[1] - gap - height) if pointer else (bottom - height)
    if y < top:
        y = top
    return int(x), int(y)


class _TipManager:
    """The one tooltip window of a Tk root: at most one tip is pending or shown."""

    WATCH_MS = 200

    def __init__(self, root: Any):
        self.root = root
        self.owner: Any = None
        self.source: Optional[TextSource] = None
        self.window: Optional[tk.Toplevel] = None
        self.shown_text = ""
        self._pending: Optional[str] = None
        self._watch: Optional[str] = None

    # -- public
    def schedule(self, owner: Any, source: TextSource, delay_ms: int = 500) -> None:
        if self.owner is owner and self.source == source and (self._pending or self.window):
            return                              # already on its way or showing: no flicker
        self.cancel()
        self.owner, self.source = owner, source
        self._pending = self.root.after(max(0, delay_ms), self._show)
        self._start_watch()

    def cancel(self, owner: Any = None) -> None:
        """Hide the tip and forget what was pending (only if it belongs to ``owner``)."""
        if owner is not None and owner is not self.owner:
            return
        if self._pending is not None:
            try:
                self.root.after_cancel(self._pending)
            except tk.TclError:
                pass
            self._pending = None
        if self._watch is not None:
            try:
                self.root.after_cancel(self._watch)
            except tk.TclError:
                pass
            self._watch = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
        self.owner, self.source, self.shown_text = None, None, ""

    @property
    def visible(self) -> bool:
        return self.window is not None

    # -- internal
    def _pointer_on_owner(self) -> bool:
        owner = self.owner
        if owner is None or not _alive(owner):
            return False
        try:
            if not owner.winfo_viewable():
                return False
            x, y = owner.winfo_pointerxy()
            under = owner.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            return False
        return under is not None and _is_inside(under, owner)

    def _start_watch(self) -> None:
        if self._watch is None:
            self._watch = self.root.after(self.WATCH_MS, self._check)

    def _check(self) -> None:
        # A page change, a minimised window or another program in front gives no <Leave>:
        # look for ourselves whether the mouse is still on the control.
        self._watch = None
        if self.owner is None:
            return
        if not self._pointer_on_owner():
            self.cancel()
            return
        self._watch = self.root.after(self.WATCH_MS, self._check)

    def _show(self) -> None:
        self._pending = None
        owner, source = self.owner, self.source
        if owner is None or not self._pointer_on_owner():
            self.cancel()
            return
        try:
            text = source() if callable(source) else source
        except Exception:
            text = ""
        if not text:
            self.cancel()
            return
        look = _theme.current(owner)
        p = look.palette
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        for attribute, value in (("-topmost", True), ("-type", "tooltip")):
            try:
                window.attributes(attribute, value)
            except tk.TclError:
                pass
        fill = p.surface_alt if look.dark else p.surface
        window.configure(background=p.border_strong)
        body = tk.Label(window, text=text, justify="left", anchor="w", wraplength=look.px(340),
                        background=fill, foreground=p.text, font=look.font("small"),
                        padx=look.px(10), pady=look.px(7), borderwidth=0)
        body.pack(padx=look.hair, pady=look.hair)
        window.update_idletasks()
        pointer_x, pointer_y = owner.winfo_pointerxy()
        gap = look.px(12)
        x, y = place_popup(owner, window.winfo_reqwidth(), window.winfo_reqheight(),
                           pointer_x + look.px(14), pointer_y + look.px(20),
                           pointer=(pointer_x, pointer_y), gap=gap)
        window.geometry("+%d+%d" % (x, y))
        window.deiconify()
        try:
            window.lift()
        except tk.TclError:
            pass
        self.window = window
        self.shown_text = text
        self._start_watch()


def _tips(widget: Any) -> _TipManager:
    root = _root_of(widget)
    found = getattr(root, "_tesseract_win_tips", None)
    if found is None:
        found = _TipManager(root)
        root._tesseract_win_tips = found
    return found


def hide_tooltips(widget: Any) -> None:
    """Take away the tooltip of this window, shown or pending. Call it on a page change."""
    _tips(widget).cancel()


class Tooltip:
    """Hover text for one widget (and everything inside it).

    Appears about half a second after the mouse comes to rest on the widget, just below
    and to the right of the pointer, wrapped, inside the screen; never under the pointer,
    so it cannot flicker. Goes away when the mouse leaves, on a click, on a key press, when
    the widget loses focus or is hidden, and when the page changes. It does not time out:
    the texts are two sentences long.

    ``text`` may be a function that returns the text when the tip is about to show.
    """

    def __init__(self, widget: Any, text: TextSource, *, delay_ms: int = 500):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.enabled = True
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")
        widget.bind("<KeyPress>", self._leave, add="+")
        widget.bind("<FocusOut>", self._leave, add="+")
        widget.bind("<Unmap>", self._leave, add="+")
        widget.bind("<Destroy>", self._leave, add="+")

    def _enter(self, _event: Any = None) -> None:
        if self.enabled and self.text:
            _tips(self.widget).schedule(self.widget, self.text, self.delay_ms)

    def _leave(self, _event: Any = None) -> None:
        try:
            _tips(self.widget).cancel(self.widget)
        except Exception:                       # the window may be on its way out
            pass

    def show_now(self) -> None:
        """Show at once (for tests and for keyboard help)."""
        if self.text:
            manager = _tips(self.widget)
            manager.cancel()
            manager.schedule(self.widget, self.text, 0)

    def hide(self) -> None:
        self._leave()

    @property
    def visible(self) -> bool:
        manager = _tips(self.widget)
        return manager.visible and manager.owner is self.widget


# =========================================================================== the help line

class HelpLine(ttk.Frame):
    """The line at the bottom of a page that explains the control in use.

    Three things can speak. A message (:meth:`say`, for example a number field that was
    set back) outranks the others while it lasts. Of the control under the mouse and the
    control with keyboard focus, the one that was reached last speaks, so a mouse left
    resting somewhere does not silence the keyboard. With none of them the line shows
    the default text. Its height is fixed (``lines`` lines) so the page never jumps.
    Stretch it horizontally.
    """

    def __init__(self, parent: Any, *, default: str = "", lines: int = 2, **options: Any):
        self.ground = ground_of(parent)
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self._default = default
        self._focus: Tuple[Any, str] = (None, "")
        self._hover: Tuple[Any, str] = (None, "")
        self._message: Tuple[str, str] = ("", "")
        self._message_job: Optional[str] = None
        self._latest = "focus"
        self._lines = max(1, lines)
        self.kind = "info"
        height = lines * int(look.font("body").metrics("linespace")) + look.px(4)
        self.configure(height=height)
        self.grid_propagate(False)
        self.columnconfigure(1, weight=1)
        self._icon = ttk.Label(self, style=look.style_name("TLabel", self.ground))
        self._icon.grid(row=0, column=0, sticky="n", padx=(0, look.px(8)), pady=(look.px(2), 0))
        self._label = ttk.Label(self, style=look.style_name("Soft.TLabel", self.ground),
                                justify="left", anchor="nw", wraplength=look.px(600))
        self._label.grid(row=0, column=1, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self._label.bind("<Configure>", self._rewrap, add="+")
        self.bind("<Destroy>", self._forget_timer, add="+")
        self._refresh()

    # -- what speaks
    def set_default(self, text: str) -> None:
        self._default = text
        self._refresh()

    def focus_text(self, text: str, owner: Any = None) -> None:
        """Shown while ``owner`` has keyboard focus."""
        self._focus = (owner, text)
        self._latest = "focus"
        self._refresh()

    def clear_focus(self, owner: Any = None) -> None:
        if owner is None or self._focus[0] is owner:
            self._focus = (None, "")
            self._refresh()

    def hover_text(self, text: str, owner: Any = None) -> None:
        """Shown while the mouse is over ``owner``."""
        self._hover = (owner, text)
        self._latest = "hover"
        self._refresh()

    def clear_hover(self, owner: Any = None) -> None:
        if owner is None or self._hover[0] is owner:
            self._hover = (None, "")
            self._refresh()

    def say(self, text: str, kind: str = "warn", seconds: float = 8.0) -> None:
        """A message that outranks hover and focus texts for ``seconds``."""
        self._forget_timer()
        self._message = (text, kind)
        if text and seconds > 0:
            self._message_job = self.after(int(seconds * 1000), self._end_message)
        self._refresh()

    def clear(self) -> None:
        """Forget message, hover and focus texts (on a page change)."""
        self._forget_timer()
        self._message, self._hover, self._focus = ("", ""), (None, ""), (None, "")
        self._refresh()

    @property
    def text(self) -> str:
        """What the line shows now."""
        return str(self._label.cget("text"))

    # -- internal
    def _end_message(self) -> None:
        self._message_job = None
        self._message = ("", "")
        self._refresh()

    def _forget_timer(self, _event: Any = None) -> None:
        if self._message_job is not None:
            try:
                self.after_cancel(self._message_job)
            except tk.TclError:
                pass
            self._message_job = None

    def _rewrap(self, event: Any) -> None:
        if event.width > 20 and int(event.width) != int(self._label.cget("wraplength")):
            self._label.configure(wraplength=int(event.width))
            self._refresh()

    def _refresh(self) -> None:
        look = self._look
        if self._message[0]:
            text, kind = self._message
        else:
            first, second = (self._hover[1], self._focus[1]) if self._latest == "hover" else (
                self._focus[1], self._hover[1])
            text, kind = (first or second or self._default), "info"
        self.kind = kind
        base = {"warn": "Warn.TLabel", "bad": "Bad.TLabel", "ok": "Ok.TLabel"}.get(kind, "Soft.TLabel")
        try:
            room = self._lines * max(1, int(self._label.cget("wraplength")))
            if look.text_width(text, "body") > room * 0.92:
                base = "Small" + base if base != "Soft.TLabel" else "SmallSoft.TLabel"   # long: smaller type
            self._label.configure(text=text, style=look.style_name(base, self.ground))
            if not text:
                self._icon.configure(image=look.icons.blank(look.px(16), look.px(16)))
            elif kind == "info":
                self._icon.configure(image=look.icons.glyph("info", "muted", 16))
            else:
                self._icon.configure(image=look.icons.glyph(kind, kind, 16))
        except tk.TclError:
            pass


def _descendants(widget: Any) -> List[Any]:
    found = [widget]
    for child in widget.winfo_children():
        found.extend(_descendants(child))
    return found


def explain(widget: Any, text: str, help_line: Optional[HelpLine] = None, *,
            delay_ms: int = 500) -> Tooltip:
    """Give ``widget`` its hover text, in both forms.

    A tooltip after about half a second of hover, and the same text in ``help_line``
    while the control (or anything inside it) has keyboard focus or the mouse over it.
    A composite such as :class:`TickRow` is covered as a whole. Returns the tooltip;
    assign to its ``text`` to change the words later (the help line follows).
    """
    target = getattr(widget, "explain_target", widget)
    tip = Tooltip(target, text, delay_ms=delay_ms)
    if help_line is None:
        return tip

    def current() -> str:
        value = tip.text() if callable(tip.text) else tip.text
        return value or ""

    def focus_in(_event: Any) -> None:
        help_line.focus_text(current(), owner=target)

    def focus_out(_event: Any) -> None:
        help_line.clear_focus(owner=target)

    def hover_in(_event: Any) -> None:
        help_line.hover_text(current(), owner=target)

    def hover_out(_event: Any) -> None:
        help_line.clear_hover(owner=target)

    for part in _descendants(target):
        part.bind("<FocusIn>", focus_in, add="+")
        part.bind("<FocusOut>", focus_out, add="+")
    target.bind("<Enter>", hover_in, add="+")
    target.bind("<Leave>", hover_out, add="+")
    target.bind("<Unmap>", lambda _e: (help_line.clear_hover(owner=target), help_line.clear_focus(owner=target)), add="+")
    return tip


# =========================================================================== the stepper

class Stepper(ttk.Frame):
    """The steps across the top of the window.

    Each step shows its number, its name and a small line under the name ("40 ready",
    "standard", "changed"). The current step is marked. A step that may be visited is a
    link: click it, or Tab to it and press Enter or Space. During a run the steps are
    locked. Stretch it horizontally.

    ``command(key)`` is called when the user picks another step; the stepper does not
    change the current step by itself, the caller does with :meth:`set_current`.
    """

    def __init__(self, parent: Any, steps: Sequence[Tuple[str, str]] = texts.STEPS, *,
                 command: Optional[Callable[[str], Any]] = None, current: Optional[str] = None,
                 help_line: Optional[HelpLine] = None, **options: Any):
        self.ground = ground_of(parent)
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self.command = command
        self.step_keys = [key for key, _name in steps]
        self.current = current if current in self.step_keys else self.step_keys[0]
        self.locked = False
        self._enabled = {key: True for key in self.step_keys}
        self._sub = {key: ("", "muted") for key in self.step_keys}
        self._hover: Optional[str] = None
        self._parts: Dict[str, Dict[str, Any]] = {}
        back = look.ground_colour(self.ground)
        px = look.px
        column = 0
        for index, (key, name) in enumerate(steps):
            if index:
                self.columnconfigure(column, weight=1, minsize=px(24))
                line = tk.Frame(self, background=look.palette.border, height=look.hair, width=px(24),
                                highlightthickness=0, borderwidth=0)
                line.grid(row=0, column=column, sticky="ew", padx=px(6))
                column += 1
            cell = tk.Frame(self, background=back, highlightthickness=px(2), highlightbackground=back,
                            highlightcolor=look.palette.focus, borderwidth=0)
            cell.grid(row=0, column=column, sticky="w")
            column += 1
            disc = tk.Label(cell, text=str(index + 1), compound="center", font=look.font("bold"),
                            background=back, borderwidth=0, padx=0, pady=0)
            disc.grid(row=0, column=0, rowspan=2, padx=(px(4), px(9)), pady=px(4))
            title = tk.Label(cell, text=name, font=look.font("heading"), background=back, anchor="w",
                             borderwidth=0, padx=0, pady=0)
            title.grid(row=0, column=1, sticky="sw", padx=(0, px(8)))
            sub = tk.Label(cell, text=" ", font=look.font("small"), background=back, anchor="w",
                           borderwidth=0, padx=0, pady=0)
            sub.grid(row=1, column=1, sticky="nw", padx=(0, px(8)))
            self._parts[key] = {"cell": cell, "disc": disc, "title": title, "sub": sub}
            for part in (cell, disc, title, sub):
                part.bind("<Button-1>", lambda _e, k=key: self._pick(k), add="+")
            # the cell hears the mouse come and go for everything inside it
            cell.bind("<Enter>", lambda _e, k=key: self._set_hover(k), add="+")
            cell.bind("<Leave>", lambda _e, k=key: self._set_hover(None), add="+")
            cell.bind("<Return>", lambda _e, k=key: self._pick(k))
            cell.bind("<KP_Enter>", lambda _e, k=key: self._pick(k))
            cell.bind("<space>", lambda _e, k=key: self._pick(k))
            cell.bind("<FocusIn>", lambda _e: self._paint(), add="+")
            cell.bind("<FocusOut>", lambda _e: self._paint(), add="+")
            explain(cell, lambda k=key: self._hover_text(k), help_line)
        self._paint()

    def _hover_text(self, key: str) -> str:
        if self.locked:
            return texts.HOVER["step_locked"]
        return texts.HOVER["step"] if self.can_pick(key) else ""

    # -- state
    def set_current(self, key: str) -> None:
        if key not in self._parts:
            raise KeyError(key)
        self.current = key
        self._paint()

    def set_sub(self, key: str, text: str, kind: str = "muted") -> None:
        """The small line under a step. ``kind``: muted, accent, ok, warn, bad."""
        self._sub[key] = (text, kind)
        self._paint()

    def sub(self, key: str) -> str:
        return self._sub[key][0]

    def set_enabled(self, key: str, enabled: bool = True) -> None:
        """Whether a step may be visited at all."""
        self._enabled[key] = bool(enabled)
        self._paint()

    def lock(self, locked: bool = True) -> None:
        """During a run: no step can be picked."""
        self.locked = bool(locked)
        self._paint()

    def can_pick(self, key: str) -> bool:
        return (not self.locked) and self._enabled.get(key, False) and key != self.current

    def pick(self, key: str) -> bool:
        """What a click does. True if the command was called."""
        return self._pick(key)

    # -- internal
    def _pick(self, key: str) -> bool:
        if not self.can_pick(key):
            return False
        hide_tooltips(self)
        if self.command is not None:
            self.command(key)
        return True

    def _set_hover(self, key: Optional[str]) -> None:
        if key != self._hover:
            self._hover = key
            self._paint()

    def _paint(self) -> None:
        look = self._look
        p = look.palette
        icons = look.icons
        for key, parts in self._parts.items():
            is_current = key == self.current
            pickable = self.can_pick(key)
            hot = pickable and self._hover == key
            if is_current:
                disc, number, name = icons.disc("accent"), p.on_accent, p.text
            elif pickable:
                disc = icons.disc("accent_soft" if hot else "surface", "accent" if hot else "border_strong")
                number = p.accent if hot else p.text_soft
                name = p.accent if hot else p.text_soft
            else:
                disc, number, name = icons.disc("surface_sunk", "border"), look.disabled_fg, look.disabled_fg
            text, kind = self._sub[key]
            sub_colour = {"accent": p.accent, "ok": p.ok, "warn": p.warn, "bad": p.bad}.get(kind, p.muted)
            if not is_current and not pickable:
                sub_colour = look.disabled_fg
            try:
                parts["disc"].configure(image=disc, foreground=number)
                parts["title"].configure(foreground=name)
                parts["sub"].configure(text=text or " ", foreground=sub_colour)
                cursor = "hand2" if pickable else ""
                for part in parts.values():
                    part.configure(cursor=cursor)
                parts["cell"].configure(takefocus=1 if pickable else 0)
            except tk.TclError:
                pass


# =========================================================================== the bottom bar

class BottomBar(ttk.Frame):
    """Back and Next on the left, the one Start button on the right.

    Start is the only primary-coloured button of the program. It is never the window's
    Enter key: it is pressed with the mouse, with Space when it has focus, or with Alt+S.
    When it is disabled the reason stands beside it; otherwise that place can hold a note
    with a link ("2 left out: why?") or the first-time hint. Stretch it horizontally.
    """

    def __init__(self, parent: Any, *, on_back: Optional[Callable[[], Any]] = None,
                 on_next: Optional[Callable[[], Any]] = None,
                 on_start: Optional[Callable[[], Any]] = None,
                 help_line: Optional[HelpLine] = None, **options: Any):
        self.ground = ground_of(parent)
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        px, icons = look.px, look.icons
        self.back = button(self, texts.BUTTON["back"], on_back, compound="left",
                           image=(icons.chevron("left", "text_soft"), "disabled", icons.chevron("left", "border_strong")))
        self.next = button(self, "", on_next, compound="right",
                           image=(icons.chevron("right", "text_soft"), "disabled", icons.chevron("right", "border_strong")))
        self.start = button(self, texts.BUTTON["start"], on_start, kind="primary")
        self.message = WrapLabel(self, "", "soft", wrap=380, justify="right", anchor="e")
        self.link = LinkLabel(self, "")
        self.back.grid(row=0, column=0, padx=(0, px(8)))
        self.next.grid(row=0, column=1)
        self.columnconfigure(2, weight=1)
        self.message.grid(row=0, column=2, sticky="ew", padx=(px(16), px(4)))
        self.link.grid(row=0, column=3, padx=(0, px(4)))
        self.start.grid(row=0, column=4, padx=(px(10), 0))
        self.link.grid_remove()
        self._reason = ""
        self._note: Tuple[str, str, Optional[Callable[[], Any]], str] = ("", "", None, "soft")
        self._hint = ""
        self._start_enabled = True
        explain(self.back, texts.HOVER["back"], help_line)
        explain(self.next, texts.HOVER["next"], help_line)
        explain(self.start, texts.HOVER["start"], help_line)
        self.set_next(None)
        self._refresh_message()

    # -- buttons
    def set_back(self, enabled: bool = True, visible: bool = True) -> None:
        self._show(self.back, visible)
        self.back.state(["!disabled"] if enabled else ["disabled"])

    def set_next(self, step_key: Optional[str], enabled: bool = True) -> None:
        """Name the step Next leads to ("Next: Trimming"); ``None`` takes Next away."""
        if step_key is None:
            self._show(self.next, False)
            return
        self.next.configure(text=texts.next_label(step_key))
        self._show(self.next, True)
        self.next.state(["!disabled"] if enabled else ["disabled"])

    def set_start(self, ready: Optional[int] = None, *, enabled: bool = True, reason: str = "",
                  text: Optional[str] = None, visible: bool = True) -> None:
        """The Start button: its count, whether it can be pressed and, if not, why."""
        if text is None and ready is not None:
            text = texts.start_label(ready)
        if text is not None:
            self.start.configure(text=text)
        self._show(self.start, visible)
        self._start_enabled = bool(enabled)
        self.start.state(["!disabled"] if enabled else ["disabled"])
        self._reason = "" if enabled else reason
        self._refresh_message()

    def set_note(self, text: str = "", *, link_text: str = "",
                 link_command: Optional[Callable[[], Any]] = None, kind: str = "soft") -> None:
        """Words beside an enabled Start, with an optional link after them."""
        self._note = (text, link_text, link_command, kind)
        self._refresh_message()

    def set_hint(self, text: str = "") -> None:
        """The first-time hint; shown only while there is no reason and no note."""
        self._hint = text
        self._refresh_message()

    def bind_keys(self, window: Optional[Any] = None) -> None:
        """Alt+S presses Start, Alt+B Back, Alt+N Next, anywhere in ``window``."""
        target = window or self.winfo_toplevel()
        for keys, control in (("s", self.start), ("b", self.back), ("n", self.next)):
            for key in (keys, keys.upper()):
                target.bind("<Alt-%s>" % key, lambda _e, c=control: self._press(c), add="+")

    @property
    def message_text(self) -> str:
        return str(self.message.cget("text"))

    # -- internal
    def _press(self, control: ttk.Button) -> str:
        if _alive(control) and control.winfo_ismapped() and control.instate(["!disabled"]):
            control.invoke()
        return "break"

    @staticmethod
    def _show(widget: Any, visible: bool) -> None:
        if visible:
            widget.grid()
        else:
            widget.grid_remove()

    def _refresh_message(self) -> None:
        look = self._look
        link_text: str = ""
        link_command: Optional[Callable[[], Any]] = None
        if self._reason:
            text, kind = self._reason, "soft"
        elif self._note[0] or self._note[1]:
            text, link_text, link_command, kind = self._note
        else:
            text, kind = self._hint, "muted"
        self.message.configure(text=text, style=look.style_name(_LABEL_KINDS.get(kind, "Soft.TLabel"), self.ground))
        if link_text:
            self.link.configure(text=link_text, command=link_command or (lambda: None))
            self.link.grid()
        else:
            self.link.grid_remove()


# =========================================================================== text that fits

def shorten_to_width(text: str, max_width: float, char_width: Callable[[str], float],
                     ellipsis: str = ELLIPSIS) -> str:
    """Shorten ``text`` in the middle so that it fits ``max_width``.

    ``char_width(character)`` gives the width of one character in whatever unit
    ``max_width`` is in. The start and the end of the text are kept (the end gets the odd
    character: a file name tells most at its end) with one ellipsis between them. Text that
    fits comes back unchanged.
    """
    widths = [char_width(ch) for ch in text]
    if sum(widths) <= max_width:
        return text
    budget = max_width - sum(char_width(ch) for ch in ellipsis)
    count = len(text)
    head = tail = 0
    used = 0.0
    while head + tail < count:
        width = widths[count - 1 - tail]
        if used + width > budget:
            break
        used += width
        tail += 1
        if head + tail >= count:
            break
        width = widths[head]
        if used + width > budget:
            break
        used += width
        head += 1
    return text[:head] + ellipsis + (text[count - tail:] if tail else "")


def shorten_end(text: str, max_width: float, char_width: Callable[[str], float],
                ellipsis: str = ELLIPSIS) -> str:
    """Cut ``text`` at the end so that it fits ``max_width``, with an ellipsis to say so."""
    widths = [char_width(ch) for ch in text]
    if sum(widths) <= max_width:
        return text
    budget = max_width - sum(char_width(ch) for ch in ellipsis)
    used = 0.0
    keep = 0
    for width in widths:
        if used + width > budget:
            break
        used += width
        keep += 1
    return text[:keep].rstrip() + ellipsis


def shorten_middle(text: str, max_chars: int, ellipsis: str = ELLIPSIS) -> str:
    """Shorten ``text`` in the middle to at most ``max_chars`` characters.

    ``shorten_middle("KP_002_S2_L001_R1_001.fastq.gz", 20)`` gives
    ``"KP_002_S2…001.fastq.gz"``-like output: start, one ellipsis, end.
    """
    return shorten_to_width(text, max_chars, lambda _ch: 1.0, ellipsis)


class _CharWidths:
    """Width of text as the sum of its characters, each measured once.

    Asking Tk to measure every cell of a 500-row table takes too long; adding up cached
    character widths is close enough (kerning aside) and fast.
    """

    def __init__(self, font: Any):
        self.font = font
        self._known: Dict[str, int] = {}

    def char(self, character: str) -> int:
        width = self._known.get(character)
        if width is None:
            width = int(self.font.measure(character))
            self._known[character] = width
        return width

    def text(self, text: str) -> int:
        return sum(self.char(ch) for ch in text)


# =========================================================================== the table

#: Colour kind of the status words, by value of ``model.SampleState`` / ``model.RunStatus``.
_STATUS_KINDS: Dict[str, str] = {
    "ready": "ok", "ready_note": "warn", "left_out": "muted",
    "waiting": "muted", "joining_lanes": "accent", "trimming": "accent", "checking_reads": "accent",
    "assembling": "accent", "summarising": "accent", "done": "ok", "done_notes": "warn",
    "failed": "bad", "skipped": "muted", "stopped": "warn", "not_started": "muted",
}


def status_kind(state: Any) -> str:
    """The colour kind for a ``SampleState`` or ``RunStatus`` (or its value): ok, warn, bad,
    accent or muted. Pass it as ``status`` to a :class:`CheckTable` row."""
    value = getattr(state, "value", state)
    return _STATUS_KINDS.get(str(value), "muted")


@dataclass
class Column:
    """One column of a :class:`CheckTable`. Widths are for a 96 dpi screen."""
    key: str
    title: str
    width: int = 140
    min_width: int = 60
    stretch: bool = False
    anchor: str = "w"            # "w", "e" or "center"
    #: what happens to text that does not fit: "end" cuts it with an ellipsis, "middle" keeps
    #: both ends (file names), "" leaves it to the table. The tooltip has the text in full.
    shorten: str = "end"
    editable: bool = False       # double-click or F2 edits the cell in place
    status: bool = False         # coloured words with a dot; the colour is the row's ``status``
    hover: str = ""              # hover text of the heading


class _Row:
    __slots__ = ("values", "checked", "status", "tips", "dim")

    def __init__(self, values: Mapping[str, Any], checked: Optional[bool], status: str,
                 tips: Optional[Mapping[str, str]], dim: bool):
        self.values: Dict[str, str] = {k: "" if v is None else str(v) for k, v in values.items()}
        self.checked = checked
        self.status = status
        self.tips: Dict[str, str] = dict(tips or {})
        self.dim = dim


_KEEP: Any = object()


class CheckTable(ttk.Frame):
    """A table with a real tick-box column, built on ``ttk.Treeview``.

    - **Tick column** (``checks=True``): a box drawn at the screen's scale, ticked,
      unticked or "not available" (a row whose ``checked`` is ``None``). A click on a box
      toggles that row; Space toggles the selected rows; a click on the heading of the
      tick column toggles all.
    - **Status column** (``Column(status=True)``): the words in the colour of the row's
      ``status`` kind (ok, warn, bad, accent, muted) with a dot before them.
    - **Long text**: cut with an ellipsis; with ``Column(shorten="middle")`` shortened in
      the middle, which suits file names. The full text is the tooltip. Any cell may also
      carry its own tooltip through ``tips``.
    - **Rename in place** (``Column(editable=True)``): double-click or F2; Enter accepts,
      Esc cancels, leaving the field accepts a valid name.
    - Delete removes the selected rows; Ctrl+A selects all; every second row is tinted.

    Rows are known by an id of the caller's choosing: any text that is not empty, and not
    the sample name, which can be edited. Callbacks, all optional:

    - ``on_toggle(ids, checked)``     ticks changed by the user
    - ``on_remove(ids)``              rows removed by the user (already gone from the table)
    - ``on_rename(row_id, old, new)`` a cell was edited (already changed in the table)
    - ``on_select(ids)``              the selection changed
    - ``on_activate(row_id)``         double-click or Enter on a row
    - ``validate_rename(row_id, text)`` returns the reason a name is not acceptable, or ""
    """

    def __init__(self, parent: Any, columns: Sequence[Column], *, checks: bool = True,
                 check_title: str = "", check_hover: str = "", height: int = 8,
                 on_toggle: Optional[Callable[[List[str], bool], Any]] = None,
                 on_remove: Optional[Callable[[List[str]], Any]] = None,
                 on_rename: Optional[Callable[[str, str, str], Any]] = None,
                 on_select: Optional[Callable[[List[str]], Any]] = None,
                 on_activate: Optional[Callable[[str], Any]] = None,
                 validate_rename: Optional[Callable[[str, str], str]] = None,
                 help_line: Optional[HelpLine] = None, removable: bool = True, **options: Any):
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("Box.TFrame", ground_of(parent)))
        super().__init__(parent, padding=look.hair, **options)
        self.ground = "surface"
        self.columns = list(columns)
        self.checks = checks
        self.removable = removable
        self.on_toggle, self.on_remove, self.on_rename = on_toggle, on_remove, on_rename
        self.on_select, self.on_activate = on_select, on_activate
        self.validate_rename = validate_rename
        self.help_line = help_line
        self.check_hover = check_hover
        self.enabled = True
        self._by_key = {column.key: column for column in self.columns}
        self._keys = [column.key for column in self.columns]
        self._rows: Dict[str, _Row] = {}
        self._order: List[str] = []
        self._shown: Dict[Tuple[str, str], str] = {}
        self._widths: Dict[str, int] = {}
        self._chars = _CharWidths(look.font("body"))
        self._status_labels: List[tk.Label] = []
        self._editor: Optional[ttk.Entry] = None
        self._editing: Optional[Tuple[str, str]] = None
        self._tip_cell: Optional[Tuple[str, str]] = None
        self._tip_text = ""
        self._jobs: Dict[str, Optional[str]] = {"layout": None, "status": None}
        self._pending_see: Optional[str] = None
        self._empty: Optional[ttk.Frame] = None
        self._empty_spec: Tuple[str, str, Optional[Callable[[], Any]], str] = ("", "", None, "")

        px = look.px
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(self, columns=self._keys, selectmode="extended", height=height,
                                 show="tree headings" if checks else "headings")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.scrollbar.grid_remove()
        self.tree.configure(yscrollcommand=self._on_scroll)
        if checks:
            width = look.icons.box_size + px(24)
            self.tree.column("#0", width=width, minwidth=width, stretch=False, anchor="w")
            self.tree.heading("#0", text=check_title, anchor="w", command=self.toggle_all)
        for column in self.columns:
            self.tree.heading(column.key, text=column.title, anchor=column.anchor)
            self.tree.column(column.key, width=px(column.width), minwidth=px(column.min_width),
                             stretch=column.stretch, anchor=column.anchor)
        self.tree.tag_configure("odd", background=look.table_alt)
        self.tree.tag_configure("dim", foreground=look.palette.muted)
        self._status_columns = [column.key for column in self.columns if column.status]

        bind = self.tree.bind
        bind("<Button-1>", self._on_click)
        bind("<Double-Button-1>", self._on_double_click)
        bind("<space>", self._on_space)
        bind("<Delete>", self._on_delete)
        bind("<F2>", lambda _e: self.begin_rename() and "break")
        bind("<Return>", self._on_return)
        bind("<KP_Enter>", self._on_return)
        bind("<Control-a>", self._select_all)
        bind("<Control-A>", self._select_all)
        bind("<<TreeviewSelect>>", self._on_select, add="+")
        bind("<Motion>", self._on_motion, add="+")
        bind("<Leave>", self._on_leave, add="+")
        bind("<ButtonPress>", lambda _e: self._drop_tip(), add="+")
        bind("<Configure>", lambda _e: self._later("layout"), add="+")
        bind("<B1-Motion>", lambda _e: self._later("layout"), add="+")
        bind("<ButtonRelease-1>", lambda _e: self._later("layout"), add="+")
        bind("<FocusIn>", lambda _e: self.state(["focus"]), add="+")
        bind("<FocusOut>", lambda _e: self.state(["!focus"]), add="+")
        bind("<Destroy>", self._on_destroy, add="+")

    # ------------------------------------------------------------------ rows
    def set_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Replace everything. Each row is a mapping with ``id`` and ``values`` (column key
        to text) and optionally ``checked`` (True, False, or None for "cannot be ticked"),
        ``status`` (ok, warn, bad, accent, muted), ``tips`` (column key to tooltip text)
        and ``dim`` (grey text, for a row that will not run). The selection is kept where
        the ids still exist."""
        selected = set(self.tree.selection())
        self._end_rename(False)
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        self._order = []
        self._shown.clear()
        for row in rows:
            row_id = str(row["id"])
            self._rows[row_id] = _Row(row.get("values", {}), row.get("checked", True), row.get("status", ""),
                                      row.get("tips"), bool(row.get("dim", False)))
            self._order.append(row_id)
        self._measure_columns()
        for index, row_id in enumerate(self._order):
            self._insert(row_id, index, "end")
        keep = [row_id for row_id in self._order if row_id in selected]
        if keep:
            self.tree.selection_set(keep)
        self._changed()

    def add_row(self, row_id: str, values: Mapping[str, Any], *, checked: Optional[bool] = True,
                status: str = "", tips: Optional[Mapping[str, str]] = None, dim: bool = False,
                index: Optional[int] = None) -> None:
        row_id = str(row_id)
        if row_id in self._rows:
            raise ValueError("row %r is already in the table" % row_id)
        self._rows[row_id] = _Row(values, checked, status, tips, dim)
        position = len(self._order) if index is None else max(0, min(index, len(self._order)))
        self._order.insert(position, row_id)
        if not self._widths:
            self._measure_columns()
        self._insert(row_id, position, position)
        if position != len(self._order) - 1:
            self._restripe()
        self._changed()

    def update_row(self, row_id: str, values: Optional[Mapping[str, Any]] = None, *,
                   checked: Any = _KEEP, status: Optional[str] = None,
                   tips: Optional[Mapping[str, str]] = None, dim: Optional[bool] = None) -> None:
        """Change parts of a row; what is not given stays."""
        row = self._rows[row_id]
        if values:
            for key, value in values.items():
                row.values[key] = "" if value is None else str(value)
                if key in self._by_key:
                    self.tree.set(row_id, key, self._display(row_id, key))
        if checked is not _KEEP:
            row.checked = checked
            if self.checks:
                self.tree.item(row_id, image=self._tick_image(row))
        if status is not None:
            row.status = status
        if tips is not None:
            row.tips.update(tips)
        if dim is not None and dim != row.dim:
            row.dim = dim
            self.tree.item(row_id, tags=self._tags(row_id))
        self._later("status")

    def remove_rows(self, row_ids: Iterable[str], notify: bool = False) -> List[str]:
        gone = [row_id for row_id in row_ids if row_id in self._rows]
        if not gone:
            return []
        self._end_rename(False)
        first = min(self._order.index(row_id) for row_id in gone)
        had_selection = bool(set(self.tree.selection()) & set(gone)) or self.tree.focus() in gone
        self.tree.delete(*gone)
        dropped = set(gone)
        for row_id in gone:
            del self._rows[row_id]
            for key in self._keys:
                self._shown.pop((row_id, key), None)
        self._order = [row_id for row_id in self._order if row_id not in dropped]
        self._restripe()
        if self._order and had_selection:     # keep the keyboard on the list
            follow = self._order[min(first, len(self._order) - 1)]
            self.tree.focus(follow)
            self.tree.selection_set(follow)
        self._changed()
        if notify and self.on_remove:
            self.on_remove(gone)
        return gone

    def remove_selected(self) -> List[str]:
        """What the Delete key and a Remove button do."""
        return self.remove_rows(list(self.tree.selection()), notify=True)

    def clear(self) -> None:
        self.set_rows([])

    def ids(self) -> List[str]:
        return list(self._order)

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, row_id: object) -> bool:
        return row_id in self._rows

    def row(self, row_id: str) -> Dict[str, Any]:
        """A copy of what the table knows about a row."""
        row = self._rows[row_id]
        return {"id": row_id, "values": dict(row.values), "checked": row.checked,
                "status": row.status, "tips": dict(row.tips), "dim": row.dim}

    def value(self, row_id: str, key: str) -> str:
        return self._rows[row_id].values.get(key, "")

    def shown(self, row_id: str, key: str) -> str:
        """The text on screen for a cell (shortened if it had to be)."""
        return self._shown.get((row_id, key), self._rows[row_id].values.get(key, ""))

    # ------------------------------------------------------------------ ticks
    def is_checked(self, row_id: str) -> Optional[bool]:
        return self._rows[row_id].checked

    def checked_ids(self) -> List[str]:
        return [row_id for row_id in self._order if self._rows[row_id].checked]

    def set_checked(self, row_ids: Iterable[str], checked: bool, notify: bool = False) -> List[str]:
        """Tick or untick rows that can be ticked. Returns the ids that changed."""
        changed = []
        for row_id in row_ids:
            row = self._rows.get(row_id)
            if row is None or row.checked is None or row.checked == checked:
                continue
            row.checked = checked
            self.tree.item(row_id, image=self._tick_image(row))
            changed.append(row_id)
        if changed and notify and self.on_toggle:
            self.on_toggle(changed, checked)
        return changed

    def check_all(self, checked: bool, notify: bool = True) -> List[str]:
        """"Trim all" and "Trim none"."""
        return self.set_checked(self._order, checked, notify)

    def toggle(self, row_ids: Iterable[str]) -> List[str]:
        """What Space does: if any of the rows is unticked, tick them all; else untick them."""
        usable = [row_id for row_id in row_ids if row_id in self._rows and self._rows[row_id].checked is not None]
        if not usable or not self.enabled:
            return []
        target = not all(self._rows[row_id].checked for row_id in usable)
        return self.set_checked(usable, target, notify=True)

    def toggle_all(self) -> List[str]:
        """What a click on the heading of the tick column does."""
        return self.toggle(self._order)

    # ------------------------------------------------------------------ selection
    def selection(self) -> List[str]:
        return list(self.tree.selection())

    def select(self, row_ids: Iterable[str], see: bool = True) -> None:
        wanted = [row_id for row_id in row_ids if row_id in self._rows]
        self.tree.selection_set(wanted)
        if wanted:
            self.tree.focus(wanted[0])
            if see:
                self.see(wanted[0])

    def see(self, row_id: str) -> None:
        """Scroll until the row is in view (once the table is on screen)."""
        if row_id not in self._rows:
            return
        if self.tree.winfo_ismapped() and self.tree.winfo_height() > self._look.row_height:
            self._pending_see = None
            self.tree.see(row_id)
        else:
            self._pending_see = row_id          # a table without a size yet would scroll wrongly

    def set_enabled(self, enabled: bool = True) -> None:
        """A locked table still scrolls and selects, but ticks, names and rows cannot change."""
        self.enabled = bool(enabled)
        if not enabled:
            self._end_rename(False)

    # ------------------------------------------------------------------ the empty state
    def set_empty(self, text: str = "", link_text: str = "",
                  link_command: Optional[Callable[[], Any]] = None, link_lead: str = "") -> None:
        """What an empty table says in its middle, with an optional link under it."""
        self._empty_spec = (text, link_text, link_command, link_lead)
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        self._show_empty()

    def _show_empty(self) -> None:
        text, link_text, link_command, link_lead = self._empty_spec
        wanted = bool(text) and not self._order
        if not wanted:
            if self._empty is not None:
                self._empty.place_forget()
            return
        look = self._look
        if self._empty is None:
            holder = Frame(self.tree, ground="surface")
            words = WrapLabel(holder, text, "soft", wrap=420, justify="center", anchor="center")
            words.pack(fill="x")
            if link_text:
                line = Frame(holder)
                line.pack(pady=(look.px(8), 0))
                if link_lead:
                    label(line, link_lead, "muted").pack(side="left")
                LinkLabel(line, link_text, link_command).pack(side="left")
            self._empty = holder
        self._empty.place(relx=0.5, rely=0.55, anchor="center", width=look.px(440))

    # ------------------------------------------------------------------ rename in place
    def begin_rename(self, row_id: Optional[str] = None, column: Optional[str] = None) -> bool:
        """Open the field over a cell. Without arguments: the focused row, the first
        editable column. True if editing began."""
        if not self.enabled:
            return False
        if column is None:
            editable = [c.key for c in self.columns if c.editable]
            if not editable:
                return False
            column = editable[0]
        elif not self._by_key[column].editable:
            return False
        if row_id is None:
            row_id = self.tree.focus() or (self.tree.selection()[0] if self.tree.selection() else "")
        if not row_id or row_id not in self._rows:
            return False
        self._end_rename(True)
        self._drop_tip()
        self.tree.see(row_id)
        self.tree.update_idletasks()
        box = self.tree.bbox(row_id, column)
        if not box:
            return False
        x, y, width, height = box
        look = self._look
        editor = ttk.Entry(self.tree, style=look.style_name("Cell.TEntry", "surface"), font=look.font("body"))
        editor.insert(0, self._rows[row_id].values.get(column, ""))
        editor.select_range(0, "end")
        editor.icursor("end")
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()
        editor.bind("<Return>", lambda _e: self._end_rename(True, strict=True) or "break")
        editor.bind("<KP_Enter>", lambda _e: self._end_rename(True, strict=True) or "break")
        editor.bind("<Escape>", lambda _e: self._end_rename(False) or "break")
        editor.bind("<FocusOut>", lambda _e: self._end_rename(True))
        editor.bind("<KeyRelease>", self._check_rename, add="+")
        self._editor, self._editing = editor, (row_id, column)
        if self.help_line is not None:
            self.help_line.focus_text(texts.RENAME_HINT, owner=editor)
        return True

    @property
    def editing(self) -> Optional[Tuple[str, str]]:
        """``(row_id, column)`` while a cell is being edited."""
        return self._editing

    @property
    def editor(self) -> Optional[ttk.Entry]:
        return self._editor

    def _rename_problem(self, text: str) -> str:
        if self._editing is None:
            return ""
        row_id, _column = self._editing
        if self.validate_rename is not None:
            return self.validate_rename(row_id, text) or ""
        return "" if text.strip() else texts.RENAME_EMPTY

    def _check_rename(self, _event: Any = None) -> None:
        editor = self._editor
        if editor is None or self._editing is None:
            return
        row_id, column = self._editing
        text = editor.get()
        problem = "" if text == self._rows[row_id].values.get(column, "") else self._rename_problem(text)
        editor.state(["invalid"] if problem else ["!invalid"])
        if self.help_line is not None:
            if problem:
                self.help_line.say(problem, "warn", seconds=0)
            else:
                self.help_line.say("", seconds=0)

    def _end_rename(self, accept: bool, strict: bool = False) -> None:
        """Close the field. ``accept``: take the text if it is valid. ``strict`` (the Enter
        key): an invalid text keeps the field open instead of dropping the edit."""
        editor, editing = self._editor, self._editing
        if editor is None or editing is None:
            return
        row_id, column = editing
        old = self._rows[row_id].values.get(column, "") if row_id in self._rows else ""
        new = editor.get()
        problem = self._rename_problem(new) if (accept and new != old) else ""
        if accept and problem and strict:
            self._check_rename()
            return
        self._editor, self._editing = None, None
        if self.help_line is not None:
            self.help_line.say("", seconds=0)
            self.help_line.clear_focus(owner=editor)
        had_focus = False
        try:
            had_focus = editor.focus_get() is editor
        except (tk.TclError, KeyError):
            pass
        try:
            editor.destroy()
        except tk.TclError:
            pass
        if had_focus and _alive(self.tree):
            self.tree.focus_set()
        if accept and not problem and new != old and row_id in self._rows:
            self._rows[row_id].values[column] = new
            self.tree.set(row_id, column, self._display(row_id, column))
            self._later("status")
            if self.on_rename:
                self.on_rename(row_id, old, new)

    # ------------------------------------------------------------------ drawing rows
    def _tick_image(self, row: _Row) -> Any:
        icons = self._look.icons
        return icons.tick("na" if row.checked is None else ("on" if row.checked else "off"))

    def _tags(self, row_id: str, index: Optional[int] = None) -> Tuple[str, ...]:
        if index is None:
            index = self._order.index(row_id)
        tags = []
        if index % 2:
            tags.append("odd")
        if self._rows[row_id].dim:
            tags.append("dim")
        return tuple(tags)

    def _display(self, row_id: str, key: str) -> str:
        text = self._rows[row_id].values.get(key, "")
        column = self._by_key[key]
        if column.shorten and text:
            room = self._widths.get(key, 0) - 2 * self._look.px(8) - self._look.px(2)
            if column.status:
                room -= self._look.px(10) + self._chars.char(" ")      # the dot before the words
            if room > 0:
                cut = shorten_to_width if column.shorten == "middle" else shorten_end
                text = cut(text, room, self._chars.char)
        self._shown[(row_id, key)] = text
        return text

    def _insert(self, row_id: str, index: int, where: Any) -> None:
        row = self._rows[row_id]
        options: Dict[str, Any] = {"values": [self._display(row_id, key) for key in self._keys],
                                   "tags": self._tags(row_id, index)}
        if self.checks:
            options["image"] = self._tick_image(row)
        self.tree.insert("", where, iid=row_id, **options)

    def _restripe(self) -> None:
        for index, row_id in enumerate(self._order):
            self.tree.item(row_id, tags=self._tags(row_id, index))

    def _measure_columns(self) -> bool:
        """Read the column widths; True if a column that shortens its text changed."""
        changed = False
        for column in self.columns:
            width = int(self.tree.column(column.key, "width"))
            if self._widths.get(column.key) != width:
                self._widths[column.key] = width
                changed = changed or bool(column.shorten)
        return changed

    def _changed(self) -> None:
        self._show_empty()
        self._later("status")

    # ------------------------------------------------------------------ deferred work
    def _later(self, what: str) -> None:
        if self._jobs.get(what) is None and _alive(self):
            self._jobs[what] = self.after_idle(self._layout if what == "layout" else self._paint_status)

    def _layout(self) -> None:
        """After a resize: shorten names again for the new widths, move what lies on top."""
        self._jobs["layout"] = None
        if not _alive(self.tree):
            return
        if self._pending_see is not None:
            self.see(self._pending_see)
        if self._measure_columns():
            for column in self.columns:
                if not column.shorten:
                    continue
                for row_id in self._order:
                    before = self._shown.get((row_id, column.key))
                    after = self._display(row_id, column.key)
                    if after != before:
                        self.tree.set(row_id, column.key, after)
        if self._editor is not None and self._editing is not None:
            box = self.tree.bbox(*self._editing)
            if box:
                self._editor.place(x=box[0], y=box[1], width=box[2], height=box[3])
            else:
                self._end_rename(True)
        self._paint_status()

    def _on_scroll(self, first: str, last: str) -> None:
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()
        self.scrollbar.set(first, last)
        if self._editor is not None:
            self._end_rename(True)
        self._drop_tip()
        self._paint_status()

    def _visible_ids(self) -> List[str]:
        total = len(self._order)
        if not total:
            return []
        try:
            first, last = self.tree.yview()
        except tk.TclError:
            return []
        start = max(0, int(first * total + 1e-6))
        end = min(total, int(math.ceil(last * total - 1e-6)) + 1)
        return self._order[start:end]

    def _paint_status(self) -> None:
        """Lay the coloured status words over their cells (a Treeview colours whole rows only)."""
        self._jobs["status"] = None
        if not self._status_columns or not _alive(self.tree):
            return
        look = self._look
        p = look.palette
        used = 0
        selected = set(self.tree.selection())
        colours = {"ok": p.ok, "warn": p.warn, "bad": p.bad, "accent": p.accent, "muted": p.muted}
        for row_id in self._visible_ids():
            row = self._rows.get(row_id)
            if row is None:
                continue
            for key in self._status_columns:
                text = self._shown.get((row_id, key), row.values.get(key, ""))
                if not text:
                    continue
                try:
                    box = self.tree.bbox(row_id, key)
                except tk.TclError:
                    box = ""
                if not box:
                    continue
                if used >= len(self._status_labels):
                    self._status_labels.append(self._make_status_label())
                item = self._status_labels[used]
                used += 1
                kind = row.status if row.status in colours else ""
                is_selected = row_id in selected
                back = look.select_bg if is_selected else (
                    look.table_alt if self._order.index(row_id) % 2 else look.table_bg)
                ink = colours.get(kind, p.muted if row.dim else p.text)
                if is_selected and look.high_contrast:
                    ink = look.select_fg
                dot = look.icons.dot(kind, size=10) if kind else look.icons.dot("border_strong", hollow=True, size=10)
                item.configure(text=" " + text, image=dot, background=back, foreground=ink)
                item.place(x=box[0], y=box[1], width=box[2], height=box[3])
        for item in self._status_labels[used:]:
            item.place_forget()

    def _make_status_label(self) -> tk.Label:
        look = self._look
        item = tk.Label(self.tree, compound="left", anchor="w", font=look.font("body"),
                        padx=look.px(8), pady=0, borderwidth=0, highlightthickness=0)
        # the words lie on top of the table: hand every mouse event down to it
        for sequence in ("<Button-1>", "<ButtonRelease-1>", "<Double-Button-1>", "<B1-Motion>",
                         "<Motion>", "<MouseWheel>", "<Button-4>", "<Button-5>"):
            item.bind(sequence, lambda event, s=sequence, w=item: self._hand_down(w, s, event))
        return item

    def _hand_down(self, item: tk.Label, sequence: str, event: Any) -> str:
        options: Dict[str, Any] = {"x": item.winfo_x() + event.x, "y": item.winfo_y() + event.y,
                                   "rootx": event.x_root, "rooty": event.y_root}
        if sequence == "<Double-Button-1>":
            # Tk refuses to generate a double click: do what the table would do with it
            event.x, event.y = options["x"], options["y"]
            self._on_double_click(event)
            return "break"
        if isinstance(event.state, int):
            options["state"] = event.state
        if sequence == "<MouseWheel>":
            options["delta"] = event.delta
        try:
            self.tree.event_generate(sequence, **options)
        except tk.TclError:
            pass
        return "break"

    # ------------------------------------------------------------------ events
    def _cell_at(self, x: int, y: int) -> Tuple[str, str, str]:
        """(region, row id, column key or "#0") under a point of the tree."""
        region = self.tree.identify_region(x, y)
        row_id = self.tree.identify_row(y) if region in ("tree", "cell") else ""
        column = self.tree.identify_column(x)
        key = "#0"
        if column and column != "#0":
            try:
                key = self._keys[int(column[1:]) - 1]
            except (ValueError, IndexError):
                key = ""
        return region, row_id, key

    def _on_click(self, event: Any) -> Optional[str]:
        self._drop_tip()
        region, row_id, key = self._cell_at(event.x, event.y)
        if self._editor is not None:
            self._end_rename(True)
        if self.checks and key == "#0" and row_id and region in ("tree", "cell"):
            self.tree.focus_set()
            self.tree.focus(row_id)
            self.toggle([row_id])
            return "break"
        return None

    def _on_double_click(self, event: Any) -> Optional[str]:
        region, row_id, key = self._cell_at(event.x, event.y)
        if not row_id or region not in ("tree", "cell"):
            return None
        if key == "#0":
            if self.checks:
                self.toggle([row_id])           # a quick second click is a second toggle
            return "break"
        if key and self._by_key[key].editable and self.enabled:
            self.begin_rename(row_id, key)
            return "break"
        if self.on_activate:
            self.on_activate(row_id)
        return "break"

    def _on_space(self, _event: Any) -> str:
        if self.checks:
            rows = list(self.tree.selection()) or ([self.tree.focus()] if self.tree.focus() else [])
            self.toggle(rows)
        return "break"

    def _on_delete(self, _event: Any) -> str:
        if self.removable and self.enabled:
            self.remove_selected()
        return "break"

    def _on_return(self, _event: Any) -> str:
        row_id = self.tree.focus()
        if row_id and self.on_activate:
            self.on_activate(row_id)
        return "break"

    def _select_all(self, _event: Any) -> str:
        self.tree.selection_set(self._order)
        return "break"

    def _on_select(self, _event: Any) -> None:
        self._paint_status()
        if self.on_select:
            self.on_select(list(self.tree.selection()))

    # -- hover text of headings and cells
    def tip_for(self, region: str, row_id: str, key: str) -> str:
        """The hover text for a place in the table (also used by the tests)."""
        if region == "heading":
            return self.check_hover if key == "#0" else (self._by_key[key].hover if key in self._by_key else "")
        if not row_id or row_id not in self._rows:
            return ""
        row = self._rows[row_id]
        if key == "#0":
            return row.tips.get("#0", self.check_hover)
        if key not in self._by_key:
            return ""
        if key in row.tips:
            return row.tips[key]
        full = row.values.get(key, "")
        if self._shown.get((row_id, key), full) != full:
            return full
        room = self._widths.get(key, 0) - 2 * self._look.px(8)
        if full and not self._by_key[key].shorten and room > 0 and self._chars.text(full) > room:
            return full                              # cut off at the right by the table itself
        return ""

    def _on_motion(self, event: Any) -> None:
        region, row_id, key = self._cell_at(event.x, event.y)
        cell = (region if region == "heading" else row_id, key)
        if cell == self._tip_cell:
            return
        self._tip_cell = cell
        text = self.tip_for(region, row_id, key)
        self._tip_text = text
        manager = _tips(self.tree)
        if text:
            manager.schedule(self.tree, text, 500)
        else:
            manager.cancel(self.tree)
        if self.help_line is not None:
            if text:
                self.help_line.hover_text(text, owner=self.tree)
            else:
                self.help_line.clear_hover(owner=self.tree)

    def _on_leave(self, _event: Any) -> None:
        self._drop_tip()

    def _drop_tip(self) -> None:
        self._tip_cell = None
        self._tip_text = ""
        try:
            _tips(self.tree).cancel(self.tree)
            if self.help_line is not None:
                self.help_line.clear_hover(owner=self.tree)
        except tk.TclError:
            pass

    def _on_destroy(self, event: Any) -> None:
        if event.widget is not self.tree:
            return
        for what, job in list(self._jobs.items()):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                self._jobs[what] = None


# =========================================================================== options

class Disclosure(ttk.Frame):
    """"More options": a header that opens and closes a body, closed by default.

    The header is a button (Tab, then Space or Enter). A small "changed" mark appears
    beside it when something inside differs from the standard, so a closed section never
    hides a changed setting. Put the content in ``body``.
    """

    def __init__(self, parent: Any, text: str = texts.BUTTON["more_options"], *, opened: bool = False,
                 command: Optional[Callable[[bool], Any]] = None, changed_text: str = texts.STEP_CHANGED,
                 help_line: Optional[HelpLine] = None, indent: int = 22, **options: Any):
        self.ground = ground_of(parent)
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self.command = command
        self._open = False
        self._changed = False
        self.columnconfigure(2, weight=1)
        self.header = ttk.Button(self, text=text, compound="left", command=self.toggle,
                                 image=look.icons.chevron("right", "text_soft"),
                                 style=look.style_name("Disclosure.TButton", self.ground))
        self.header.grid(row=0, column=0, sticky="w")
        self.header.bind("<Return>", lambda _e: self.toggle(), add="+")
        self.mark = ttk.Label(self, text=changed_text, style=look.style_name("Badge.TLabel", self.ground))
        self.body = Frame(self)
        self._indent = look.px(indent)
        explain(self.header, texts.HOVER["more_options"], help_line)
        if opened:
            self.open()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def changed(self) -> bool:
        return self._changed

    def open(self) -> None:
        self._set_open(True)

    def close(self) -> None:
        self._set_open(False)

    def toggle(self) -> None:
        self._set_open(not self._open)

    def set_changed(self, changed: bool = True) -> None:
        """Show or hide the "changed" mark."""
        self._changed = bool(changed)
        if self._changed:
            self.mark.grid(row=0, column=1, sticky="w", padx=(self._look.px(8), 0))
        else:
            self.mark.grid_remove()

    def _set_open(self, opened: bool) -> None:
        if opened == self._open:
            return
        self._open = opened
        look = self._look
        self.header.configure(image=look.icons.chevron("down" if opened else "right", "text_soft"))
        if opened:
            self.body.grid(row=1, column=0, columnspan=3, sticky="nsew",
                           padx=(self._indent, 0), pady=(look.px(6), 0))
        else:
            self.body.grid_remove()
        if self.command is not None:
            self.command(opened)


_WHOLE = re.compile(r"^[+-]?\d+$")
_DECIMAL = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)$")


def parse_number(text: str, *, decimal: bool = False) -> Optional[float]:
    """The number typed into a field, or ``None`` if it is not one.

    Whole numbers: digits only. Decimal: a dot or a comma (``5.5`` and ``5,5`` are the
    same number; half of the keyboards have a comma on the number pad). Spaces around
    the number are ignored. Nothing else is accepted: no exponents, no thousands marks.
    """
    cleaned = text.strip().replace(" ", "")
    if decimal:
        if not _DECIMAL.match(cleaned):
            return None
        return float(cleaned.replace(",", "."))
    if not _WHOLE.match(cleaned):
        return None
    return int(cleaned)


def format_number(value: float, *, decimal: bool = False, decimals: int = 1) -> str:
    """How a number field shows a value: ``"500"``, ``"5.0"``."""
    return "%.*f" % (decimals, value) if decimal else "%d" % int(value)


def settle_number(text: str, minimum: float, maximum: float, last_good: float, *,
                  decimal: bool = False, decimals: int = 1) -> Tuple[float, str]:
    """Decide what a number field holds when it is left.

    Returns ``(value, message)``. A number inside the range is taken (a decimal is rounded
    to ``decimals`` places) and the message is empty. Anything else gives back
    ``last_good`` and the sentence for the help line.
    """
    value = parse_number(text, decimal=decimal)
    if value is not None and decimal:
        value = round(value, decimals)
    if value is None or value < minimum or value > maximum:
        return last_good, texts.number_reset(minimum, maximum, last_good, decimal, decimals)
    return (float(value) if decimal else int(value)), ""


class NumberField(ttk.Entry):
    """A field for one number with a fixed range.

    Whole numbers by default; ``decimal=True`` takes one decimal number and accepts both
    ``5.5`` and ``5,5``. Typing is free. When the field is left (or Enter is pressed) the
    text is checked: a value out of range, or not a number, goes back to the last good
    value and the help line says so ("Use a whole number from 0 to 40. Set back to 10.").
    Never a pop-up. Up and Down change the value by ``step``; Esc restores the last good
    value. ``command(value)`` is called when the value has changed.
    """

    def __init__(self, parent: Any, *, value: float, minimum: float, maximum: float,
                 decimal: bool = False, decimals: int = 1, step: Optional[float] = None,
                 width: Optional[int] = None, command: Optional[Callable[[float], Any]] = None,
                 help_line: Optional[HelpLine] = None, **options: Any):
        self.minimum, self.maximum = minimum, maximum
        self.decimal, self.decimals = decimal, decimals
        self.step = step if step is not None else (10 ** -decimals if decimal else 1)
        self.command = command
        self.help_line = help_line
        self.last_message = ""
        self._good = self._clamp(value)
        if width is None:
            width = max(len(format_number(maximum, decimal=decimal, decimals=decimals)),
                        len(format_number(minimum, decimal=decimal, decimals=decimals))) + 1
            width = max(4, width)
        options.setdefault("style", style_for(parent, "TEntry"))
        options.setdefault("justify", "right")
        super().__init__(parent, width=width, **options)
        self._write(self._good)
        self.bind("<FocusOut>", lambda _e: self.commit(), add="+")
        self.bind("<Return>", lambda _e: self.commit() and None, add="+")
        self.bind("<KP_Enter>", lambda _e: self.commit() and None, add="+")
        self.bind("<Escape>", self._restore, add="+")
        self.bind("<Up>", lambda _e: self._nudge(+1))
        self.bind("<Down>", lambda _e: self._nudge(-1))

    # -- value
    @property
    def value(self) -> float:
        """The value in force. Text not yet checked is checked first."""
        return self.commit()

    def set_value(self, value: float, notify: bool = False) -> None:
        """Set from the program (Reset to standard). Out-of-range values are clamped."""
        before = self._good
        self._good = self._clamp(value)
        self._write(self._good)
        if notify and self.command is not None and self._good != before:
            self.command(self._good)

    def commit(self) -> float:
        """Check the text now. Returns the value in force afterwards."""
        try:
            text = self.get()
        except tk.TclError:
            return self._good
        value, message = settle_number(text, self.minimum, self.maximum, self._good,
                                       decimal=self.decimal, decimals=self.decimals)
        self.last_message = message
        changed = value != self._good
        self._good = value
        if format_number(value, decimal=self.decimal, decimals=self.decimals) != text:
            self._write(value)
        if message and self.help_line is not None:
            self.help_line.say(message, "warn")
        if changed and self.command is not None:
            self.command(value)
        return value

    def set_enabled(self, enabled: bool = True) -> None:
        self.state(["!disabled"] if enabled else ["disabled"])

    # -- internal
    def _clamp(self, value: float) -> float:
        value = min(self.maximum, max(self.minimum, value))
        return round(float(value), self.decimals) if self.decimal else int(value)

    def _write(self, value: float) -> None:
        was_disabled = self.instate(["disabled"])
        if was_disabled:
            self.state(["!disabled"])
        self.delete(0, "end")
        self.insert(0, format_number(value, decimal=self.decimal, decimals=self.decimals))
        if was_disabled:
            self.state(["disabled"])

    def _restore(self, _event: Any = None) -> None:
        self._write(self._good)
        self.select_range(0, "end")

    def _nudge(self, direction: int) -> str:
        base = parse_number(self.get(), decimal=self.decimal)
        if base is None:
            base = self._good
        self._write(self._clamp(base + direction * self.step))
        self.commit()
        self.select_range(0, "end")
        return "break"


class OptionRow(ttk.Frame):
    """One line of an options page, built from left to right.

    ``add_tick``, ``add_radio``, ``add_text``, ``add_number``, ``add_entry`` put the parts
    side by side, evenly spaced and centred on one line; every row is at least as tall as
    a text field, so a page of rows keeps an even rhythm. ``explain`` gives the whole row
    its hover text.
    """

    def __init__(self, parent: Any, *, help_text: str = "", help_line: Optional[HelpLine] = None,
                 indent: int = 0, **options: Any):
        self.ground = ground_of(parent)
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self.help_line = help_line
        self.parts: List[Any] = []
        self.tip: Optional[Tooltip] = None
        self._gap = look.px(6)
        strut = tk.Frame(self, width=look.px(indent), height=look.control_height,
                         background=look.ground_colour(self.ground), highlightthickness=0, borderwidth=0)
        strut.pack(side="left")
        self._help_text = help_text

    # -- parts
    def add(self, widget: Any, gap: Optional[int] = None) -> Any:
        """Put any widget (a child of this row) next in line."""
        space = self._gap if gap is None else self._look.px(gap)
        widget.pack(side="left", padx=(space if self.parts else 0, 0))
        self.parts.append(widget)
        return widget

    def add_tick(self, text: str, variable: Optional[tk.Variable] = None,
                 command: Optional[Callable[[], Any]] = None) -> ttk.Checkbutton:
        return self.add(tick(self, text, variable, command))

    def add_radio(self, text: str, variable: tk.Variable, value: Any,
                  command: Optional[Callable[[], Any]] = None, gap: Optional[int] = None) -> ttk.Radiobutton:
        return self.add(radio(self, text, variable, value, command), gap)

    def add_text(self, text: str, kind: str = "body", gap: Optional[int] = None) -> ttk.Label:
        return self.add(label(self, text, kind), gap)

    def add_number(self, **options: Any) -> NumberField:
        options.setdefault("help_line", self.help_line)
        return self.add(NumberField(self, **options))

    def add_entry(self, textvariable: Optional[tk.Variable] = None, width: int = 18, **options: Any) -> ttk.Entry:
        made = entry(self, width=width, **options)
        if textvariable is not None:
            made.configure(textvariable=textvariable)
        return self.add(made)

    def finish(self) -> "OptionRow":
        """Attach the hover text given at creation. Call after the last part was added."""
        if self._help_text:
            self.explain(self._help_text)
        return self

    def explain(self, text: str, help_line: Optional[HelpLine] = None) -> Tooltip:
        """Hover text for the whole row (tooltip and help line)."""
        self.tip = explain(self, text, help_line or self.help_line)
        return self.tip

    def set_enabled(self, enabled: bool = True, only: Optional[Sequence[Any]] = None) -> None:
        """Grey the parts of the row out, or bring them back."""
        flag = ["!disabled"] if enabled else ["disabled"]
        for part in (only if only is not None else self.parts):
            try:
                part.state(flag)
            except (AttributeError, tk.TclError):
                pass


class TickRow(OptionRow):
    """A tick box with its label, an optional number field in the same line, and its
    hover text: ``[x] I know the genome size: about [5.0] Mb``.

    ``number`` is the set of arguments for a :class:`NumberField`. With ``gate`` the field
    can be typed in only while the box is ticked. ``command(ticked)`` is called on a click.
    """

    def __init__(self, parent: Any, text: str, *, variable: Optional[tk.BooleanVar] = None,
                 value: bool = False, command: Optional[Callable[[bool], Any]] = None,
                 number: Optional[Mapping[str, Any]] = None, suffix: str = "",
                 help_text: str = "", help_line: Optional[HelpLine] = None, gate: bool = True,
                 indent: int = 0, **options: Any):
        super().__init__(parent, help_text=help_text, help_line=help_line, indent=indent, **options)
        self.var = variable if variable is not None else tk.BooleanVar(self, value=value)
        self.command = command
        self.gate = gate
        self.tick = self.add_tick(text, self.var, self._clicked)
        self.field: Optional[NumberField] = None
        self.suffix: Optional[ttk.Label] = None
        if number is not None:
            self.field = self.add_number(**dict(number))
        if suffix:
            self.suffix = self.add_text(suffix)
        self._gate()
        self.finish()
        if self.field is not None and gate:
            # "Reset to standard" sets the variable directly: keep the field in step with it
            trace = self.var.trace_add("write", lambda *_args: self._gate())
            self.bind("<Destroy>", lambda event, t=trace: self._untrace(event, t), add="+")

    def _untrace(self, event: Any, trace: str) -> None:
        if event.widget is self:
            try:
                self.var.trace_remove("write", trace)
            except (tk.TclError, ValueError):
                pass

    @property
    def checked(self) -> bool:
        return bool(self.var.get())

    def set(self, checked: bool) -> None:
        """Set from the program (no callback)."""
        self.var.set(bool(checked))
        self._gate()

    def _clicked(self) -> None:
        self._gate()
        if self.command is not None:
            self.command(self.checked)

    def _gate(self) -> None:
        if self.field is not None and self.gate:
            try:
                usable = self.checked and self.tick.instate(["!disabled"])
                self.field.set_enabled(usable)
                if self.suffix is not None:
                    self.suffix.state(["!disabled"] if usable else ["disabled"])
            except tk.TclError:
                pass

    def set_enabled(self, enabled: bool = True, only: Optional[Sequence[Any]] = None) -> None:
        super().set_enabled(enabled, only)
        if enabled:
            self._gate()


class NumberRow(OptionRow):
    """A label, a number field and the words after it: ``Remove reads shorter than [50] bases``."""

    def __init__(self, parent: Any, text: str, *, number: Mapping[str, Any], suffix: str = "",
                 help_text: str = "", help_line: Optional[HelpLine] = None, indent: int = 0,
                 label_width: Optional[int] = None, **options: Any):
        super().__init__(parent, help_text=help_text, help_line=help_line, indent=indent, **options)
        self.label = self.add_text(text)
        if label_width:
            self.label.configure(width=label_width)
        self.field = self.add_number(**dict(number))
        self.suffix = self.add_text(suffix) if suffix else None
        self.finish()


class ChoiceRow(ttk.Frame):
    """A label and a set of radio buttons, each with its own hover text:
    ``Assembly mode   (o) Standard (recommended)  ( ) Fast  ( ) Careful  ( ) Aggressive``.

    ``choices`` is a sequence of ``(value, label, hover text)``. ``command(value)`` is
    called when the user picks one. Arrow keys move between the choices.
    """

    def __init__(self, parent: Any, text: str, choices: Sequence[Tuple[str, str, str]], *,
                 variable: Optional[tk.StringVar] = None, value: Optional[str] = None,
                 command: Optional[Callable[[str], Any]] = None, help_text: str = "",
                 help_line: Optional[HelpLine] = None, vertical: bool = False, **options: Any):
        self.ground = ground_of(parent)
        look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        first = choices[0][0] if choices else ""
        self.var = variable if variable is not None else tk.StringVar(self, value=value if value is not None else first)
        self.command = command
        self.buttons: Dict[str, ttk.Radiobutton] = {}
        self.label = label(self, text)
        strut = tk.Frame(self, width=0, height=look.control_height, background=look.ground_colour(self.ground),
                         highlightthickness=0, borderwidth=0)
        if vertical:
            self.label.grid(row=0, column=0, sticky="w")
        else:
            strut.grid(row=0, column=0)
            self.label.grid(row=0, column=1, sticky="w", padx=(0, look.px(14)))
        if help_text:
            explain(self.label, help_text, help_line)
        for index, (choice, words, hover) in enumerate(choices):
            made = radio(self, words, self.var, choice, self._picked)
            if vertical:
                made.grid(row=index + 1, column=0, sticky="w", pady=(look.px(4), 0))
            else:
                made.grid(row=0, column=index + 2, sticky="w", padx=(0, look.px(16)))
            if hover:
                explain(made, hover, help_line)
            self.buttons[choice] = made

    @property
    def value(self) -> str:
        return str(self.var.get())

    def set(self, value: str) -> None:
        """Set from the program (no callback)."""
        self.var.set(value)

    def set_enabled(self, enabled: bool = True) -> None:
        flag = ["!disabled"] if enabled else ["disabled"]
        self.label.state(flag)
        for made in self.buttons.values():
            made.state(flag)

    def _picked(self) -> None:
        if self.command is not None:
            self.command(self.value)


# =========================================================================== run page pieces

_LEVEL_STYLE = {"info": "Info", "ok": "Ok", "warn": "Warn", "bad": "Bad"}


class NoticeBar(ttk.Frame):
    """Coloured bars that tell something without stopping anything.

    ``show("battery", "Running on battery...", "warn")`` puts up a bar; showing the same
    ``kind`` again replaces its words in place; ``dismiss("battery")`` (or its close
    button, or showing an empty text) takes it away. Levels: info, ok, warn, bad. Bars
    stack in the order they first appeared. With no bar the widget takes no room.
    """

    def __init__(self, parent: Any, **options: Any):
        self.ground = ground_of(parent)
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self.columnconfigure(0, weight=1)
        self._bars: Dict[str, Dict[str, Any]] = {}
        self._rows = 0

    def show(self, kind: str, text: str, level: str = "warn", *,
             actions: Sequence[Tuple[str, Callable[[], Any]]] = (), dismissible: bool = True,
             on_dismiss: Optional[Callable[[str], Any]] = None) -> None:
        """Show or replace the bar of ``kind``. ``actions`` are ``(label, callback)`` buttons."""
        if not text:
            self.dismiss(kind)
            return
        if level not in _LEVEL_STYLE:
            level = "warn"
        existing = self._bars.get(kind)
        row = existing["row"] if existing else self._rows
        if existing:
            existing["frame"].destroy()
        else:
            self._rows += 1
        look = self._look
        px = look.px
        prefix = _theme.GROUNDS[self.ground] or "OnSurface."
        style = "%s.Notice.TFrame" % _LEVEL_STYLE[level]
        frame = ttk.Frame(self, style=style if self.ground == "page" else prefix + style,
                          padding=(px(12), px(8), px(8), px(8)))
        frame.ground = level                                             # type: ignore[attr-defined]
        frame.grid(row=row, column=0, sticky="ew", pady=(0, px(8)))
        frame.columnconfigure(1, weight=1)
        colour = {"info": "accent", "ok": "ok", "warn": "warn", "bad": "bad"}[level]
        icon = ttk.Label(frame, image=look.icons.glyph(level, colour, 16), style=look.style_name("TLabel", level))
        icon.grid(row=0, column=0, sticky="n", padx=(0, px(10)), pady=(px(3), 0))
        words = WrapLabel(frame, text, "body", wrap=520)
        words.grid(row=0, column=1, sticky="ew", pady=px(2))
        column = 2
        buttons = []
        for caption, callback in actions:
            made = button(frame, caption, callback)
            made.grid(row=0, column=column, padx=(px(8), 0), sticky="n")
            buttons.append(made)
            column += 1
        close = None
        if dismissible:
            close = ttk.Button(frame, image=look.icons.glyph("close", "text_soft", 14),
                               style=look.style_name("Tool.TButton", level), padding=px(5),
                               command=lambda: self.dismiss(kind, by_user=True))
            close.grid(row=0, column=column, padx=(px(6), 0), sticky="n")
            Tooltip(close, texts.BUTTON["dismiss"])
        self._bars[kind] = {"frame": frame, "row": row, "text": text, "level": level, "words": words,
                            "buttons": buttons, "close": close, "on_dismiss": on_dismiss}

    def dismiss(self, kind: str, by_user: bool = False) -> bool:
        """Take the bar of ``kind`` away. True if there was one."""
        bar = self._bars.pop(kind, None)
        if bar is None:
            return False
        bar["frame"].destroy()
        if by_user and bar["on_dismiss"] is not None:
            bar["on_dismiss"](kind)
        return True

    def clear(self) -> None:
        for kind in list(self._bars):
            self.dismiss(kind)

    def handle(self, notice: Any) -> None:
        """Show a ``model.Notice`` event (an empty text clears the bar of that kind)."""
        self.show(notice.kind, notice.text, notice.level or "warn")

    def kinds(self) -> List[str]:
        return sorted(self._bars, key=lambda kind: self._bars[kind]["row"])

    def text(self, kind: str) -> str:
        return self._bars[kind]["text"] if kind in self._bars else ""

    def level(self, kind: str) -> str:
        return self._bars[kind]["level"] if kind in self._bars else ""

    def button_of(self, kind: str, index: int = 0) -> ttk.Button:
        """An action button of a bar (for tests and for giving it focus)."""
        return self._bars[kind]["buttons"][index]


class WorkingIndicator(ttk.Frame):
    """A small turning ring with optional words beside it.

    It turns only while the window's own event loop is running, so a ring that moves
    proves the window is alive. ``start`` and ``stop`` switch it; stopped, it shows a still
    ring (or nothing with ``hide_when_stopped``).
    """

    def __init__(self, parent: Any, text: str = "", *, size: int = 16, kind: str = "soft",
                 hide_when_stopped: bool = False, **options: Any):
        self.ground = ground_of(parent)
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        self._size = size
        self._hide = hide_when_stopped
        self._frames = look.icons.spinner("accent", size)
        self._still = look.icons.blank(look.px(size), look.px(size)) if hide_when_stopped \
            else look.icons.ring("border", size)
        self.running = False
        self.ring = ttk.Label(self, image=self._still, style=look.style_name("TLabel", self.ground))
        self.ring.pack(side="left")
        self.words = label(self, text, kind)
        self.words.pack(side="left", padx=(look.px(8), 0))
        self.bind("<Destroy>", self._gone, add="+")

    def _gone(self, event: Any) -> None:
        if event.widget is self:
            self.running = False
            try:
                _animator(self).remove(self)
            except Exception:
                pass

    def start(self) -> None:
        if not self.running:
            self.running = True
            _animator(self).add(self, self._turn)

    def stop(self) -> None:
        if self.running:
            self.running = False
            _animator(self).remove(self)
            self.ring.configure(image=self._still)

    def set_text(self, text: str) -> None:
        self.words.configure(text=text)

    def _turn(self, frame: int) -> None:
        self.ring.configure(image=self._frames[frame % len(self._frames)])


class StageChips(ttk.Frame):
    """The stages of the sample that is running, as a row of chips:
    Join lanes, Trim, Check reads, Assemble, Summarise.

    Each chip is ``waiting``, ``running`` (its ring turns), ``done``, ``skipped`` or
    ``failed``. Stages are named by the values of ``model.Stage`` (the enum members work
    too). "Join lanes" is hidden until :meth:`show_stage` asks for it.
    """

    STATES = ("waiting", "running", "done", "skipped", "failed")

    def __init__(self, parent: Any, stages: Optional[Sequence[Tuple[str, str]]] = None, *,
                 hidden: Sequence[str] = ("join_lanes",), **options: Any):
        self.ground = ground_of(parent)
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("TFrame", self.ground))
        super().__init__(parent, **options)
        if stages is None:
            stages = [(stage.value, words) for stage, words in texts.STAGE_LABEL.items()]
        self.stage_keys = [str(getattr(key, "value", key)) for key, _words in stages]
        self._names = {str(getattr(key, "value", key)): words for key, words in stages}
        self._state = {key: "waiting" for key in self.stage_keys}
        self._hidden = {str(getattr(key, "value", key)) for key in hidden}
        self._chips: Dict[str, ttk.Label] = {}
        self._spin = look.icons.spinner("accent", 12, track="accent_soft")
        for key in self.stage_keys:
            chip = ttk.Label(self, text=self._names[key], compound="left")
            self._chips[key] = chip
            Tooltip(chip, lambda k=key: "%s: %s" % (self._names[k], texts.STAGE_STATE_LABEL[self._state[k]]))
        self.bind("<Destroy>", self._gone, add="+")
        self._arrange()
        self._paint()

    def _gone(self, event: Any) -> None:
        if event.widget is self:
            try:
                _animator(self).remove(self)
            except Exception:
                pass

    @staticmethod
    def _key(stage: Any) -> str:
        return str(getattr(stage, "value", stage))

    def set(self, stage: Any, state: str) -> None:
        key = self._key(stage)
        if state not in self.STATES:
            raise ValueError("no such state: %r" % (state,))
        if key not in self._state:
            raise KeyError(key)
        self._state[key] = state
        self._paint()

    def state_of(self, stage: Any) -> str:
        return self._state[self._key(stage)]

    def reset(self) -> None:
        """All chips back to waiting (a new sample begins)."""
        for key in self.stage_keys:
            self._state[key] = "waiting"
        self._paint()

    def show_stage(self, stage: Any, visible: bool = True) -> None:
        key = self._key(stage)
        if visible:
            self._hidden.discard(key)
        else:
            self._hidden.add(key)
        self._arrange()

    def visible_stages(self) -> List[str]:
        return [key for key in self.stage_keys if key not in self._hidden]

    def _arrange(self) -> None:
        gap = self._look.px(8)
        for chip in self._chips.values():
            chip.pack_forget()
        first = True
        for key in self.stage_keys:
            if key in self._hidden:
                continue
            self._chips[key].pack(side="left", padx=(0 if first else gap, 0))
            first = False

    def _paint(self) -> None:
        look = self._look
        icons = look.icons
        running = False
        for key, chip in self._chips.items():
            state = self._state[key]
            style = look.style_name("%s.Chip.TLabel" % state.capitalize(), self.ground)
            if state == "running":
                running = True
                picture = self._spin[_animator(self).frame % len(self._spin)]
            elif state == "done":
                picture = icons.glyph("check", "ok", 12)
            elif state == "failed":
                picture = icons.glyph("cross", "bad", 12)
            elif state == "skipped":
                picture = icons.glyph("dash", "border_strong", 12)
            else:
                picture = icons.dot("border_strong", hollow=True, size=12)
            chip.configure(style=style, image=picture)
        if running:
            _animator(self).add(self, self._turn)
        else:
            _animator(self).remove(self)

    def _turn(self, frame: int) -> None:
        picture = self._spin[frame % len(self._spin)]
        for key, chip in self._chips.items():
            if self._state[key] == "running":
                chip.configure(image=picture)


class LogPane(ttk.Frame):
    """The technical messages of the engines: read-only, monospaced, at most
    ``max_lines`` lines (older lines are dropped), with a scroll bar.

    New text is added at the end and the view follows it, unless the user has scrolled
    up to read: then the view stays where it is until they scroll back to the end.
    Give :meth:`append` many lines at once rather than one call per line.
    """

    def __init__(self, parent: Any, *, max_lines: int = 2000, height: int = 10, **options: Any):
        look = self._look = _theme.current(parent)
        options.setdefault("style", look.style_name("Well.TFrame", ground_of(parent)))
        super().__init__(parent, padding=look.px(6), **options)
        self.ground = "sunk"
        self.max_lines = max(1, int(max_lines))
        p = look.palette
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(self, height=height, width=40, wrap="char", state="disabled",
                            font=look.font("mono"), background=p.surface_sunk, foreground=p.text_soft,
                            selectbackground=p.accent, selectforeground=p.on_accent,
                            inactiveselectbackground=p.surface_alt, insertwidth=0,
                            borderwidth=0, highlightthickness=0, padx=look.px(6), pady=look.px(4),
                            cursor="arrow", takefocus=1)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview,
                                       style=look.style_name("Vertical.TScrollbar", "sunk"))
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.bind("<Button-1>", lambda _e: self.text.focus_set(), add="+")
        self._lines = 0

    @property
    def line_count(self) -> int:
        return self._lines

    @property
    def following(self) -> bool:
        """True while the end of the log is in view (new lines will keep it in view)."""
        return float(self.text.yview()[1]) >= 0.999

    def append(self, lines: Union[str, Iterable[str]]) -> None:
        """Add text: one string (line ends inside it are kept) or many lines."""
        if isinstance(lines, str):
            chunk = lines if lines.endswith("\n") else lines + "\n"
        else:
            chunk = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
        if chunk == "\n" and isinstance(lines, str) and not lines:
            return
        if not chunk:
            return
        follow = self.following
        text = self.text
        text.configure(state="normal")
        text.insert("end", chunk)
        self._lines += chunk.count("\n")
        extra = self._lines - self.max_lines
        if extra > 0:
            text.delete("1.0", "%d.0" % (extra + 1))
            self._lines -= extra
        text.configure(state="disabled")
        if follow:
            text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._lines = 0

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c")


# =========================================================================== dialogs

class Dialog(tk.Toplevel):
    """A question in a window of our own, never ``tkinter.messagebox``: our buttons carry
    words such as Skip, Overwrite and Stop now.

    ``choices`` are ``texts.Choice`` objects or ``(key, label[, kind[, description]])``
    tuples. One of them is the ``default`` (Enter; it has the focus when the window
    opens) and one is the ``cancel`` (Esc and the window's close box). A choice with a
    ``description`` is listed with that sentence beside its button; the others stand in a
    row at the bottom right. ``details`` adds a closed "Show details" section.

    ``show()`` opens the window on top of its parent, centred, waits, and returns the key
    of the chosen button. The program underneath keeps running while it is open.
    ``Dialog.ask(parent, question)`` does all of it for a ``texts.Question``.
    """

    #: Callables given every dialog window as it opens (dark title bar, icon).
    created_hooks: List[Callable[[tk.Toplevel], Any]] = []

    def __init__(self, parent: Any, *, title: str, body: str = "",
                 choices: Sequence[Any] = (), default: Optional[str] = None,
                 cancel: Optional[str] = None, details: str = "", details_label: str = "",
                 level: str = "", wrap: int = 440):
        look = self._look = _theme.current(parent)
        super().__init__(parent, background=look.palette.bg)
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.parent = parent
        self.result: Optional[str] = None
        self.ground = "page"
        px = look.px
        parsed = [self._choice(item) for item in choices] or [texts.Choice("ok", texts.BUTTON["ok"])]
        keys = [item.key for item in parsed]
        self.default = default if default in keys else keys[0]
        self.cancel = cancel if cancel in keys else keys[-1]
        self.buttons: Dict[str, ttk.Button] = {}

        content = Frame(self, ground="surface", padding=(22, 20, 22, 18))
        content.pack(fill="both", expand=True)
        content.columnconfigure(1, weight=1)
        row = 0
        if level in ("info", "warn", "bad", "ok"):
            colour = {"info": "accent", "ok": "ok", "warn": "warn", "bad": "bad"}[level]
            ttk.Label(content, image=look.icons.glyph(level, colour, 22),
                      style=look.style_name("TLabel", "surface")).grid(row=0, column=0, rowspan=2, sticky="n",
                                                                       padx=(0, px(14)), pady=(px(2), 0))
        self.heading = label(content, title, "heading")
        self.heading.grid(row=row, column=1, sticky="w")
        row += 1
        self.body = ttk.Label(content, text=body, style=look.style_name("TLabel", "surface"),
                              wraplength=px(wrap), justify="left", anchor="nw")
        if body:
            self.body.grid(row=row, column=1, sticky="ew", pady=(px(8), 0))
            row += 1

        listed = [item for item in parsed if item.description]
        in_row = [item for item in parsed if not item.description]
        if listed:
            options = Frame(content)
            options.grid(row=row, column=1, sticky="ew", pady=(px(14), 0))
            options.columnconfigure(1, weight=1)
            row += 1
            for index, item in enumerate(listed):
                made = self._make_button(options, item)
                made.grid(row=index, column=0, sticky="new", pady=(0 if index == 0 else px(10), 0))
                words = ttk.Label(options, text=item.description, style=look.style_name("Soft.TLabel", "surface"),
                                  wraplength=px(wrap - 170), justify="left", anchor="nw")
                words.grid(row=index, column=1, sticky="nw", padx=(px(14), 0),
                           pady=(px(5) if index == 0 else px(15), 0))
        self.details: Optional[Disclosure] = None
        if details:
            self.details = Disclosure(content, details_label or texts.BUTTON["show_details"])
            self.details.grid(row=row, column=1, sticky="ew", pady=(px(12), 0))
            row += 1
            holder = Well(self.details.body, padding=6)
            holder.pack(fill="both", expand=True)
            count = details.count("\n") + 1
            box = tk.Text(holder, height=min(8, max(2, count)), width=44, wrap="word",
                          font=look.font("small"), background=look.palette.surface_sunk,
                          foreground=look.palette.text_soft, borderwidth=0, highlightthickness=0,
                          padx=px(4), pady=px(2), cursor="arrow")
            box.insert("1.0", details)
            box.configure(state="disabled")
            box.grid(row=0, column=0, sticky="nsew")
            holder.columnconfigure(0, weight=1)
            if count > 8:
                bar = ttk.Scrollbar(holder, orient="vertical", command=box.yview,
                                    style=look.style_name("Vertical.TScrollbar", "sunk"))
                bar.grid(row=0, column=1, sticky="ns")
                box.configure(yscrollcommand=bar.set)
            self.details_text = box

        hairline(self).pack(fill="x")
        footer = Frame(self, ground="page", padding=(22, 12, 22, 14))
        footer.pack(fill="x")
        spacer = Frame(footer)
        spacer.pack(side="left", fill="x", expand=True)
        for index, item in enumerate(in_row):
            made = self._make_button(footer, item)
            made.pack(side="left", padx=(0 if index == 0 else px(8), 0))

        self.protocol("WM_DELETE_WINDOW", self._cancelled)
        self.bind("<Escape>", lambda _e: self._cancelled())
        self.bind("<Return>", self._enter)
        self.bind("<KP_Enter>", self._enter)
        self.bind("<Left>", lambda _e: self._step(-1))
        self.bind("<Right>", lambda _e: self._step(+1))
        self._order = [item.key for item in listed] + [item.key for item in in_row]

    # -- building
    @staticmethod
    def _choice(item: Any) -> texts.Choice:
        if isinstance(item, texts.Choice):
            return item
        return texts.Choice(*item)

    def _make_button(self, parent: Any, item: texts.Choice) -> ttk.Button:
        kind = "danger" if item.kind == "danger" else ("default" if item.key == self.default else "plain")
        made = button(parent, item.label, lambda key=item.key: self._chosen(key), kind=kind)
        self.buttons[item.key] = made
        return made

    # -- running
    @classmethod
    def ask(cls, parent: Any, question: texts.Question, **options: Any) -> str:
        """Show a ``texts.Question`` and return the key of the chosen button."""
        dialog = cls(parent, title=question.title, body=question.body, choices=question.choices,
                     default=question.default, cancel=question.cancel, details=question.details,
                     details_label=question.details_label, **options)
        return dialog.show()

    def show(self) -> str:
        """Open, wait for an answer, close. Returns the key of the chosen button."""
        self.open()
        self.wait_window(self)
        return self.result if self.result is not None else self.cancel

    def open(self) -> None:
        """Put the window on screen without waiting (``show`` does both)."""
        top = self.parent.winfo_toplevel()
        hide_tooltips(self.parent)
        try:
            if top.state() == "iconic":          # a question needs its window: bring it back
                top.deiconify()
                top.update_idletasks()
            if top.winfo_viewable():
                self.transient(top)
        except tk.TclError:
            pass
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        if top.winfo_viewable():
            x = top.winfo_rootx() + (top.winfo_width() - width) // 2
            y = top.winfo_rooty() + max(0, (top.winfo_height() - height) * 2 // 5)
        else:
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 3
        x, y = place_popup(top, width, height, x, y)
        self.geometry("+%d+%d" % (x, y))
        self.deiconify()
        self.lift()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        for hook in list(Dialog.created_hooks):          # the window exists now: title bar, icon
            try:
                hook(self)
            except Exception:
                pass
        focus = self.buttons.get(self.default)
        if focus is not None:
            focus.focus_set()

    def invoke(self, key: str) -> None:
        """Press a button from the program (tests)."""
        self.buttons[key].invoke()

    # -- internal
    def _chosen(self, key: str) -> None:
        self.result = key
        self._close()

    def _cancelled(self) -> None:
        self.result = self.cancel
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        parent = self.parent
        self.destroy()
        try:
            if _alive(parent):
                parent.winfo_toplevel().focus_set()
        except tk.TclError:
            pass

    def _enter(self, _event: Any) -> str:
        focused = self.focus_get()
        for key, made in self.buttons.items():
            if made is focused:
                self._chosen(key)
                return "break"
        if isinstance(focused, ttk.Button):          # the details header: let it toggle
            return ""
        self._chosen(self.default)
        return "break"

    def _step(self, direction: int) -> str:
        focused = self.focus_get()
        keys = self._order
        for index, key in enumerate(keys):
            if self.buttons[key] is focused:
                self.buttons[keys[(index + direction) % len(keys)]].focus_set()
                break
        return "break"
