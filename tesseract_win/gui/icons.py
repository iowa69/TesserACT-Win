# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Pictures drawn at run time, at the size the screen scaling asks for.

Tk cannot scale a picture and a ``ttk.Treeview`` has no tick-box cell, so every small
picture the window needs (tick boxes, radio marks, chevrons, status dots, the rounded
backgrounds of buttons, fields, cards and chips, the "working" ring, the window icon) is
drawn here, in memory, in the colours of the palette in use, and handed to Tk as a PNG
with real transparency. Nothing is read from disk except the optional large icon.

Standard library only. The one exception is :func:`make_icon`, which draws the program's
mark with Pillow for the packaging step and for ``assets/icon.png``; Pillow is imported
inside that function and nowhere else, so the program starts without it.

Tk forgets a picture as soon as Python drops its last reference. :class:`IconSet` keeps
every picture it makes for as long as it lives; the theme owns one ``IconSet`` per window.
"""

from __future__ import annotations

import base64
import math
import os
import struct
import zlib
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "rgb", "to_hex", "mix", "encode_png", "stretch_rgba", "Bitmap", "IconSet", "make_icon", "save_assets",
    "mark_geometry", "MARK_BLUE",
]

RGB = Tuple[int, int, int]
Point = Tuple[float, float]

#: The house blue of the program's mark (the light palette's accent).
MARK_BLUE = "#2563eb"
_MARK_BLUE_DEEP = "#1d4ed8"
_MARK_BLUE_PALE = "#8fb3f8"
_TILE_TOP = "#ffffff"
_TILE_BOTTOM = "#dce7fd"
_TILE_EDGE = "#c3cfe6"


# --------------------------------------------------------------------------- colours

def rgb(colour: str) -> RGB:
    """``"#rrggbb"`` (or ``"#rgb"``) to a tuple of three numbers from 0 to 255."""
    text = colour.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError("not a colour: %r" % (colour,))
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def to_hex(colour: Sequence[float]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in colour[:3])


def mix(first: str, second: str, amount: float) -> str:
    """A colour between ``first`` (amount 0) and ``second`` (amount 1)."""
    a, b = rgb(first), rgb(second)
    t = max(0.0, min(1.0, amount))
    return to_hex([a[i] + (b[i] - a[i]) * t for i in range(3)])


# --------------------------------------------------------------------------- PNG

def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """An 8-bit RGBA PNG from ``width * height * 4`` bytes, rows from the top."""
    if len(rgba) != width * height * 4:
        raise ValueError("pixel data does not match the size")
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        raw.append(0)                                   # filter type 0: none
        raw += rgba[row * stride:(row + 1) * stride]

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- drawing

class Bitmap:
    """A small RGBA picture with anti-aliased shapes, in pure Python.

    Coordinates are pixels with the origin at the top left; a shape that ends on a whole
    number ends exactly on a pixel edge, so straight sides stay sharp and only curves and
    slanted lines are softened. Meant for pictures of a few dozen pixels.
    """

    def __init__(self, width: int, height: int):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._p = [0.0] * (self.width * self.height * 4)      # premultiplied r, g, b, a

    # -- low level
    def _blend(self, x: int, y: int, colour: Sequence[float], alpha: float) -> None:
        if alpha <= 0.0:
            return
        if alpha > 1.0:
            alpha = 1.0
        i = (y * self.width + x) * 4
        keep = 1.0 - alpha
        p = self._p
        p[i] = colour[0] * alpha + p[i] * keep
        p[i + 1] = colour[1] * alpha + p[i + 1] * keep
        p[i + 2] = colour[2] * alpha + p[i + 2] * keep
        p[i + 3] = alpha + p[i + 3] * keep

    def _span(self, low: float, high: float, limit: int) -> range:
        return range(max(0, int(math.floor(low))), min(limit, int(math.ceil(high))))

    # -- shapes
    def rounded_rect(self, x0: float, y0: float, x1: float, y1: float, radius: float, *,
                     fill: Optional[str] = None, outline: Optional[str] = None,
                     width: float = 1.0, opacity: float = 1.0,
                     fill_bottom: Optional[str] = None) -> None:
        """A rectangle with round corners. ``outline`` is drawn inside the edge.

        ``fill_bottom`` turns the fill into a top-to-bottom blend from ``fill``.
        """
        if x1 <= x0 or y1 <= y0 or (fill is None and outline is None):
            return
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        hw, hh = (x1 - x0) / 2.0, (y1 - y0) / 2.0
        r = max(0.0, min(radius, hw, hh))
        fill_rgb = rgb(fill) if fill else None
        bottom_rgb = rgb(fill_bottom) if fill_bottom else None
        line_rgb = rgb(outline) if outline else None
        span_y = max(1e-9, y1 - y0)
        for py in self._span(y0, y1, self.height):
            dy = abs(py + 0.5 - cy) - (hh - r)
            row_fill = fill_rgb
            if fill_rgb is not None and bottom_rgb is not None:
                t = min(1.0, max(0.0, (py + 0.5 - y0) / span_y))
                row_fill = tuple(fill_rgb[i] + (bottom_rgb[i] - fill_rgb[i]) * t for i in range(3))
            for px in self._span(x0, x1, self.width):
                dx = abs(px + 0.5 - cx) - (hw - r)
                ox = dx if dx > 0.0 else 0.0
                oy = dy if dy > 0.0 else 0.0
                inside = dx if dx > dy else dy
                dist = math.sqrt(ox * ox + oy * oy) + (inside if inside < 0.0 else 0.0) - r
                outer = 0.5 - dist
                if outer <= 0.0:
                    continue
                if outer > 1.0:
                    outer = 1.0
                if line_rgb is None:
                    self._blend(px, py, row_fill, outer * opacity)       # type: ignore[arg-type]
                    continue
                inner = 0.5 - (dist + width)
                inner = 0.0 if inner < 0.0 else (1.0 if inner > 1.0 else inner)
                if row_fill is None:
                    self._blend(px, py, line_rgb, (outer - inner) * opacity)
                else:
                    colour = (line_rgb[0] + (row_fill[0] - line_rgb[0]) * inner,
                              line_rgb[1] + (row_fill[1] - line_rgb[1]) * inner,
                              line_rgb[2] + (row_fill[2] - line_rgb[2]) * inner)
                    self._blend(px, py, colour, outer * opacity)

    def circle(self, cx: float, cy: float, radius: float, *, fill: Optional[str] = None,
               outline: Optional[str] = None, width: float = 1.0, opacity: float = 1.0) -> None:
        self.rounded_rect(cx - radius, cy - radius, cx + radius, cy + radius, radius,
                          fill=fill, outline=outline, width=width, opacity=opacity)

    def polyline(self, points: Sequence[Point], width: float, colour: str, *,
                 opacity: float = 1.0, closed: bool = False) -> None:
        """Joined straight lines with round ends and round joints."""
        if len(points) < 2:
            return
        pts = list(points) + ([points[0]] if closed else [])
        segments = [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1)]
        half = width / 2.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ink = rgb(colour)
        prepared = []
        for ax, ay, bx, by in segments:
            vx, vy = bx - ax, by - ay
            prepared.append((ax, ay, vx, vy, vx * vx + vy * vy))
        for py in self._span(min(ys) - half - 1, max(ys) + half + 1, self.height):
            qy = py + 0.5
            for px in self._span(min(xs) - half - 1, max(xs) + half + 1, self.width):
                qx = px + 0.5
                best = 1e18
                for ax, ay, vx, vy, length2 in prepared:
                    wx, wy = qx - ax, qy - ay
                    t = 0.0 if length2 <= 0.0 else (wx * vx + wy * vy) / length2
                    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                    ex, ey = wx - vx * t, wy - vy * t
                    d2 = ex * ex + ey * ey
                    if d2 < best:
                        best = d2
                cover = 0.5 - (math.sqrt(best) - half)
                if cover > 0.0:
                    self._blend(px, py, ink, (1.0 if cover > 1.0 else cover) * opacity)

    def line(self, x0: float, y0: float, x1: float, y1: float, width: float, colour: str, *,
             opacity: float = 1.0) -> None:
        self.polyline([(x0, y0), (x1, y1)], width, colour, opacity=opacity)

    def arc(self, cx: float, cy: float, radius: float, start: float, extent: float,
            width: float, colour: str, *, opacity: float = 1.0) -> None:
        """Part of a circle; angles in degrees, clockwise from twelve o'clock."""
        steps = max(4, int(abs(extent) / 12.0))
        points = []
        for i in range(steps + 1):
            angle = math.radians(start + extent * i / steps)
            points.append((cx + radius * math.sin(angle), cy - radius * math.cos(angle)))
        self.polyline(points, width, colour, opacity=opacity)

    # -- output
    def to_rgba(self) -> bytes:
        out = bytearray(len(self._p))
        p = self._p
        for i in range(0, len(p), 4):
            alpha = p[i + 3]
            if alpha <= 0.0:
                continue
            out[i] = max(0, min(255, int(p[i] / alpha + 0.5)))
            out[i + 1] = max(0, min(255, int(p[i + 1] / alpha + 0.5)))
            out[i + 2] = max(0, min(255, int(p[i + 2] / alpha + 0.5)))
            out[i + 3] = max(0, min(255, int(alpha * 255.0 + 0.5)))
        return bytes(out)

    def to_png(self) -> bytes:
        return encode_png(self.width, self.height, self.to_rgba())


