# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The look of the window: palette, scaling, fonts and the ttk style set.

House style: calm and light, one restrained blue accent, white cards on a slightly
darker page ground, generous even spacing, hairline borders, no 3-D edges, no system grey.

How to use it::

    root = tk.Tk()
    look = theme.init(root, is_dark=os_says_dark, is_high_contrast=os_says_high_contrast)
    ttk.Button(parent, text="Browse...", style=look.style_name("TButton", "page"))
    gap = look.px(8)

``init`` is called once, before any widget is made. The palette is chosen there and never
changes while the program runs. The caller finds out about dark mode and high contrast
(from the operating system) and passes them in; this module asks nobody.

**Grounds.** A ttk widget paints its own background, so it must know what it stands on.
Style names without a prefix are for widgets on a white card (ground ``"surface"``).
For another ground put its prefix first: ``OnPage.`` (the page ground), ``OnSunk.``,
``OnAlt.``, ``OnInfo.``, ``OnOk.``, ``OnWarn.``, ``OnBad.``. :meth:`Theme.style_name` builds
the name; the widgets of ``widgets.py`` work it out by themselves.

**Scaling.** Fonts are in points and follow the screen by themselves. Every pixel number
(padding, widths, wrap lengths, row heights, picture sizes) goes through :meth:`Theme.px`.

**High contrast.** With ``is_high_contrast`` the native ttk theme and the system colours
are used and nothing is drawn by hand, so Windows' own contrast themes stay in charge.

Standard library only. Depends on ``icons`` (pictures) and on nothing else of the program.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import icons as _icons

__all__ = [
    "Palette", "LIGHT", "DARK", "Theme", "init", "current", "px",
    "contrast", "relative_luminance", "pick_family", "GROUNDS",
    "TEXT_FAMILIES", "TITLE_FAMILIES", "MONO_FAMILIES", "FONT_SIZES",
]


# --------------------------------------------------------------------------- palettes

@dataclass(frozen=True)
class Palette:
    """The colours of one mode. All values are ``#rrggbb``."""
    name: str
    bg: str               # the page ground
    surface: str          # cards
    surface_alt: str      # a darker plate: troughs, hover on headings
    surface_sunk: str     # a slightly sunk plate: table headings, wells, disabled fields
    border: str           # hairlines
    border_strong: str    # the outline of controls
    text: str
    text_soft: str        # secondary text
    muted: str            # tertiary text, hints
    accent: str
    accent_hover: str
    accent_active: str
    accent_soft: str      # selected rows, hover on plain buttons
    on_accent: str        # text on the accent
    ok: str
    warn: str
    bad: str
    ok_soft: str
    warn_soft: str
    bad_soft: str
    focus: str            # the keyboard focus ring
    row_alt: str          # every second table row

    def as_dict(self) -> Dict[str, str]:
        d = asdict(self)
        d.pop("name")
        return d

    def __getitem__(self, key: str) -> str:
        return getattr(self, key)


LIGHT = Palette(
    name="light", bg="#ecf0f6", surface="#ffffff", surface_alt="#e3e8f0", surface_sunk="#f6f8fc",
    border="#c9d2e0", border_strong="#a5b1c4", text="#121820", text_soft="#3d4754", muted="#5b6573",
    accent="#2563eb", accent_hover="#3b76ee", accent_active="#1d4ed8", accent_soft="#e9f0fe",
    on_accent="#ffffff", ok="#0f7032", warn="#8a5300", bad="#b42318", ok_soft="#e7f5ec",
    warn_soft="#fdf3e2", bad_soft="#fdeceb", focus="#1d4ed8", row_alt="#f6f8fc")

DARK = Palette(
    name="dark", bg="#111419", surface="#191d24", surface_alt="#232a36", surface_sunk="#14181e",
    border="#38424f", border_strong="#4d5868", text="#e9edf4", text_soft="#c3cbd7", muted="#98a3b3",
    accent="#6ea0ff", accent_hover="#8db4ff", accent_active="#4d87f5", accent_soft="#1d2836",
    on_accent="#0b1220", ok="#4ade80", warn="#fbbf24", bad="#fb7185", ok_soft="#14261c",
    warn_soft="#2a2113", bad_soft="#2c1619", focus="#8db4ff", row_alt="#1d222a")

#: The grounds a widget can stand on, with the style-name prefix of each.
GROUNDS: Dict[str, str] = {
    "surface": "", "page": "OnPage.", "sunk": "OnSunk.", "alt": "OnAlt.",
    "info": "OnInfo.", "ok": "OnOk.", "warn": "OnWarn.", "bad": "OnBad.",
}
_GROUND_COLOUR = {"surface": "surface", "page": "bg", "sunk": "surface_sunk", "alt": "surface_alt",
                  "info": "accent_soft", "ok": "ok_soft", "warn": "warn_soft", "bad": "bad_soft"}


def relative_luminance(colour: str) -> float:
    """WCAG relative luminance of ``#rrggbb``, 0 (black) to 1 (white)."""
    channels = []
    for value in _icons.rgb(colour):
        c = value / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(first: str, second: str) -> float:
    """WCAG contrast ratio of two colours, 1 to 21. Body text needs 4.5 or more."""
    a, b = relative_luminance(first), relative_luminance(second)
    if a < b:
        a, b = b, a
    return (a + 0.05) / (b + 0.05)


# --------------------------------------------------------------------------- fonts

