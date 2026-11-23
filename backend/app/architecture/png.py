"""A diagram as a PNG picture, for a Word file (2026-11-22, the user's request: the design documents' Word files should carry the ER pictures).

Word cannot show an SVG without a picture inside it as well, and no SVG converter is installed here, so this draws the picture again with Pillow from the same SVG text the screen shows.
It reads ONLY what the Architecture diagrams use (`rect` with rounded corners, `line`, `polyline`, `polygon` and `text`, with their fill, stroke, width, size, weight and anchor) and
skips anything else, so a picture it cannot fully read still comes out as far as it can, and nothing here ever raises: the caller gets None and writes the document without a picture.

The picture is drawn at twice its size, so text stays sharp when the person resizes it in Word.
"""
from __future__ import annotations

import io
import re
from html import unescape
from typing import Iterator

from PIL import Image, ImageDraw, ImageFont

SCALE = 2
MAX_SIDE = 6000                  # a picture is never larger than this on either side, before the scale
MAX_SHAPES = 4000
_TAG = re.compile(r"<(?P<name>rect|line|polyline|polygon|text|svg)\b(?P<attrs>[^>]*?)(?P<close>/?)>(?P<body>[^<]*)", re.DOTALL)
_ATTR = re.compile(r'([A-Za-z][A-Za-z0-9:-]*)\s*=\s*"([^"]*)"')
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_FONTS: dict[tuple[float, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _attrs(text: str) -> dict[str, str]:
    return {k: v for k, v in _ATTR.findall(text)}


def _num(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _colour(value: str | None) -> str | None:
    """A colour Pillow can use, or None for 'none' or anything it cannot read."""
    if not value or value == "none":
        return None
    return value if re.fullmatch(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", value) else None


def _font(size: float, bold: bool):
    key = (round(size * SCALE, 1), bold)
    cached = _FONTS.get(key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.load_default(size=key[0])
    except Exception:                                    # an old Pillow with no scalable default font: the bitmap one still draws something
        font = ImageFont.load_default()
    _FONTS[key] = font
    return font


def _points(text: str | None) -> list[tuple[float, float]]:
    numbers = [float(n) for n in _NUMBER.findall(text or "")]
    return [(numbers[i] * SCALE, numbers[i + 1] * SCALE) for i in range(0, len(numbers) - 1, 2)]


def _shapes(svg: str) -> Iterator[tuple[str, dict[str, str], str]]:
    count = 0
    for m in _TAG.finditer(svg):
        count += 1
        if count > MAX_SHAPES:
            return
        yield m.group("name"), _attrs(m.group("attrs")), unescape(m.group("body") or "")


def svg_to_png(svg: str) -> bytes | None:
    """The PNG for one of our diagrams, or None when it cannot be read or is not one."""
    try:
        return _draw(svg)
    except Exception:                                   # a picture is a convenience: the document is written without it
        return None


def _draw(svg: str) -> bytes | None:
    head = next(((a) for name, a, _ in _shapes(svg) if name == "svg"), None)
    if head is None:
        return None
    width, height = _num(head.get("width")), _num(head.get("height"))
    if not (10 <= width <= MAX_SIDE and 10 <= height <= MAX_SIDE):
        return None
    image = Image.new("RGB", (int(width * SCALE), int(height * SCALE)), "white")
    draw = ImageDraw.Draw(image)
    for name, a, body in _shapes(svg):
        if name == "rect":
            x, y, w, h = (_num(a.get(k)) * SCALE for k in ("x", "y", "width", "height"))
            fill, stroke = _colour(a.get("fill")), _colour(a.get("stroke"))
            line = max(1, round(_num(a.get("stroke-width"), 1) * SCALE)) if stroke else 0
            radius = _num(a.get("rx")) * SCALE
            box = (x, y, x + w - 1, y + h - 1)
            if radius > 0:
                draw.rounded_rectangle(box, radius=radius, fill=fill, outline=stroke, width=line)
            else:
                draw.rectangle(box, fill=fill, outline=stroke, width=line)
        elif name == "line":
            stroke = _colour(a.get("stroke"))
            if stroke:
                draw.line([(_num(a.get("x1")) * SCALE, _num(a.get("y1")) * SCALE), (_num(a.get("x2")) * SCALE, _num(a.get("y2")) * SCALE)],
                          fill=stroke, width=max(1, round(_num(a.get("stroke-width"), 1) * SCALE)))
        elif name == "polyline":
            stroke, pts = _colour(a.get("stroke")), _points(a.get("points"))
            if stroke and len(pts) >= 2:
                draw.line(pts, fill=stroke, width=max(1, round(_num(a.get("stroke-width"), 1) * SCALE)), joint="curve")
        elif name == "polygon":
            pts = _points(a.get("points"))
            fill, stroke = _colour(a.get("fill")), _colour(a.get("stroke"))
            if len(pts) >= 3 and (fill or stroke):
                draw.polygon(pts, fill=fill, outline=stroke)
        elif name == "text":
            text = " ".join(body.split()).replace("\u2192", "->")           # the built-in font has no arrow character
            fill = _colour(a.get("fill")) or "#1f2937"
            if text:
                anchor = {"middle": "ms", "end": "rs"}.get(a.get("text-anchor", "start"), "ls")           # the baseline is the y the picture gives
                draw.text((_num(a.get("x")) * SCALE, _num(a.get("y")) * SCALE), text, font=_font(_num(a.get("font-size"), 12), a.get("font-weight") == "bold"), fill=fill, anchor=anchor)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def png_size(png: bytes) -> tuple[int, int]:
    """(width, height) in pixels of a PNG made here; (0, 0) when it cannot be read."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            return image.size
    except Exception:
        return (0, 0)