def stretch_rgba(rgba: bytes, width: int, height: int, column: int, row: int,
                 wide: int, tall: int) -> Tuple[bytes, int, int]:
    """Repeat one column ``wide`` times and one row ``tall`` times.

    ttk fills a stretched picture by laying copies of its middle side by side. With a
    middle of one pixel that is thousands of copies for a card, and the window takes
    seconds to draw; with a wide middle it is a handful. So the small picture is drawn
    once and its middle is widened here, by copying bytes.
    """
    wide, tall = max(1, wide), max(1, tall)
    stride = width * 4
    rows = []
    for y in range(height):
        line = rgba[y * stride:(y + 1) * stride]
        rows.append(line[:column * 4] + line[column * 4:(column + 1) * 4] * wide + line[(column + 1) * 4:])
    out = b"".join(rows[:row]) + rows[row] * tall + b"".join(rows[row + 1:])
    return out, width - 1 + wide, height - 1 + tall


# --------------------------------------------------------------------------- the mark

def _cube_edges() -> List[Tuple[int, int]]:
    edges = []
    for a in range(8):
        for bit in (1, 2, 4):
            b = a ^ bit
            if a < b:
                edges.append((a, b))
    return edges


def mark_geometry(detail: bool = True) -> Dict[str, Any]:
    """The program's mark in unit coordinates (0 to 1 across the tile).

    A tesseract: a cube within a cube, joined at the corners. ``detail`` gives the cubes
    seen from slightly above and to one side; without it (for 16 to 32 pixel pictures) the
    same figure seen head-on, a square within a square, which stays readable when small.

    Returns ``{"lines": [(x0, y0, x1, y1, width, colour)], "dots": [(x, y, radius, colour)]}``
    in drawing order.
    """
    lines: List[Tuple[float, float, float, float, float, str]] = []
    dots: List[Tuple[float, float, float, str]] = []
    if not detail:
        outer, inner = 0.27, 0.105
        corners = ((-1, -1), (1, -1), (1, 1), (-1, 1))
        for sx, sy in corners:
            lines.append((0.5 + sx * outer, 0.5 + sy * outer, 0.5 + sx * inner, 0.5 + sy * inner,
                          0.06, _MARK_BLUE_PALE))
        for half, width, colour in ((outer, 0.085, MARK_BLUE), (inner, 0.07, _MARK_BLUE_DEEP)):
            for i in range(4):
                ax, ay = corners[i]
                bx, by = corners[(i + 1) % 4]
                lines.append((0.5 + ax * half, 0.5 + ay * half, 0.5 + bx * half, 0.5 + by * half,
                              width, colour))
        return {"lines": lines, "dots": dots}

    yaw, pitch = math.radians(-29.0), math.radians(23.0)

    def turn(x: float, y: float, z: float) -> Tuple[float, float, float]:
        x1 = x * math.cos(yaw) + z * math.sin(yaw)
        z1 = -x * math.sin(yaw) + z * math.cos(yaw)
        return x1, y * math.cos(pitch) - z1 * math.sin(pitch), y * math.sin(pitch) + z1 * math.cos(pitch)

    corners3 = [((1 if i & 1 else -1), (1 if i & 2 else -1), (1 if i & 4 else -1)) for i in range(8)]
    turned = [turn(*c) for c in corners3]
    far = min(range(8), key=lambda i: turned[i][2])          # the corner hidden behind the cube
    reach = max(max(abs(x), abs(y)) for x, y, _z in turned)
    outer_scale = 0.335 / reach
    inner_scale = outer_scale * 0.46
    outer = [(0.5 + x * outer_scale, 0.5 + y * outer_scale) for x, y, _z in turned]
    inner = [(0.5 + x * inner_scale, 0.5 + y * inner_scale) for x, y, _z in turned]
    edges = _cube_edges()
    back = [e for e in edges if far in e]
    front = [e for e in edges if far not in e]
    for i in range(8):
        lines.append((outer[i][0], outer[i][1], inner[i][0], inner[i][1], 0.014, _MARK_BLUE_PALE))
    for group, width, colour in ((back, 0.022, _MARK_BLUE_PALE), (front, 0.032, MARK_BLUE)):
        for a, b in group:
            lines.append((outer[a][0], outer[a][1], outer[b][0], outer[b][1], width, colour))
    for group, width, colour in ((back, 0.016, _MARK_BLUE_PALE), (front, 0.023, _MARK_BLUE_DEEP)):
        for a, b in group:
            lines.append((inner[a][0], inner[a][1], inner[b][0], inner[b][1], width, colour))
    for i, (x, y) in enumerate(outer):
        if i != far:
            dots.append((x, y, 0.027, MARK_BLUE))
    for i, (x, y) in enumerate(inner):
        if i != far:
            dots.append((x, y, 0.019, _MARK_BLUE_DEEP))
    return {"lines": lines, "dots": dots}


