"""Which handheld are we running on?

The stock launcher's System Info screen reads /mnt/vendor/oem/board.ini, so that
file is the only thing on these devices that actually knows the model: an
RG35XX SP holds "RG35xxSP" and an RG35XX Plus holds "RG35xx+_P". Nothing else
can tell them apart, because every H700 handheld reports sun50iw9 as its device
tree model and ANBERNIC as its hostname.
"""

import os

BOARD_FILE = "/mnt/vendor/oem/board.ini"
FALLBACK = "Anbernic"
MAX_LEN = 40


def board(path=BOARD_FILE):
    """The model string, or "" when this is not an Anbernic handheld."""
    try:
        with open(path) as f:
            raw = f.read()
    except OSError:
        return ""
    # Stock writes the model with no trailing newline, but don't count on it,
    # and keep only what a host will happily show in a device name.
    return "".join(c for c in raw.strip() if c.isprintable())[:MAX_LEN]


def gamepad_name(path=BOARD_FILE):
    """What to call ourselves over USB and Bluetooth."""
    return f"{board(path) or FALLBACK} Gamepad"
