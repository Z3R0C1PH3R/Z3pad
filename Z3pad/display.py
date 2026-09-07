"""Framebuffer text output using nothing but the standard library.

Z3apps draws to the framebuffer with numpy, which is a 34 MB pip install on a
handheld that ships without it. Z3pad only ever puts lines of text on screen, so
this does the same glyph blitting with mmap and byte slices instead, which keeps
the whole app installable offline.

font32.bin is a flat array of 95 glyphs (space through '~'), each 21x38 pixels of
32-bit ARGB, so a glyph row is just an 84-byte slice that can be copied straight
into the framebuffer.
"""

from fcntl import ioctl
from mmap import mmap
import os

FB_DEVICE = "/dev/fb0"
RES = (640, 480)          # w, h
BPP = 4
FULL = RES[0] * RES[1]

W, H = 21, 38             # glyph size
FIRST, GLYPHS = 32, 95    # ' ' .. '~'

BLACK = b"\x00\x00\x00\xff"
WHITE = b"\xff\xff\xff\xff"

COLS = RES[0] // W        # 30 characters per line
ROWS = RES[1] // H        # 12 lines per screen

_HERE = os.path.dirname(os.path.abspath(__file__))
_GLYPH_BYTES = W * H * BPP
_ROW_BYTES = W * BPP
_MISSING_ROW = WHITE * W  # unknown characters show as a solid block

_fb = open(FB_DEVICE, "r+b")
_mem = mmap(_fb.fileno(), BPP * FULL)

with open(os.path.join(_HERE, "font32.bin"), "rb") as _f:
    _font = _f.read()

_blank_screen = None


def reset_screen():
    """Put the framebuffer into the mode we expect and pan to the first buffer.

    Resolution specific, copied from Z3apps: the stock display is set up with a
    double-height virtual buffer, so without the pan our writes land off-screen.
    """
    ioctl(_fb, 0x4601, b'\x80\x02\x00\x00\xe0\x01\x00\x00\x80\x02\x00\x00\xc0\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00 \x00\x00\x00\x00\x00\x00\x00\x10\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00\x08\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00\x18\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x80\x00\x00\x00^\x00\x00\x00\x96\x00\x00\x00\x00\x00\x00\x00\xc2\xa2\x00\x00\x1a\x00\x00\x00T\x00\x00\x00\x0c\x00\x00\x00\x1e\x00\x00\x00\x14\x00\x00\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
    ioctl(_fb, 0x4611, 0)


def clear(colour=BLACK):
    global _blank_screen
    if colour == BLACK:
        if _blank_screen is None:
            _blank_screen = BLACK * FULL
        fill = _blank_screen
    else:
        fill = colour * FULL
    _mem.seek(0)
    _mem.write(fill)


def _glyph_row(char, row):
    index = ord(char) - FIRST
    if 0 <= index < GLYPHS:
        start = index * _GLYPH_BYTES + row * _ROW_BYTES
        return _font[start:start + _ROW_BYTES]
    return _MISSING_ROW


def draw_text(text, pos=(0, 0)):
    """Draw text at pos (x, y) in pixels, hard-wrapping to the screen width.

    Empty lines are kept as blank rows, so callers can space things out.
    """
    x, y = pos
    max_chars = max((RES[0] - x) // W, 1)
    lines = []
    for line in text.split("\n"):
        lines.extend([line[i:i + max_chars] for i in range(0, len(line), max_chars)] or [""])

    for index, line in enumerate(lines):
        top = y + index * H
        if top >= RES[1]:
            break
        for row in range(min(H, RES[1] - top)):
            data = b"".join(_glyph_row(char, row) for char in line)
            if not data:
                continue
            _mem.seek(((top + row) * RES[0] + x) * BPP)
            _mem.write(data[:(RES[0] - x) * BPP])


reset_screen()