def _draw_mark(size: int) -> Bitmap:
    """The mark at ``size`` pixels with the built-in drawing code (no Pillow)."""
    bmp = Bitmap(size, size)
    edge = max(1.0, size / 96.0)
    bmp.rounded_rect(0, 0, size, size, size * 0.22, fill=_TILE_TOP, fill_bottom=_TILE_BOTTOM,
                     outline=_TILE_EDGE, width=edge)
    shape = mark_geometry(detail=size >= 40)
    for x0, y0, x1, y1, width, colour in shape["lines"]:
        bmp.line(x0 * size, y0 * size, x1 * size, y1 * size, max(1.0, width * size), colour)
    for x, y, radius, colour in shape["dots"]:
        bmp.circle(x * size, y * size, radius * size, fill=colour)
    return bmp


def make_icon(size: int = 256):
    """The program's mark as a Pillow image (RGBA), ``size`` pixels square.

    A tesseract in the house blue on a rounded, nearly white tile. Used by the packaging
    step (the ``.ico``) and to write ``assets/icon.png``. Needs Pillow; the running program
    never calls it.
    """
    from PIL import Image, ImageDraw     # only here: the program itself starts without Pillow

    size = max(16, int(size))
    over = 4 if size <= 256 else 2
    big = size * over
    top, bottom = rgb(_TILE_TOP), rgb(_TILE_BOTTOM)
    blend = Image.new("RGBA", (1, big))
    for y in range(big):
        t = y / max(1, big - 1)
        blend.putpixel((0, y), tuple(int(round(top[i] + (bottom[i] - top[i]) * t)) for i in range(3)) + (255,))
    tile = blend.resize((big, big))
    mask = Image.new("L", (big, big), 0)
    radius = int(round(big * 0.22))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=255)
    picture = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    picture.paste(tile, (0, 0), mask)
    draw = ImageDraw.Draw(picture)
    edge = max(over, int(round(big / 96.0)))
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, outline=rgb(_TILE_EDGE) + (255,), width=edge)

    shape = mark_geometry(detail=size >= 40)
    for x0, y0, x1, y1, width, colour in shape["lines"]:
        w = max(over, int(round(width * big)))
        ink = rgb(colour) + (255,)
        draw.line((x0 * big, y0 * big, x1 * big, y1 * big), fill=ink, width=w)
        for x, y in ((x0, y0), (x1, y1)):                       # round ends and joints
            draw.ellipse((x * big - w / 2.0, y * big - w / 2.0, x * big + w / 2.0, y * big + w / 2.0), fill=ink)
    for x, y, r, colour in shape["dots"]:
        draw.ellipse((x * big - r * big, y * big - r * big, x * big + r * big, y * big + r * big),
                     fill=rgb(colour) + (255,))
    return picture.resize((size, size), Image.LANCZOS)


