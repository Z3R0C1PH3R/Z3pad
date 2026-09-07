"""Read the handheld's own buttons and turn them into HID gamepad reports.

The RG35XX Plus exposes its pad as "ANBERNIC-keys" (/dev/input/event1): the face
and shoulder buttons are EV_KEY, the d-pad is EV_ABS on ABS_HAT0X/ABS_HAT0Y, and
there are no analog sticks. Key codes here were confirmed against the driver's
advertised capability bitmap, since the vendor does not use the conventional
BTN_* assignments (START and SELECT sit on BTN_TR and BTN_TL, for instance).
"""

import fcntl
import json
import os
import select
import time
import struct

# struct input_event on 64-bit: two 8-byte timeval fields, then type/code/value.
EVENT_FORMAT = "<qqHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
ABS_HAT0X = 0x10
ABS_HAT0Y = 0x11

EVIOCGRAB = 0x40044590

# Which bit of the report each pad button sets, i.e. HID button number minus one.
#
# The numbering is not sequential on purpose. Linux turns HID button N into
# BTN_GAMEPAD + N - 1, and hosts then place buttons by that BTN_* name, so button
# 3 becomes the meaningless BTN_C while button 5 becomes BTN_WEST, the "X" slot.
# Picking the numbers below is what makes each press arrive where the PC expects
# it; laying them out 1, 2, 3, 4... scatters them instead.
BUTTON_SLOTS = {
    "A": 0,        # button 1  BTN_SOUTH   pad slot 0
    "B": 1,        # button 2  BTN_EAST    pad slot 1
    "Y": 3,        # button 4  BTN_NORTH   pad slot 3
    "X": 4,        # button 5  BTN_WEST    pad slot 2
    "L1": 6,       # button 7  BTN_TL      left shoulder
    "R1": 7,       # button 8  BTN_TR      right shoulder
    "L2": 8,       # button 9  BTN_TL2     left trigger
    "R2": 9,       # button 10 BTN_TR2     right trigger
    "SELECT": 10,  # button 11 BTN_SELECT  select / back
    "START": 11,   # button 12 BTN_START   start
    "FN": 13,      # button 14 BTN_THUMBL  left stick click
    "VOL-": 14,    # button 15 BTN_THUMBR  right stick click
    "VOL+": 15,    # button 16
}
MENU_BIT = 12  # button 13, BTN_MODE: the guide button a short MENU tap sends

# Fallback evdev codes for the RG35XX Plus, matching this repo's buttoninput.py.
# The vendor does not follow the usual BTN_* meanings: X and Y sit on BTN_NORTH
# and BTN_C, while L1 and R1 take BTN_WEST and BTN_Z. Other handhelds in the
# family differ again, so padcalib.py can override all of these.
DEFAULT_CODES = {
    "A": 304, "B": 305, "X": 307, "Y": 306,
    "L1": 308, "R1": 309, "L2": 314, "R2": 315,
    "SELECT": 310, "START": 311, "MENU": 312, "FN": 354,
    "VOL-": 114, "VOL+": 115,
}
MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "padmap.json")

EXIT_HOLD_SECONDS = 1.0

AXIS_MAX = 127
HAT_CENTER = 8
# (x, y) from ABS_HAT0X/ABS_HAT0Y -> the hat's clockwise-from-north encoding.
HAT_VALUES = {
    (0, -1): 0, (1, -1): 1, (1, 0): 2, (1, 1): 3,
    (0, 1): 4, (-1, 1): 5, (-1, 0): 6, (-1, -1): 7,
}


def load_codes():
    """Button name -> evdev code, preferring a calibration produced by padcalib."""
    codes = dict(DEFAULT_CODES)
    try:
        with open(MAP_FILE) as f:
            saved = json.load(f)
        codes.update({k: int(v) for k, v in saved.items() if isinstance(v, int)})
    except (OSError, ValueError):
        pass
    return codes


def find_joypad():
    """Locate the evdev node carrying the handheld's buttons and d-pad."""
    blocks = ""
    try:
        with open("/proc/bus/input/devices") as f:
            blocks = f.read()
    except OSError:
        pass
    candidates = []
    for block in blocks.split("\n\n"):
        name = handlers = ""
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1]
        events = [h for h in handlers.split() if h.startswith("event")]
        if not events:
            continue
        if "js" in handlers or any(
            k in name.lower() for k in ("anbernic", "retrogame", "joypad", "gamepad")
        ):
            candidates.append("/dev/input/" + events[0])
    if not candidates:
        raise RuntimeError("no joypad evdev node found under /dev/input")
    return candidates[0]