TEXT_FAMILIES: Tuple[str, ...] = ("Segoe UI Variable Text", "Segoe UI", "Noto Sans", "DejaVu Sans")
#: For titles. A family that is itself semibold is used at normal weight, the others in bold.
TITLE_FAMILIES: Tuple[str, ...] = ("Segoe UI Variable Display", "Segoe UI Semibold") + TEXT_FAMILIES[1:]
#: For the 11 point headings: semibold where the PC has it, otherwise the text family in bold.
HEADING_FAMILIES: Tuple[str, ...] = ("Segoe UI Semibold",) + TEXT_FAMILIES
MONO_FAMILIES: Tuple[str, ...] = ("Cascadia Mono", "Consolas", "DejaVu Sans Mono")
#: Sizes in points.
FONT_SIZES: Dict[str, int] = {"small": 9, "body": 10, "heading": 11, "title": 16, "mono": 9}


def pick_family(available: Iterable[str], wanted: Sequence[str], fallback: str = "") -> str:
    """The first of ``wanted`` that the PC has (capitals do not matter), else ``fallback``."""
    have = {name.casefold(): name for name in available}
    for name in wanted:
        found = have.get(name.casefold())
        if found:
            return found
    return fallback


def _is_semibold(family: str) -> bool:
    lowered = family.casefold()
    return "semibold" in lowered or lowered.endswith("semib")


# --------------------------------------------------------------------------- the theme

