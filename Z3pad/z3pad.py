#!/usr/bin/env python3
"""Controller mode launcher: pick USB or Bluetooth, calibrate, or run diagnostics.

This is the single entry point for controller mode. Everything it offers can also
be run directly (controller.py, padcalib.py, hiddiag.py) if you prefer a shell.
"""

import os
import subprocess
import sys
import textwrap
import time

import padreader

HERE = os.path.dirname(os.path.abspath(__file__))

SCREEN_COLS = 30   # 640px wide at the 21px font display.py uses
SCREEN_ROWS = 10   # 480px tall at 38px per row, leaving room for a footer

# title, subtitle, command, whether to page its output on screen afterwards
MENU = [
    ("USB gamepad", "Plug into a PC by cable",
     ["controller.py", "--transport", "usb"], False),
    ("Bluetooth gamepad", "Pair with a PC wirelessly",
     ["controller.py", "--transport", "bluetooth"], False),
    ("Calibrate buttons", "Teach it your button layout", ["padcalib.py"], False),
    ("Diagnostics", "Check what this kernel supports", ["hiddiag.py", "--brief"], True),
    ("Exit", "", None, False),
]


class Screen:
    """Framebuffer menu rendering, with a plain-text fallback."""

    def __init__(self):
        try:
            import display
            self.display = display
        except Exception as e:
            print(f"framebuffer unavailable ({e})", file=sys.stderr)
            self.display = None

    def draw_menu(self, selected):
        lines = ["Controller mode", ""]
        for i, (title, _subtitle, _cmd, _capture) in enumerate(MENU):
            lines.append(("> " if i == selected else "  ") + title)
        lines.append("")
        lines.append(MENU[selected][1])
        lines.append("")
        lines.append("D-pad + A,  MENU quits")
        self.draw(lines)

    def draw(self, lines):
        text = "\n".join(lines)
        print(text.replace("\n", " | "), file=sys.stderr)
        if not self.display:
            return
        d = self.display
        try:
            d.clear()
            d.draw_text(text)
        except Exception:
            self.display = None

    def clear(self):
        if self.display:
            try:
                self.display.clear()
            except Exception:
                pass


def page_text(text, screen):
    """Show captured output on screen, a page at a time.

    Without this a text-only tool like hiddiag.py would print into the logfile
    and vanish before anyone could read it.
    """
    lines = []
    for raw in text.expandtabs(2).splitlines():
        lines.extend(textwrap.wrap(raw, SCREEN_COLS) or [""])
    pages = [lines[i:i + SCREEN_ROWS] for i in range(0, len(lines), SCREEN_ROWS)] or [[""]]

    page = 0
    with padreader.ButtonEvents(grab=True) as buttons:
        while True:
            screen.draw(pages[page] + [""] + [f"{page + 1}/{len(pages)}  D-pad, B=back"])
            pressed = buttons.next(timeout=None)
            if pressed in ("DOWN", "RIGHT", "R1"):
                page = min(page + 1, len(pages) - 1)
            elif pressed in ("UP", "LEFT", "L1"):
                page = max(page - 1, 0)
            elif pressed in ("B", "MENU", "A", "START"):
                return


def run(title, argv, capture, screen):
    """Run one of the sub-apps. The pad is already released so it can grab it."""
    screen.draw([title, "", "Starting..."])
    path = [sys.executable, os.path.join(HERE, argv[0])] + argv[1:]
    if capture:
        result = subprocess.run(path, cwd=HERE, capture_output=True, text=True)
        page_text((result.stdout + result.stderr).strip() or "(no output)", screen)
        return result.returncode

    result = subprocess.run(path, cwd=HERE)
    if result.returncode != 0:
        screen.draw([
            "That did not work.",
            "",
            f"{argv[0]} exited {result.returncode}.",
            "",
            "See the logfile in the",
            "APPS folder for details.",
        ])
        time.sleep(5)
    return result.returncode


def main():
    if os.geteuid() != 0:
        print("controller mode must run as root", file=sys.stderr)
        return 1

    screen = Screen()
    selected = 0
    while True:
        screen.draw_menu(selected)
        # The pad is only held while the menu is on screen, so each sub-app is
        # free to grab it exclusively.
        with padreader.ButtonEvents(grab=True) as buttons:
            choice = None
            while choice is None:
                pressed = buttons.next(timeout=None)
                if pressed in ("DOWN", "RIGHT"):
                    selected = (selected + 1) % len(MENU)
                    screen.draw_menu(selected)
                elif pressed in ("UP", "LEFT"):
                    selected = (selected - 1) % len(MENU)
                    screen.draw_menu(selected)
                elif pressed == "A":
                    choice = selected
                elif pressed in ("MENU", "B"):
                    choice = len(MENU) - 1  # Exit

        if MENU[choice][2] is None:
            screen.clear()
            return 0
        title, _subtitle, argv, capture = MENU[choice]
        run(title, argv, capture, screen)


if __name__ == "__main__":
    sys.exit(main())