class PadReader:
    """Turns evdev traffic into 4-byte HID reports.

    While this is open the joypad is grabbed exclusively, so the stock Anbernic
    frontend running behind us does not also react to every press.
    """

    def __init__(self, device=None, grab=True):
        self.path = device or find_joypad()
        self.codes = load_codes()
        self.menu_key = self.codes.get("MENU")
        self.button_bits = {self.codes[name]: bit
                            for name, bit in BUTTON_SLOTS.items()
                            if name in self.codes}
        self.names = {code: name for name, code in self.codes.items()}
        self.fd = os.open(self.path, os.O_RDONLY)
        self.grabbed = False
        if grab:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 1)
                self.grabbed = True
            except OSError:
                pass  # not fatal, the frontend will just see the presses too
        self.buttons = 0
        self.hat_x = 0
        self.hat_y = 0
        self.menu_down_at = None
        self.exit_requested = False
        self.pending_tap = None

    def close(self):
        if self.fd is not None:
            if self.grabbed:
                try:
                    fcntl.ioctl(self.fd, EVIOCGRAB, 0)
                except OSError:
                    pass
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def report(self, extra_bits=0):
        x = max(-1, min(1, self.hat_x))
        y = max(-1, min(1, self.hat_y))
        return struct.pack(
            "<HBbb",
            self.buttons | extra_bits,
            HAT_VALUES.get((x, y), HAT_CENTER),
            x * AXIS_MAX,
            y * AXIS_MAX,
        )

    def wait(self, timeout):
        """Block until input arrives. True if there is something to read."""
        readable, _, _ = select.select([self.fd], [], [], timeout)
        return bool(readable)

    def poll(self, timeout=0.5):
        """Consume pending events. Returns a report if the pad state changed.

        MENU is not forwarded directly: a short tap is reported as the guide
        button, while holding it for EXIT_HOLD_SECONDS asks the app to quit.
        """
        if not self.wait(timeout):
            return None
        try:
            data = os.read(self.fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return None
        except OSError:
            self.exit_requested = True
            return None

        changed = False
        for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            sec, usec, etype, code, value = struct.unpack(EVENT_FORMAT, data[i:i + EVENT_SIZE])
            now = sec + usec / 1e6
            if etype == EV_KEY:
                if value == 2:
                    continue  # autorepeat, the state already says it is held
                if code == self.menu_key:
                    changed |= self._handle_menu(value, now)
                elif code in self.button_bits:
                    bit = 1 << self.button_bits[code]
                    before = self.buttons
                    self.buttons = self.buttons | bit if value else self.buttons & ~bit
                    changed |= self.buttons != before
            elif etype == EV_ABS:
                if code == ABS_HAT0X and value != self.hat_x:
                    self.hat_x, changed = value, True
                elif code == ABS_HAT0Y and value != self.hat_y:
                    self.hat_y, changed = value, True
            elif etype == EV_SYN:
                continue

        return self.report() if changed else None

    def _handle_menu(self, value, now):
        if value:
            self.menu_down_at = now
            return False
        held = now - (self.menu_down_at or now)
        self.menu_down_at = None
        if held >= EXIT_HOLD_SECONDS:
            self.exit_requested = True
            return False
        self.pending_tap = 1 << MENU_BIT
        return True

    def take_tap(self):
        """A one-shot button pulse that must be sent then released."""
        tap, self.pending_tap = self.pending_tap, None
        return tap

    def pressed_names(self):
        held = [n for c, n in self.names.items()
                if c in self.button_bits and self.buttons & (1 << self.button_bits[c])]
        if self.hat_x:
            held.append("RIGHT" if self.hat_x > 0 else "LEFT")
        if self.hat_y:
            held.append("DOWN" if self.hat_y > 0 else "UP")
        return held


DPAD_NAMES = {
    (ABS_HAT0X, 1): "RIGHT", (ABS_HAT0X, -1): "LEFT",
    (ABS_HAT0Y, 1): "DOWN", (ABS_HAT0Y, -1): "UP",
}


class ButtonEvents:
    """Discrete presses by name, for menus rather than HID reports.

    Uses the same calibrated mapping as PadReader, so a menu answers to whatever
    the pad really sends. Only presses are reported, never releases.
    """

    def __init__(self, device=None, grab=True):
        self.path = device or find_joypad()
        self.names = {code: name for name, code in load_codes().items()}
        self.fd = os.open(self.path, os.O_RDONLY)
        self.grabbed = False
        if grab:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 1)
                self.grabbed = True
            except OSError:
                pass
        self._queue = []

    def close(self):
        if self.fd is not None:
            if self.grabbed:
                try:
                    fcntl.ioctl(self.fd, EVIOCGRAB, 0)
                except OSError:
                    pass
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def next(self, timeout=None):
        """The name of the next button pressed, or None if timeout elapses."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._queue:
                return self._queue.pop(0)
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0.0:
                return None
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                return None
            try:
                data = os.read(self.fd, EVENT_SIZE * 64)
            except OSError:
                return None
            for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack(
                    EVENT_FORMAT, data[i:i + EVENT_SIZE]
                )
                if etype == EV_KEY and value == 1 and code in self.names:
                    self._queue.append(self.names[code])
                elif etype == EV_ABS and value:
                    name = DPAD_NAMES.get((code, max(-1, min(1, value))))
                    if name:
                        self._queue.append(name)


if __name__ == "__main__":
    # Mapping check: prints the pad state and the report bytes as buttons are
    # pressed, without grabbing the device or touching USB.
    import sys

    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    with PadReader(grab=False) as pad:
        print(f"reading {pad.path} for {seconds:.0f}s, press some buttons")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not pad.exit_requested:
            report = pad.poll(timeout=0.3)
            tap = pad.take_tap()
            if tap:
                print("MENU tap -> guide button")
            if report:
                print(f"{report.hex()}  {' '.join(pad.pressed_names()) or '(none)'}")
        if pad.exit_requested:
            print("MENU held -> quit requested")