class Theme:
    """Palette, scale, fonts, pictures and styles of one Tk root. Made by :func:`init`."""

    THEME_NAME = "tesseract-win"

    def __init__(self, root: Any, *, is_dark: bool = False, is_high_contrast: bool = False,
                 scale: Optional[float] = None):
        from tkinter import ttk

        self.root = root
        self.dark = bool(is_dark)
        self.high_contrast = bool(is_high_contrast)
        if scale is not None:
            # A scale forced by the caller (the gallery, tests): make points follow it too.
            root.tk.call("tk", "scaling", float(scale) * 96.0 / 72.0)
            self.scale = float(scale)
        else:
            self.scale = self._detect_scale(root)
        self.style = ttk.Style(root)
        if self.high_contrast:
            self._use_native_theme()
            self.palette = self._system_palette()
        else:
            self.palette = DARK if self.dark else LIGHT
        self.icons = _icons.IconSet(root, self.palette.as_dict(), self.scale)
        self.hair = self.icons.hair
        p = self.palette
        #: text of anything disabled: weaker than ``muted`` but still readable
        self.disabled_fg = p.muted if self.high_contrast else _icons.mix(p.muted, p.surface, 0.42)
        #: selected table rows
        self.select_bg = p.accent if self.high_contrast else p.accent_soft
        self.select_fg = p.on_accent if self.high_contrast else p.text
        #: plain and tinted table rows (what lies behind the text of a cell)
        self.table_bg = self._native_colour("Treeview", "fieldbackground", p.surface) if self.high_contrast else p.surface
        self.table_alt = self.table_bg if self.high_contrast else p.row_alt
        self.fonts: Dict[str, Any] = {}
        self._make_fonts()
        #: height of a table row: one line of body text plus even padding
        self.row_height = int(self.fonts["body"].metrics("linespace")) + self.px(10)
        self.control_height = self.row_height
        self._grounded: List[str] = []
        if self.high_contrast:
            self._build_native()
        else:
            self._build()
        self._plain_tk_defaults()

    # ----------------------------------------------------------------- numbers and names
    @staticmethod
    def _detect_scale(root: Any) -> float:
        """Screen scaling from the root window: 1.0 at 96 dpi, 1.5 at 150 %."""
        try:
            value = float(root.winfo_fpixels("1i")) / 96.0
        except Exception:
            return 1.0
        return min(4.0, max(1.0, value))

    def px(self, value: float) -> int:
        """Pixels on this screen for a number meant for a 96 dpi screen."""
        if value == 0:
            return 0
        scaled = int(round(abs(value) * self.scale))
        return max(1, scaled) if value > 0 else -max(1, scaled)

    def font(self, name: str) -> Any:
        """A named font: ``small``, ``body``, ``bold``, ``heading``, ``title``, ``mono``,
        ``link`` (body, underlined), ``small_bold``."""
        return self.fonts[name]

    def colour(self, name: str) -> str:
        return getattr(self.palette, name)

    def ground_colour(self, ground: str = "surface") -> str:
        return getattr(self.palette, _GROUND_COLOUR[ground])

    def style_name(self, base: str, ground: str = "surface") -> str:
        """The style for ``base`` (for example ``"Muted.TLabel"``) on ``ground``."""
        return GROUNDS[ground] + base

    def text_width(self, text: str, font: str = "body") -> int:
        return int(self.fonts[font].measure(text))

    # ----------------------------------------------------------------- palette of the system
    def _use_native_theme(self) -> None:
        for name in ("vista", "xpnative", "winnative"):
            if name in self.style.theme_names():
                self.style.theme_use(name)
                return

    def _hex(self, colour: Any, fallback: str) -> str:
        """Any colour Tk knows (a name, a system colour) as ``#rrggbb``."""
        try:
            r, g, b = self.root.winfo_rgb(str(colour))
            return "#%02x%02x%02x" % (r >> 8, g >> 8, b >> 8)
        except Exception:
            return fallback

    def _native_colour(self, style: str, option: str, fallback: str, state: Sequence[str] = ()) -> str:
        try:
            found = self.style.lookup(style, option, list(state)) if state else self.style.lookup(style, option)
        except Exception:
            found = ""
        return self._hex(found, fallback) if found else fallback

    def _system_palette(self) -> Palette:
        """High contrast: the colours come from the native theme and the operating system,
        so that hand-drawn parts (the stepper, tick boxes, status words) match the rest."""
        dark = self.dark
        window = self._hex("SystemWindow", "#000000" if dark else "#ffffff")
        ink = self._hex("SystemWindowText", "#ffffff" if dark else "#000000")
        frame = self._native_colour("TFrame", "background", window)
        ink = self._native_colour("TLabel", "foreground", ink)
        if contrast(ink, frame) < 4.5:                 # never trust a pair that cannot be read
            ink = "#ffffff" if relative_luminance(frame) < 0.2 else "#000000"
        mark = self._native_colour("Treeview", "background", self._hex("SystemHighlight", "#0a3d91"), ["selected"])
        on_mark = self._native_colour("Treeview", "foreground", self._hex("SystemHighlightText", "#ffffff"), ["selected"])
        if contrast(mark, frame) < 3.0:
            mark = ink
            on_mark = frame
        grey = self._hex("SystemGrayText", _icons.mix(ink, frame, 0.35))
        if contrast(grey, frame) < 4.5:
            grey = ink
        return Palette(
            name="high-contrast", bg=frame, surface=frame, surface_alt=frame, surface_sunk=frame,
            border=ink, border_strong=ink, text=ink, text_soft=ink, muted=grey,
            accent=mark, accent_hover=mark, accent_active=mark, accent_soft=frame, on_accent=on_mark,
            ok=ink, warn=ink, bad=ink, ok_soft=frame, warn_soft=frame, bad_soft=frame,
            focus=mark, row_alt=frame)

    # ----------------------------------------------------------------- fonts
    def _make_fonts(self) -> None:
        from tkinter import font as tkfont

        root = self.root
        try:
            available = list(tkfont.families(root))
        except Exception:
            available = []
        default = tkfont.nametofont("TkDefaultFont", root=root)
        fixed = tkfont.nametofont("TkFixedFont", root=root)
        text_family = pick_family(available, TEXT_FAMILIES, default.actual("family"))
        title_family = pick_family(available, TITLE_FAMILIES, text_family)
        heading_family = pick_family(available, HEADING_FAMILIES, text_family)
        mono_family = pick_family(available, MONO_FAMILIES, fixed.actual("family"))
        self.families = {"text": text_family, "title": title_family, "heading": heading_family,
                         "mono": mono_family}

        def weight(family: str) -> str:
            return "normal" if _is_semibold(family) else "bold"

        specs = {
            "small": dict(family=text_family, size=FONT_SIZES["small"]),
            "body": dict(family=text_family, size=FONT_SIZES["body"]),
            "bold": dict(family=heading_family, size=FONT_SIZES["body"], weight=weight(heading_family)),
            "small_bold": dict(family=heading_family, size=FONT_SIZES["small"], weight=weight(heading_family)),
            "heading": dict(family=heading_family, size=FONT_SIZES["heading"], weight=weight(heading_family)),
            "title": dict(family=title_family, size=FONT_SIZES["title"], weight=weight(title_family)),
            "mono": dict(family=mono_family, size=FONT_SIZES["mono"]),
            "link": dict(family=text_family, size=FONT_SIZES["body"], underline=True),
            "small_link": dict(family=text_family, size=FONT_SIZES["small"], underline=True),
        }
        existing = set(tkfont.names(root))
        for key, spec in specs.items():
            name = "TessWin" + "".join(part.capitalize() for part in key.split("_"))
            if name in existing:
                made = tkfont.Font(root=root, name=name, exists=True)
                made.configure(**spec)
            else:
                made = tkfont.Font(root=root, name=name, **spec)
            self.fonts[key] = made
        # Tk's own named fonts, so that anything not styled by hand still matches.
        for name, key in (("TkDefaultFont", "body"), ("TkTextFont", "body"), ("TkMenuFont", "body"),
                          ("TkHeadingFont", "bold"), ("TkCaptionFont", "heading"),
                          ("TkSmallCaptionFont", "small"), ("TkIconFont", "body"),
                          ("TkTooltipFont", "small"), ("TkFixedFont", "mono")):
            try:
                tkfont.nametofont(name, root=root).configure(**self.fonts[key].actual())
            except Exception:
                pass

    # ----------------------------------------------------------------- style helpers
    def _set(self, name: str, grounded: bool = True, **options: Any) -> None:
        """Configure a style; a foreground also gets its disabled colour."""
        self.style.configure(name, **options)
        if "foreground" in options:
            self.style.map(name, foreground=[("disabled", self.disabled_fg)])
        if grounded and name not in self._grounded:
            self._grounded.append(name)

    def _element(self, name: str, default: Any, states: Sequence[Tuple[Any, ...]] = (), **options: Any) -> None:
        """An element drawn from pictures; ``states`` are ``(state, ..., picture)`` in order."""
        if name in self.style.element_names():
            return
        # ttk pads the inside of a picture element by its border unless told otherwise;
        # all spacing here comes from the style's own padding instead
        options.setdefault("padding", 0)
        # ... and never lets an element get smaller than its picture, whose middle is
        # drawn wide on purpose: the true minimum is the two unstretched borders
        border = options.get("border")
        if border is not None and "width" not in options and "height" not in options:
            if isinstance(border, int):
                left = top = right = bottom = border
            elif len(border) == 2:
                left, top = border
                right, bottom = border
            else:
                left, top, right, bottom = border
            options["width"] = left + right + 1
            options["height"] = top + bottom + 1
        self.style.element_create(name, "image", default, *states, **options)

    @staticmethod
    def _wrap(element: str, *inner: str, **extra: Any) -> List[Any]:
        """A layout: ``element`` behind, then ``inner`` nested one in the other."""
        node: Optional[Tuple[str, Dict[str, Any]]] = None
        for name in reversed(inner):
            options: Dict[str, Any] = {"sticky": "nsew"}
            if node is not None:
                options["children"] = [node]
            node = (name, options)
        top: Dict[str, Any] = {"sticky": "nsew"}
        top.update(extra)
        if node is not None:
            top["children"] = [node]
        return [(element, top)]

    # ----------------------------------------------------------------- the style set
    def _build(self) -> None:
        st, p, ic, px = self.style, self.palette, self.icons, self.px
        mix = _icons.mix
        f = self.fonts
        dis = self.disabled_fg

        if self.THEME_NAME not in st.theme_names():
            st.theme_create(self.THEME_NAME, parent="clam")
        st.theme_use(self.THEME_NAME)

        st.configure(".", background=p.surface, foreground=p.text, bordercolor=p.border,
                     darkcolor=p.surface, lightcolor=p.surface, troughcolor=p.surface_alt,
                     selectbackground=p.accent, selectforeground=p.on_accent,
                     fieldbackground=p.surface, insertcolor=p.text, focuscolor=p.focus,
                     arrowcolor=p.text_soft, font=f["body"], relief="flat")
        st.map(".", foreground=[("disabled", dis)])

        # ---- frames
        big = 256                    # repeated middle of the large plates, in pixels
        self._set("TFrame", background=p.surface)
        for name, fill, edge, radius in (("Card.TFrame", "surface", "border", 8),
                                         ("Well.TFrame", "surface_sunk", "border", 6)):
            element = "Tw.%s.bg" % name.split(".")[0]
            self._element(element, ic.patch(fill=fill, outline=edge, radius=radius, middle=big),
                          border=ic.patch_border(radius), sticky="nsew")
            st.layout(name, [(element, {"sticky": "nsew"})])
        # what shows in the corners of a card is the ground it lies on: the page, as a rule
        self._set("Card.TFrame", grounded=False, background=p.bg)
        self._set("Well.TFrame", background=p.surface)
        self._element("Tw.Box.bg", ic.patch(fill="surface", outline="border", radius=0, middle=big),
                      [("focus", ic.patch(fill="surface", outline="focus", radius=0, middle=big))],
                      border=ic.patch_border(0), sticky="nsew")
        st.layout("Box.TFrame", [("Tw.Box.bg", {"sticky": "nsew"})])
        self._set("Box.TFrame", background=p.surface)
        for level, colour, soft in (("Info", p.accent, p.accent_soft), ("Ok", p.ok, p.ok_soft),
                                    ("Warn", p.warn, p.warn_soft), ("Bad", p.bad, p.bad_soft)):
            element = "Tw.Notice.%s" % level
            self._element(element, ic.patch(fill=soft, outline=mix(soft, colour, 0.38), radius=6, middle=big),
                          border=ic.patch_border(6), sticky="nsew")
            st.layout("%s.Notice.TFrame" % level, [(element, {"sticky": "nsew"})])
            self._set("%s.Notice.TFrame" % level, grounded=False, background=p.bg)
        self._element("Tw.Hairline", ic.patch(fill="border", radius=0, middle=big), sticky="nsew",
                      width=self.hair, height=self.hair)
        st.layout("TSeparator", [("Tw.Hairline", {"sticky": "nsew"})])
        self._set("TSeparator", background=p.border)

        # ---- labels (text starts exactly at the edge of the label: no hidden inset)
        st.layout("TLabel", [("Label.padding", {"sticky": "nsew", "children": [
            ("Label.label", {"sticky": "nsew"})]})])
        self._set("TLabel", background=p.surface, foreground=p.text, font=f["body"])
        self._set("Soft.TLabel", foreground=p.text_soft)
        self._set("Muted.TLabel", foreground=p.muted)
        self._set("Small.TLabel", foreground=p.muted, font=f["small"])
        self._set("SmallSoft.TLabel", foreground=p.text_soft, font=f["small"])
        self._set("Bold.TLabel", foreground=p.text, font=f["bold"])
        self._set("Heading.TLabel", foreground=p.text, font=f["heading"])
        self._set("Title.TLabel", foreground=p.text, font=f["title"])
        self._set("Mono.TLabel", foreground=p.text_soft, font=f["mono"])
        for word in ("accent", "ok", "warn", "bad"):
            self._set("%s.TLabel" % word.capitalize(), foreground=getattr(p, word))
            self._set("Small%s.TLabel" % word.capitalize(), foreground=getattr(p, word), font=f["small"])

        # ---- buttons
        radius = 5
        edge = ic.patch_border(radius)
        hover_fill = p.accent_soft
        press_fill = mix(p.accent_soft, p.accent, 0.16)
        off = ic.patch(fill="surface_sunk", outline="border", radius=radius)

        def button(element: str, normal: Any, hover: Any, pressed: Any, focus: Any, focus_hover: Any,
                   disabled: Any) -> None:
            self._element(element, normal, [("disabled", disabled), ("pressed", pressed),
                                            ("focus", "active", focus_hover), ("focus", focus),
                                            ("active", hover)], border=edge, sticky="nsew")

        button("Tw.Button.bg",
               ic.patch(fill="surface", outline="border_strong", radius=radius),
               ic.patch(fill=hover_fill, outline="accent", radius=radius),
               ic.patch(fill=press_fill, outline="accent_active", radius=radius),
               ic.patch(fill="surface", outline="focus", width=2, radius=radius),
               ic.patch(fill=hover_fill, outline="focus", width=2, radius=radius), off)
        button("Tw.Primary.bg",
               ic.patch(fill="accent", outline="accent", radius=radius),
               ic.patch(fill="accent_hover", outline="accent_hover", radius=radius),
               ic.patch(fill="accent_active", outline="accent_active", radius=radius),
               ic.patch(fill="accent", outline="focus", width=2, inner="on_accent", radius=radius),
               ic.patch(fill="accent_hover", outline="focus", width=2, inner="on_accent", radius=radius),
               ic.patch(fill="surface_alt", outline="border", radius=radius))
        danger_edge = mix(p.bad, p.surface, 0.45)
        button("Tw.Danger.bg",
               ic.patch(fill="surface", outline=danger_edge, radius=radius),
               ic.patch(fill="bad_soft", outline="bad", radius=radius),
               ic.patch(fill=mix(p.bad_soft, p.bad, 0.18), outline="bad", radius=radius),
               ic.patch(fill="surface", outline="focus", width=2, radius=radius),
               ic.patch(fill="bad_soft", outline="focus", width=2, radius=radius), off)
        button("Tw.Default.bg",
               ic.patch(fill="accent_soft", outline="accent", radius=radius),
               ic.patch(fill=mix(p.accent_soft, p.accent, 0.14), outline="accent", radius=radius),
               ic.patch(fill=mix(p.accent_soft, p.accent, 0.26), outline="accent_active", radius=radius),
               ic.patch(fill="accent_soft", outline="focus", width=2, radius=radius),
               ic.patch(fill=mix(p.accent_soft, p.accent, 0.14), outline="focus", width=2, radius=radius), off)
        clear = ic.patch(radius=radius)
        ring = ic.patch(outline="focus", width=2, radius=radius)
        button("Tw.Tool.bg", clear,
               ic.patch(fill="text", opacity=0.07, radius=radius),
               ic.patch(fill="text", opacity=0.13, radius=radius),
               ring, ic.patch(fill="text", opacity=0.07, outline="focus", width=2, radius=radius), clear)
        self._element("Tw.Link.bg", ic.patch(radius=3), [("focus", ic.patch(outline="focus", width=2, radius=3))],
                      border=ic.patch_border(3), sticky="nsew")

        label_in_padding = ("Button.padding", "Button.label")
        st.layout("TButton", self._wrap("Tw.Button.bg", *label_in_padding))
        st.layout("Primary.TButton", self._wrap("Tw.Primary.bg", *label_in_padding))
        st.layout("Danger.TButton", self._wrap("Tw.Danger.bg", *label_in_padding))
        st.layout("Default.TButton", self._wrap("Tw.Default.bg", *label_in_padding))
        st.layout("Tool.TButton", self._wrap("Tw.Tool.bg", *label_in_padding))
        st.layout("Disclosure.TButton", self._wrap("Tw.Tool.bg", *label_in_padding))
        st.layout("Link.TButton", self._wrap("Tw.Link.bg", *label_in_padding))
        self._set("TButton", background=p.surface, foreground=p.text, font=f["body"],
                  padding=(px(14), px(6)), anchor="center", width=-8)
        self._set("Primary.TButton", foreground=p.on_accent, font=f["heading"], padding=(px(22), px(8)))
        self._set("Danger.TButton", foreground=p.bad)
        self._set("Default.TButton", foreground=p.accent if self.dark else p.accent_active)
        self._set("Tool.TButton", foreground=p.text_soft, padding=(px(8), px(5)), width=0)
        self._set("Disclosure.TButton", foreground=p.text, font=f["bold"], padding=(px(6), px(5)),
                  anchor="w", width=0)
        self._set("Link.TButton", foreground=p.accent, padding=(px(3), px(2)), anchor="w", width=0)
        st.map("Link.TButton",
               foreground=[("disabled", dis), ("pressed", p.accent_active), ("active", p.accent_hover)],
               font=[("active", f["link"])])
        self._set("Small.Link.TButton", foreground=p.accent, font=f["small"])
        st.map("Small.Link.TButton",
               foreground=[("disabled", dis), ("pressed", p.accent_active), ("active", p.accent_hover)],
               font=[("active", f["small_link"])])

        # ---- tick boxes and radio buttons
        gap = px(7)
        for element, picture in (("Tw.Check.indicator", ic.checkbox), ("Tw.Radio.indicator", ic.radio)):
            states: List[Tuple[Any, ...]] = [
                ("disabled", "selected", picture(True, disabled=True)),
                ("disabled", picture(False, disabled=True)),
            ]
            for focus in (True, False):
                lead = ("focus",) if focus else ()
                states += [
                    lead + ("selected", "pressed", picture(True, pressed=True, focus=focus)),
                    lead + ("selected", "active", picture(True, hover=True, focus=focus)),
                    lead + ("selected", picture(True, focus=focus)),
                    lead + ("pressed", picture(False, pressed=True, focus=focus)),
                    lead + ("active", picture(False, hover=True, focus=focus)),
                ]
                if focus:
                    states.append(("focus", picture(False, focus=True)))
            self._element(element, picture(False), states, width=ic.box_size + gap, sticky="w")
        for name, element, part in (("TCheckbutton", "Tw.Check.indicator", "Checkbutton"),
                                    ("TRadiobutton", "Tw.Radio.indicator", "Radiobutton")):
            st.layout(name, [("%s.padding" % part, {"sticky": "nsew", "children": [
                (element, {"side": "left", "sticky": ""}),
                ("%s.label" % part, {"side": "left", "sticky": "nsew"})]})])
            self._set(name, background=p.surface, foreground=p.text, font=f["body"], padding=(0, px(2)))

        # ---- text fields
        field_edge = ic.patch_border(radius)
        self._element("Tw.Entry.field", ic.patch(fill="surface", outline="border_strong", radius=radius), [
            ("disabled", ic.patch(fill="surface_sunk", outline="border", radius=radius)),
            ("invalid", ic.patch(fill="surface", outline="bad", width=2, radius=radius)),
            ("focus", ic.patch(fill="surface", outline="accent", width=2, radius=radius)),
            ("readonly", ic.patch(fill="surface_sunk", outline="border_strong", radius=radius)),
            ("hover", ic.patch(fill="surface", outline=mix(p.border_strong, p.text, 0.35), radius=radius)),
        ], border=field_edge, sticky="nsew")
        st.layout("TEntry", self._wrap("Tw.Entry.field", "Entry.padding", "Entry.textarea"))
        self._set("TEntry", background=p.surface, foreground=p.text, insertcolor=p.text,
                  insertwidth=max(1, self.hair), padding=(px(8), px(6)),
                  selectbackground=p.accent, selectforeground=p.on_accent)
        st.map("TEntry", selectbackground=[("!focus", p.surface_alt)], selectforeground=[("!focus", p.text)])
        # the field laid over a table cell while a name is edited: no room for padding
        self._set("Cell.TEntry", padding=(max(1, px(8) - 1), px(1)))
        for name in ("TCombobox", "TSpinbox"):
            self._set(name, background=p.surface, foreground=p.text, fieldbackground=p.surface,
                      bordercolor=p.border_strong, lightcolor=p.surface, darkcolor=p.surface,
                      arrowcolor=p.text_soft, arrowsize=px(12), insertcolor=p.text,
                      padding=(px(6), px(4)), selectbackground=p.accent, selectforeground=p.on_accent)
            st.map(name, fieldbackground=[("disabled", p.surface_sunk), ("readonly", p.surface)],
                   bordercolor=[("focus", p.accent)], background=[("active", p.surface_alt)],
                   arrowcolor=[("disabled", dis)],
                   selectbackground=[("readonly", "!focus", p.surface)],
                   selectforeground=[("readonly", "!focus", p.text)])

        # ---- tables
        self._element("Tw.Heading.bg", ic.heading("surface_sunk"), [
            ("pressed", ic.heading("surface_alt")), ("active", ic.heading("surface_alt"))],
            border=ic.heading_border(), sticky="nsew")
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nsew"})])
        st.layout("Treeview.Heading", [
            ("Tw.Heading.bg", {"sticky": "nsew"}),
            ("Treeheading.padding", {"sticky": "nsew", "children": [
                ("Treeheading.image", {"side": "right", "sticky": ""}),
                ("Treeheading.text", {"sticky": "we"})]})])
        # no dotted focus rectangle inside the rows: the selected row is the mark
        st.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nsew", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])
        self._set("Treeview", grounded=False, background=p.surface, fieldbackground=p.surface,
                  foreground=p.text, font=f["body"], rowheight=self.row_height, borderwidth=0, indent=0)
        st.map("Treeview", background=[("selected", self.select_bg)],
               foreground=[("disabled", dis), ("selected", self.select_fg)])
        self._set("Treeview.Heading", grounded=False, background=p.surface_sunk, foreground=p.text_soft,
                  font=f["small_bold"], padding=(px(8), px(7)))
        # Tk puts 4 unscaled pixels beside every cell; add the rest so cells line up with headings
        st.configure("Treeview.Cell", padding=(max(0, px(8) - 4), 0))

        # ---- scroll bars: a thin rounded thumb, no arrows, no trough
        for side, vertical in (("Vertical", True), ("Horizontal", False)):
            element = "Tw.%s.Scrollbar.thumb" % side
            cap = px(6)
            self._element(element, ic.pill("border_strong", vertical=vertical), [
                ("disabled", ic.pill("border_strong", vertical=vertical, opacity=0.0)),
                ("pressed", ic.pill("text_soft", vertical=vertical)),
                ("active", ic.pill("muted", vertical=vertical))],
                border=(0, cap, 0, cap) if vertical else (cap, 0, cap, 0), sticky="nsew",
                width=px(12) if vertical else px(28), height=px(28) if vertical else px(12))
            st.layout("%s.TScrollbar" % side, [("%s.Scrollbar.trough" % side, {
                "sticky": "ns" if vertical else "ew",
                "children": [(element, {"expand": "1", "sticky": "nsew"})]})])
        self._set("TScrollbar", background=p.surface, troughcolor=p.surface, bordercolor=p.surface,
                  lightcolor=p.surface, darkcolor=p.surface, borderwidth=0, arrowsize=px(12))
        self._set("Vertical.TScrollbar", background=p.surface, troughcolor=p.surface, bordercolor=p.surface,
                  lightcolor=p.surface, darkcolor=p.surface)
        self._set("Horizontal.TScrollbar", background=p.surface, troughcolor=p.surface, bordercolor=p.surface,
                  lightcolor=p.surface, darkcolor=p.surface)

        # ---- progress bar
        thick = 6
        cap = px(thick) // 2 + 1
        self._element("Tw.Progress.trough", ic.bar("surface_alt", thick), border=(cap, 0), sticky="ew",
                      width=px(40), height=px(thick))
        self._element("Tw.Progress.pbar", ic.bar("accent", thick), border=(cap, 0), sticky="ew",
                      width=px(thick), height=px(thick))
        st.layout("Horizontal.TProgressbar", [("Tw.Progress.trough", {"sticky": "ew", "children": [
            ("Tw.Progress.pbar", {"side": "left", "sticky": "ew"})]})])
        self._set("Horizontal.TProgressbar", background=p.surface)
        self._set("TProgressbar", background=p.surface)

        # ---- chips and badges
        chip_radius = 11
        chips = (("Waiting", "surface_sunk", p.border, p.muted),
                 ("Running", "accent_soft", mix(p.accent_soft, p.accent, 0.55), p.accent if self.dark else p.accent_active),
                 ("Done", "ok_soft", mix(p.ok_soft, p.ok, 0.45), p.ok),
                 ("Skipped", "surface", p.border, dis),
                 ("Failed", "bad_soft", mix(p.bad_soft, p.bad, 0.45), p.bad))
        for word, fill, line, ink in chips:
            element = "Tw.Chip.%s" % word
            self._element(element, ic.patch(fill=fill, outline=line, radius=chip_radius),
                          border=ic.patch_border(chip_radius), sticky="nsew")
            st.layout("%s.Chip.TLabel" % word, self._wrap(element, "Label.padding", "Label.label"))
            self.style.configure("%s.Chip.TLabel" % word, background=p.surface, foreground=ink,
                                 font=f["small"], padding=(px(10), px(3), px(12), px(3)))
            self._grounded.append("%s.Chip.TLabel" % word)
        self._element("Tw.Badge.bg", ic.patch(fill="accent_soft", outline=mix(p.accent_soft, p.accent, 0.45), radius=9),
                      border=ic.patch_border(9), sticky="nsew")
        st.layout("Badge.TLabel", self._wrap("Tw.Badge.bg", "Label.padding", "Label.label"))
        self._set("Badge.TLabel", background=p.surface, foreground=p.accent if self.dark else p.accent_active,
                  font=f["small"], padding=(px(8), px(1)))

        # ---- classes the program hardly uses, coloured so that none shows a wrong patch
        self._set("TLabelframe", background=p.surface, bordercolor=p.border, lightcolor=p.surface,
                  darkcolor=p.surface, borderwidth=1, relief="solid")
        self._set("TLabelframe.Label", background=p.surface, foreground=p.text_soft, font=f["bold"])
        self._set("TNotebook", background=p.surface, bordercolor=p.border, lightcolor=p.surface,
                  darkcolor=p.surface, tabmargins=(0, 0, 0, 0))
        self._set("TNotebook.Tab", grounded=False, background=p.surface_alt, foreground=p.text_soft,
                  bordercolor=p.border, lightcolor=p.surface_alt, darkcolor=p.surface_alt,
                  padding=(px(12), px(5)))
        st.map("TNotebook.Tab", background=[("selected", p.surface)],
               foreground=[("disabled", dis), ("selected", p.text)],
               lightcolor=[("selected", p.surface)], darkcolor=[("selected", p.surface)])
        self._set("TPanedwindow", background=p.surface)
        self._set("Sash", grounded=False, background=p.surface, bordercolor=p.border,
                  lightcolor=p.surface, darkcolor=p.surface, sashthickness=px(6), gripcount=0)
        self._set("TScale", background=p.surface, troughcolor=p.surface_alt, bordercolor=p.border,
                  lightcolor=p.surface, darkcolor=p.surface)
        self._set("TMenubutton", background=p.surface, foreground=p.text, bordercolor=p.border_strong,
                  lightcolor=p.surface, darkcolor=p.surface, arrowcolor=p.text_soft,
                  padding=(px(12), px(5)), relief="flat")
        st.map("TMenubutton", background=[("active", p.accent_soft)], bordercolor=[("focus", p.focus)])
        self._set("TSizegrip", background=p.surface)

        self._match_heights()
        self._twin_grounds()

    def _match_heights(self) -> None:
        """Make a plain button exactly as tall as a text field, whatever the font and scale,
        so that a field and the button beside it ("Browse...") line up."""
        from tkinter import ttk
        probe_field = ttk.Entry(self.root)
        probe_button = ttk.Button(self.root, text="Ag")
        try:
            field_height = probe_field.winfo_reqheight()
            extra = field_height - probe_button.winfo_reqheight()
            side, top = self.px(14), self.px(6)
            if extra:
                top_pad = max(1, top + extra // 2)
                bottom_pad = max(1, top + extra - extra // 2)
                self.style.configure("TButton", padding=(side, top_pad, side, bottom_pad))
            #: height in pixels shared by text fields and plain buttons
            self.control_height = field_height
        finally:
            probe_field.destroy()
            probe_button.destroy()

    def _twin_grounds(self) -> None:
        """Give every style a twin per ground, differing only in what lies behind it."""
        for ground, prefix in GROUNDS.items():
            colour = self.ground_colour(ground)
            for name in list(self._grounded):
                if prefix:
                    self.style.configure(prefix + name, background=colour)
            # frames with a drawn plate: the plain name stands on the page, the twins elsewhere
            for name in ("Card.TFrame", "Info.Notice.TFrame", "Ok.Notice.TFrame",
                         "Warn.Notice.TFrame", "Bad.Notice.TFrame"):
                self.style.configure((prefix or "OnSurface.") + name, background=colour)
        self.style.configure("OnPage.TFrame", background=self.palette.bg)

    def _build_native(self) -> None:
        """High contrast: the native theme, system colours, our fonts and paddings only."""
        st, p, px, f = self.style, self.palette, self.px, self.fonts
        st.configure(".", font=f["body"])
        st.configure("TButton", padding=(px(12), px(5)))
        st.configure("Primary.TButton", font=f["heading"], padding=(px(18), px(7)))
        st.configure("Link.TButton", foreground=p.accent, padding=(px(3), px(2)))
        st.configure("Small.Link.TButton", font=f["small"])
        st.configure("Disclosure.TButton", font=f["bold"], anchor="w")
        st.configure("TEntry", padding=(px(6), px(4)))
        st.configure("Treeview", rowheight=self.row_height, font=f["body"])
        st.configure("Treeview.Heading", font=f["small_bold"], padding=(px(8), px(6)))
        for name, font in (("Small.TLabel", "small"), ("SmallSoft.TLabel", "small"), ("Bold.TLabel", "bold"),
                           ("Heading.TLabel", "heading"), ("Title.TLabel", "title"), ("Mono.TLabel", "mono"),
                           ("Badge.TLabel", "small")):
            st.configure(name, font=f[font])
        for word in ("Waiting", "Running", "Done", "Skipped", "Failed"):
            st.configure("%s.Chip.TLabel" % word, font=f["small"], padding=(px(8), px(3)),
                         relief="solid", borderwidth=1)
        for name in ("Card.TFrame", "Well.TFrame", "Box.TFrame", "Info.Notice.TFrame", "Ok.Notice.TFrame",
                     "Warn.Notice.TFrame", "Bad.Notice.TFrame"):
            st.configure(name, relief="solid", borderwidth=1)

    def _plain_tk_defaults(self) -> None:
        """Colours for the plain Tk widgets (Text, Canvas, Toplevel, Menu, list of a
        Combobox), through the option database, so that none opens white in dark mode."""
        root, p, f = self.root, self.palette, self.fonts
        try:
            root.configure(background=p.bg)
        except Exception:
            pass
        if self.high_contrast:
            return
        add = root.option_add
        for cls in ("Frame", "Toplevel", "Canvas", "Label", "Message", "Labelframe", "Panedwindow",
                    "Checkbutton", "Radiobutton", "Button", "Scale", "Scrollbar", "Menubutton"):
            add("*%s.background" % cls, p.bg)
            add("*%s.highlightBackground" % cls, p.bg)
            add("*%s.highlightColor" % cls, p.focus)
        for cls in ("Label", "Message", "Checkbutton", "Radiobutton", "Button", "Labelframe", "Menubutton"):
            add("*%s.foreground" % cls, p.text)
            add("*%s.font" % cls, f["body"])
            add("*%s.activeBackground" % cls, p.surface_alt)
            add("*%s.activeForeground" % cls, p.text)
            add("*%s.disabledForeground" % cls, self.disabled_fg)
        add("*Canvas.highlightThickness", 0)
        add("*Scrollbar.troughColor", p.surface_alt)
        for cls in ("Text", "Entry", "Listbox", "Spinbox"):
            add("*%s.background" % cls, p.surface)
            add("*%s.foreground" % cls, p.text)
            add("*%s.selectBackground" % cls, p.accent)
            add("*%s.selectForeground" % cls, p.on_accent)
            add("*%s.insertBackground" % cls, p.text)
            add("*%s.highlightBackground" % cls, p.border)
            add("*%s.highlightColor" % cls, p.focus)
            add("*%s.disabledForeground" % cls, self.disabled_fg)
            add("*%s.disabledBackground" % cls, p.surface_sunk)
            add("*%s.readonlyBackground" % cls, p.surface_sunk)
            add("*%s.font" % cls, f["body"])
            add("*%s.relief" % cls, "flat")
        add("*Text.inactiveSelectBackground", p.surface_alt)
        add("*Text.borderWidth", 0)
        add("*Text.highlightThickness", 0)
        add("*Listbox.borderWidth", 0)
        add("*Listbox.activeStyle", "none")
        add("*Menu.background", p.surface)
        add("*Menu.foreground", p.text)
        add("*Menu.activeBackground", p.accent_soft)
        add("*Menu.activeForeground", p.text)
        add("*Menu.disabledForeground", self.disabled_fg)
        add("*Menu.selectColor", p.accent)
        add("*Menu.borderWidth", 1)
        add("*Menu.activeBorderWidth", 0)
        add("*Menu.relief", "flat")
        add("*Menu.tearOff", 0)
        add("*Menu.font", f["body"])
        # the list that drops down from a ttk.Combobox is a plain Listbox
        add("*TCombobox*Listbox.background", p.surface)
        add("*TCombobox*Listbox.foreground", p.text)
        add("*TCombobox*Listbox.selectBackground", p.accent)
        add("*TCombobox*Listbox.selectForeground", p.on_accent)
        add("*TCombobox*Listbox.font", f["body"])


# --------------------------------------------------------------------------- module level

_ATTRIBUTE = "_tesseract_win_theme"
_latest: Optional[Theme] = None


def init(root: Any, *, is_dark: bool = False, is_high_contrast: bool = False,
         scale: Optional[float] = None) -> Theme:
    """Choose the palette and build the styles for ``root``. Call once, at start-up.

    ``is_dark`` and ``is_high_contrast`` come from the operating system (the caller asks).
    ``scale`` overrides the screen scaling read from the root window (gallery and tests).
    A second call for the same root returns the theme made by the first.
    """
    global _latest
    existing = getattr(root, _ATTRIBUTE, None)
    if isinstance(existing, Theme):
        _latest = existing
        return existing
    made = Theme(root, is_dark=is_dark, is_high_contrast=is_high_contrast, scale=scale)
    setattr(root, _ATTRIBUTE, made)
    _latest = made
    return made


def current(widget: Any = None) -> Theme:
    """The theme of the window ``widget`` lives in.

    A widget used on its own, before anyone called :func:`init`, gets the light theme.
    Without ``widget``: the theme made last.
    """
    if widget is not None:
        root = widget.nametowidget(".")
        found = getattr(root, _ATTRIBUTE, None)
        return found if isinstance(found, Theme) else init(root)
    if _latest is None:
        raise RuntimeError("theme.init(root) has not been called")
    return _latest


def px(value: float, widget: Any = None) -> int:
    """Shorthand for ``current(widget).px(value)``."""
    return current(widget).px(value)