def save_assets(folder: Optional[str] = None) -> List[str]:
    """Write ``icon.png`` (256 pixels) into the assets folder. Returns the paths written."""
    if folder is None:
        folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "icon.png")
    make_icon(256).save(path, optimize=True)
    return [path]


# --------------------------------------------------------------------------- the set

class IconSet:
    """Every picture of one window, drawn once at the window's scale and kept alive.

    ``palette`` maps the colour names of the theme ("accent", "surface", "border", ...)
    to ``#rrggbb``. ``scale`` is the screen scaling (1.0 at 96 dpi, 1.5 at 150 %).
    A colour argument of any method is either such a name or a ``#rrggbb`` value.
    """

    def __init__(self, root: Any, palette: Mapping[str, str], scale: float = 1.0):
        self.root = root
        self.palette = dict(palette)
        self.scale = max(0.5, float(scale))
        #: width in pixels of a hairline at this scale (1 up to 175 %, then 2, ...)
        self.hair = max(1, int(self.scale + 0.25))
        self._cache: Dict[Tuple[Any, ...], Any] = {}

    # -- helpers
    def px(self, value: float) -> int:
        """Pixels at this scale for a number meant for a 96 dpi screen."""
        if value == 0:
            return 0
        scaled = int(round(abs(value) * self.scale))
        return max(1, scaled) if value > 0 else -max(1, scaled)

    def colour(self, name: str) -> str:
        return name if name.startswith("#") else self.palette[name]

    def _photo(self, key: Tuple[Any, ...], draw: Callable[[], Any]) -> Any:
        """The picture for ``key``; ``draw`` returns a :class:`Bitmap` or PNG bytes."""
        image = self._cache.get(key)
        if image is None:
            import tkinter
            drawn = draw()
            data = drawn if isinstance(drawn, (bytes, bytearray)) else drawn.to_png()
            image = tkinter.PhotoImage(master=self.root, data=base64.b64encode(bytes(data)))
            self._cache[key] = image
        return image

    def keep(self, key: Any, image: Any) -> Any:
        """Hold a picture made elsewhere so Tk does not drop it."""
        self._cache[("kept", key)] = image
        return image

    def __len__(self) -> int:
        return len(self._cache)

    # -- tick boxes and radio marks
    @property
    def box_size(self) -> int:
        """Side of a tick-box picture, margin for the focus ring included."""
        return self.px(16) + 2 * self.px(3)

    def checkbox(self, on: bool = False, *, hover: bool = False, pressed: bool = False,
                 disabled: bool = False, focus: bool = False, mixed: bool = False,
                 unavailable: bool = False) -> Any:
        """A tick box. ``unavailable`` is the greyed box of a row that cannot be ticked."""
        key = ("check", on, hover, pressed, disabled, focus, mixed, unavailable)
        return self._photo(key, lambda: self._draw_box(False, on, hover, pressed, disabled, focus,
                                                       mixed, unavailable))

    def radio(self, on: bool = False, *, hover: bool = False, pressed: bool = False,
              disabled: bool = False, focus: bool = False) -> Any:
        key = ("radio", on, hover, pressed, disabled, focus)
        return self._photo(key, lambda: self._draw_box(True, on, hover, pressed, disabled, focus,
                                                       False, False))

    def tick(self, state: str) -> Any:
        """The tick box of a table row: ``"on"``, ``"off"`` or ``"na"`` (not available)."""
        if state == "on":
            return self.checkbox(True)
        if state == "off":
            return self.checkbox(False)
        return self.checkbox(False, unavailable=True)

    def _draw_box(self, round_: bool, on: bool, hover: bool, pressed: bool, disabled: bool,
                  focus: bool, mixed: bool, unavailable: bool) -> Bitmap:
        c = self.colour
        box, margin = self.px(16), self.px(3)
        size = box + 2 * margin
        bmp = Bitmap(size, size)
        x0 = y0 = float(margin)
        x1 = y1 = float(margin + box)
        radius = box / 2.0 if round_ else self.px(4)
        hair = float(self.hair)
        marked = on or mixed
        if unavailable:
            fill, edge, ink = c("surface_alt"), c("border"), mix(c("muted"), c("surface_alt"), 0.35)
        elif disabled:
            fill = mix(c("border_strong"), c("surface"), 0.35) if marked else c("surface_sunk")
            edge = fill if marked else c("border")
            ink = c("surface")
        elif marked:
            fill = c("accent_active") if pressed else (c("accent_hover") if hover else c("accent"))
            edge, ink = fill, c("on_accent")
        else:
            fill = c("accent_soft") if pressed else c("surface")
            edge = c("accent") if (hover or pressed) else c("border_strong")
            ink = c("on_accent")
        if focus and not disabled:
            gap = float(self.px(2))
            bmp.rounded_rect(x0 - gap - hair, y0 - gap - hair, x1 + gap + hair, y1 + gap + hair,
                             radius + gap + hair, outline=c("focus"), width=max(hair, self.px(2) * 0.75))
        bmp.rounded_rect(x0, y0, x1, y1, radius, fill=fill, outline=edge, width=hair)
        stroke = max(1.5, box * 0.115)
        if unavailable or mixed:
            bmp.line(x0 + box * 0.30, y0 + box * 0.5, x0 + box * 0.70, y0 + box * 0.5, stroke, ink)
        elif on and round_:
            bmp.circle(x0 + box / 2.0, y0 + box / 2.0, box * 0.20, fill=ink)
        elif on:
            bmp.polyline([(x0 + box * 0.25, y0 + box * 0.53), (x0 + box * 0.43, y0 + box * 0.70),
                          (x0 + box * 0.76, y0 + box * 0.32)], stroke, ink)
        return bmp

    # -- small signs
    def chevron(self, direction: str = "right", colour: str = "muted", size: int = 12) -> Any:
        """An arrow head: ``right``, ``down``, ``left`` or ``up``."""
        def draw() -> Bitmap:
            s = self.px(size)
            bmp = Bitmap(s, s)
            a, m, b = s * 0.34, s * 0.5, s * 0.66
            lo, hi = s * 0.24, s * 0.76
            shapes = {"right": [(a, lo), (b, m), (a, hi)], "left": [(b, lo), (a, m), (b, hi)],
                      "down": [(lo, a), (m, b), (hi, a)], "up": [(lo, b), (m, a), (hi, b)]}
            bmp.polyline(shapes[direction], max(1.4, s * 0.13), self.colour(colour))
            return bmp
        return self._photo(("chevron", direction, colour, size), draw)

    def dot(self, colour: str = "ok", *, hollow: bool = False, size: int = 10) -> Any:
        """A status dot; ``hollow`` for "not yet"."""
        def draw() -> Bitmap:
            s = self.px(size)
            bmp = Bitmap(s, s)
            radius = s * 0.36
            if hollow:
                bmp.circle(s / 2.0, s / 2.0, radius, outline=self.colour(colour), width=max(1.2, s * 0.14))
            else:
                bmp.circle(s / 2.0, s / 2.0, radius, fill=self.colour(colour))
            return bmp
        return self._photo(("dot", colour, hollow, size), draw)

    def glyph(self, name: str, colour: str = "text_soft", size: int = 16) -> Any:
        """A line sign: ``check``, ``cross``, ``dash``, ``close``, or a round badge
        ``info``, ``warn``, ``bad``, ``ok`` (filled in ``colour`` with a light sign)."""
        def draw() -> Bitmap:
            s = self.px(size)
            bmp = Bitmap(s, s)
            ink = self.colour(colour)
            stroke = max(1.4, s * 0.12)
            if name in ("info", "warn", "bad", "ok"):
                bmp.circle(s / 2.0, s / 2.0, s * 0.47, fill=ink)
                light = self.colour("surface")
                thin = max(1.3, s * 0.115)
                if name == "ok":
                    bmp.polyline([(s * 0.29, s * 0.53), (s * 0.44, s * 0.67), (s * 0.71, s * 0.35)], thin, light)
                elif name == "info":
                    bmp.circle(s * 0.5, s * 0.29, thin * 0.62, fill=light)
                    bmp.line(s * 0.5, s * 0.47, s * 0.5, s * 0.73, thin, light)
                else:
                    bmp.line(s * 0.5, s * 0.26, s * 0.5, s * 0.55, thin, light)
                    bmp.circle(s * 0.5, s * 0.72, thin * 0.62, fill=light)
            elif name == "check":
                bmp.polyline([(s * 0.20, s * 0.54), (s * 0.41, s * 0.74), (s * 0.80, s * 0.28)], stroke, ink)
            elif name in ("cross", "close"):
                lo, hi = (s * 0.27, s * 0.73) if name == "cross" else (s * 0.30, s * 0.70)
                bmp.line(lo, lo, hi, hi, stroke, ink)
                bmp.line(lo, hi, hi, lo, stroke, ink)
            elif name == "dash":
                bmp.line(s * 0.26, s * 0.5, s * 0.74, s * 0.5, stroke, ink)
            else:
                raise ValueError("no such sign: %r" % (name,))
            return bmp
        return self._photo(("glyph", name, colour, size), draw)

    def spinner(self, colour: str = "accent", size: int = 16, frames: int = 12,
                track: str = "border") -> List[Any]:
        """The frames of the turning "working" ring, in order."""
        out = []
        for index in range(frames):
            def draw(index: int = index) -> Bitmap:
                s = self.px(size)
                bmp = Bitmap(s, s)
                stroke = max(1.6, s * 0.125)
                radius = s / 2.0 - stroke / 2.0 - 0.5
                bmp.circle(s / 2.0, s / 2.0, radius + stroke / 2.0, outline=self.colour(track), width=stroke)
                bmp.arc(s / 2.0, s / 2.0, radius, index * 360.0 / frames, 100.0, stroke, self.colour(colour))
                return bmp
            out.append(self._photo(("spinner", colour, size, frames, track, index), draw))
        return out

    def ring(self, colour: str = "border", size: int = 16) -> Any:
        """The still ring shown where the "working" ring is not turning."""
        def draw() -> Bitmap:
            s = self.px(size)
            bmp = Bitmap(s, s)
            stroke = max(1.6, s * 0.125)
            bmp.circle(s / 2.0, s / 2.0, s / 2.0 - 0.5, outline=self.colour(colour), width=stroke)
            return bmp
        return self._photo(("ring", colour, size), draw)

    def disc(self, fill: str, outline: Optional[str] = None, size: int = 26) -> Any:
        """A round plate, for the numbers of the stepper."""
        def draw() -> Bitmap:
            s = self.px(size)
            bmp = Bitmap(s, s)
            bmp.circle(s / 2.0, s / 2.0, s / 2.0, fill=self.colour(fill),
                       outline=self.colour(outline) if outline else None, width=float(self.hair))
            return bmp
        return self._photo(("disc", fill, outline, size), draw)

    def blank(self, width: int = 1, height: int = 1) -> Any:
        """A fully transparent picture (already in pixels)."""
        return self._photo(("blank", width, height), lambda: Bitmap(width, height))

    # -- stretchable backgrounds
    #: how wide the repeated middle of a stretchable picture is, in pixels
    MIDDLE = 64

    def patch_border(self, radius: float) -> int:
        """The part of a :meth:`patch` that must not be stretched, in pixels."""
        return max(self.px(radius) if radius else 0, 3 * self.hair)

    def patch(self, *, fill: Optional[str] = None, outline: Optional[str] = None,
              radius: float = 5, width: int = 1, opacity: float = 1.0,
              inner: Optional[str] = None, middle: Optional[int] = None) -> Any:
        """A rounded box to be stretched by ttk (corners kept, middle repeated).

        ``width`` is the outline in hairlines. ``inner`` adds a second ring just inside
        the outline (the focus mark of a filled button). With neither ``fill`` nor
        ``outline`` the picture is fully transparent. Every patch of one ``radius`` has
        the same size, whatever its outline, so the states of a control never jump.
        ``middle`` is the size of the repeated part (large for cards, see
        :func:`stretch_rgba`); the unstretched border is :meth:`patch_border`.
        """
        middle = self.MIDDLE if middle is None else middle
        key = ("patch", fill, outline, radius, width, opacity, inner, middle)

        def draw() -> bytes:
            border = self.patch_border(radius)
            side = 2 * border + 1
            bmp = Bitmap(side, side)
            r = self.px(radius) if radius else 0
            line = float(self.hair * width)
            bmp.rounded_rect(0, 0, side, side, r,
                             fill=self.colour(fill) if fill else None,
                             outline=self.colour(outline) if outline else None,
                             width=line, opacity=opacity)
            if inner:
                bmp.rounded_rect(line, line, side - line, side - line, max(0.0, r - line),
                                 outline=self.colour(inner), width=float(self.hair))
            rgba, w, h = stretch_rgba(bmp.to_rgba(), side, side, border, border, middle, middle)
            return encode_png(w, h, rgba)
        return self._photo(key, draw)

    def heading(self, fill: str, line: str = "border") -> Any:
        """The background of a table heading: flat, a hairline below, a short divider
        at the right. Stretch it with :meth:`heading_border`."""
        def draw() -> bytes:
            cap = self.px(8)
            w, h = 1 + 1 + self.hair + 1, 2 * cap + 1
            bmp = Bitmap(w, h)
            bmp.rounded_rect(0, 0, w, h, 0, fill=self.colour(fill))
            bmp.rounded_rect(0, h - self.hair, w, h, 0, fill=self.colour(line))
            bmp.rounded_rect(w - self.hair, cap, w, cap + 1, 0, fill=self.colour(line))
            rgba, w2, h2 = stretch_rgba(bmp.to_rgba(), w, h, 1, cap, self.MIDDLE, 16)
            return encode_png(w2, h2, rgba)
        return self._photo(("heading", fill, line), draw)

    def heading_border(self) -> Tuple[int, int, int, int]:
        return (1, self.px(8), self.hair + 1, self.px(8))

    def bar(self, colour: str, thickness: int = 6, length: int = 96) -> Any:
        """A rounded bar, for the trough and the filled part of a progress bar."""
        def draw() -> Bitmap:
            t, ln = self.px(thickness), self.px(length)
            bmp = Bitmap(ln, t)
            bmp.rounded_rect(0, 0, ln, t, t / 2.0, fill=self.colour(colour))
            return bmp
        return self._photo(("bar", colour, thickness, length), draw)

    def pill(self, colour: str, *, vertical: bool = True, thickness: int = 6, length: int = 72,
             margin: int = 3, opacity: float = 1.0) -> Any:
        """The thumb of a thin scroll bar: a rounded bar with clear margins beside it."""
        def draw() -> Bitmap:
            t, m, ln = self.px(thickness), self.px(margin), self.px(length)
            w, h = (t + 2 * m, ln) if vertical else (ln, t + 2 * m)
            bmp = Bitmap(w, h)
            if vertical:
                bmp.rounded_rect(m, self.px(2), m + t, h - self.px(2), t / 2.0, fill=self.colour(colour), opacity=opacity)
            else:
                bmp.rounded_rect(self.px(2), m, w - self.px(2), m + t, t / 2.0, fill=self.colour(colour), opacity=opacity)
            return bmp
        return self._photo(("pill", colour, vertical, thickness, length, margin, opacity), draw)

    # -- the window icon
    def mark(self, size: int) -> Any:
        """The program's mark at exactly ``size`` pixels, drawn without Pillow."""
        return self._photo(("mark", size), lambda: _draw_mark(size))

    def window_icons(self, folder: Optional[str] = None) -> List[Any]:
        """Pictures for ``wm iconphoto``, largest first.

        Small sizes are drawn here. The 256 pixel ``icon.png`` is added when it is found
        in ``folder``, in ``appdirs.resource_dir()`` or in the package's ``assets`` folder.
        """
        import tkinter
        images: List[Any] = []
        candidates = [folder] if folder else []
        try:
            from .. import appdirs               # where the frozen program keeps its read-only data
            candidates.append(appdirs.resource_dir())
        except Exception:
            pass
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"))
        for place in candidates:
            path = os.path.join(place, "icon.png") if place else ""
            if path and os.path.isfile(path):
                try:
                    images.append(self.keep(("icon-file", path), tkinter.PhotoImage(master=self.root, file=path)))
                    break
                except Exception:          # a damaged file must never stop the window opening
                    pass
        for size in (48, 32, 24, 16):
            images.append(self.mark(size))
        return images


if __name__ == "__main__":       # python -m tesseract_win.gui.icons : rewrite assets/icon.png
    for written in save_assets():
        print(written)
