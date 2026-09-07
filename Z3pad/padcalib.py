#!/usr/bin/env python3
"""Record which evdev code each physical button on this handheld sends.

Anbernic's driver does not follow the usual BTN_* conventions and the mapping
differs across the RG35XX family, so guessing produces a gamepad whose buttons
land in the wrong places on the PC. This asks for each button by name on the
handheld's own screen and writes the answers to padmap.json, which padreader
then loads instead of its built-in defaults.
"""

import json
import os
import struct
import sys
import time

import padreader

# MENU comes early so the remaining, possibly absent, buttons can be skipped.
PROMPTS = [
    ("A", True), ("B", True), ("X", True), ("Y", True),
    ("L1", True), ("R1", True), ("L2", True), ("R2", True),
    ("SELECT", True), ("START", True), ("MENU", True),
    ("FN", False), ("VOL-", False), ("VOL+", False),
]
IDLE_TIMEOUT = 45.0


class Prompt:
    """Draws to the framebuffer when available, otherwise just prints."""

    def __init__(self):
        try:
            import display
            self.display = display
        except Exception as e:
            print(f"framebuffer unavailable ({e})", file=sys.stderr)
            self.display = None

    def show(self, title, lines):
        print(title + " | " + " / ".join(lines), file=sys.stderr)
        if not self.display:
            return
        d = self.display
        try:
            d.clear()
            d.draw_text(title + "\n\n" + "\n".join(lines))
        except Exception:
            self.display = None

    def clear(self):
        if self.display:
            try:
                self.display.clear()
            except Exception:
                pass


def next_press(fd, ignore, deadline):
    """Wait for a key-down whose code is not already claimed."""
    import select
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.3)
        if not readable:
            continue
        data = os.read(fd, padreader.EVENT_SIZE * 64)
        for i in range(0, len(data) - padreader.EVENT_SIZE + 1, padreader.EVENT_SIZE):
            _, _, etype, code, value = struct.unpack(
                padreader.EVENT_FORMAT, data[i:i + padreader.EVENT_SIZE]
            )
            if etype == padreader.EV_KEY and value == 1 and code not in ignore:
                return code
    return None


def drain_release(fd, code, deadline):
    """Wait for the button to come back up so it is not read twice."""
    import select
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.3)
        if not readable:
            return
        data = os.read(fd, padreader.EVENT_SIZE * 64)
        for i in range(0, len(data) - padreader.EVENT_SIZE + 1, padreader.EVENT_SIZE):
            _, _, etype, ecode, value = struct.unpack(
                padreader.EVENT_FORMAT, data[i:i + padreader.EVENT_SIZE]
            )
            if etype == padreader.EV_KEY and ecode == code and value == 0:
                return


def main():
    if os.geteuid() != 0:
        print("calibration must run as root", file=sys.stderr)
        return 1

    prompt = Prompt()
    # Grab the pad so the stock frontend does not act on the presses.
    with padreader.PadReader(grab=True) as pad:
        codes = {}
        menu_code = None
        prompt.show("Button setup", [
            "Press each button as",
            "it is named.",
            "",
            "Starting...",
        ])
        time.sleep(1.5)

        for index, (name, required) in enumerate(PROMPTS, start=1):
            skip_hint = [] if required else ["", "or MENU to skip"]
            prompt.show(f"Press:  {name}", [
                f"({index} of {len(PROMPTS)})",
                *skip_hint,
            ])
            # Optional buttons are skipped with MENU, so MENU has to stay
            # readable even though it is already claimed.
            claimed = set(codes.values())
            if not required:
                claimed.discard(menu_code)
            deadline = time.monotonic() + IDLE_TIMEOUT
            code = next_press(pad.fd, claimed, deadline)
            if code is None:
                if required:
                    prompt.show("Button setup", ["Timed out.", "Nothing was saved."])
                    time.sleep(3)
                    prompt.clear()
                    return 1
                continue
            if not required and menu_code is not None and code == menu_code:
                prompt.show(f"Skipped {name}", [""])
                drain_release(pad.fd, code, time.monotonic() + 2)
                time.sleep(0.3)
                continue
            codes[name] = code
            if name == "MENU":
                menu_code = code
            prompt.show(f"{name} = {code}", ["", "release it"])
            drain_release(pad.fd, code, time.monotonic() + 3)
            time.sleep(0.2)

    with open(padreader.MAP_FILE, "w") as f:
        json.dump(codes, f, indent=2, sort_keys=True)
    print("saved " + padreader.MAP_FILE, file=sys.stderr)
    print(json.dumps(codes, indent=2, sort_keys=True))

    prompt.show("Button setup done", [
        f"{len(codes)} buttons saved.",
        "",
        "Controller mode will",
        "use this mapping.",
    ])
    time.sleep(4)
    prompt.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
