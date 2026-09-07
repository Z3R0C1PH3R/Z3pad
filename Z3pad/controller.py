#!/usr/bin/env python3
"""Controller mode: use the handheld as a HID gamepad for a PC.

Two transports, same gamepad either way:

  usb        plug into a PC with a USB-C data cable, appears instantly
  bluetooth  pair with the PC wirelessly, no cable needed

Either way the handheld's own buttons drive it and everything is torn down on
exit, so USB goes back to normal and Bluetooth can pair with controllers again.

Hold MENU for a second to quit; a short MENU tap is sent to the PC as the guide
button.
"""

import argparse
import os
import signal
import sys
import time

import padreader

HELP_TEXT = """\
A B X Y  L1 R1 L2 R2
SELECT START  D-pad
VOL -/+  FN

MENU tap  = guide button
MENU hold = quit"""

WAITING_TEXT = {
    "usb": [
        "Waiting for a PC...",
        "",
        "Connect a USB-C data",
        "cable to your PC.",
    ],
    "bluetooth": [
        "Waiting to pair...",
        "",
        "On your PC, add a",
        "Bluetooth device and",
        "pick this handheld.",
    ],
}


def make_transport(name, log):
    """Import lazily so a broken Bluetooth stack cannot break USB mode."""
    if name == "bluetooth":
        import bthid
        return bthid.BluetoothHidGamepad(log=log)
    import hidgadget
    return hidgadget.UsbHidGamepad(log=log)


class Screen:
    """Optional framebuffer output; harmless if the display cannot be opened."""

    def __init__(self, enabled=True):
        self.display = None
        if not enabled:
            return
        try:
            import display
            self.display = display
        except Exception as e:
            print(f"framebuffer unavailable ({e}), running headless", file=sys.stderr)

    def show(self, title, lines):
        if not self.display:
            return
        d = self.display
        try:
            d.clear()
            d.draw_text(title + "\n\n" + "\n".join(lines))
        except Exception:
            self.display = None

    def clear(self):
        if not self.display:
            return
        try:
            self.display.clear()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Expose this handheld as a HID gamepad")
    ap.add_argument("--transport", choices=("usb", "bluetooth"), default="usb",
                    help="how to reach the PC (default: usb)")
    ap.add_argument("--device", help="joypad evdev node (default: autodetect)")
    ap.add_argument("--no-grab", action="store_true",
                    help="let the stock frontend also see button presses")
    ap.add_argument("--no-display", action="store_true", help="do not draw to the framebuffer")
    ap.add_argument("--verbose", action="store_true", help="log USB events to stderr")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("controller mode must run as root", file=sys.stderr)
        return 1

    screen = Screen(not args.no_display)
    messages = []

    def log(msg):
        messages.append(msg)
        if args.verbose:
            print(msg, file=sys.stderr)

    stopping = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.append(True))

    starting = "Starting USB gadget..." if args.transport == "usb" else "Starting Bluetooth..."
    screen.show("Controller mode", [starting])
    try:
        gadget_ctx = make_transport(args.transport, log)
    except Exception as e:
        screen.show("Controller mode", ["FAILED", str(e)[:60]])
        print(f"cannot start {args.transport} transport: {e}", file=sys.stderr)
        return 1

    try:
        with gadget_ctx as gadget, padreader.PadReader(args.device, grab=not args.no_grab) as pad:
            log(f"reading {pad.path} (grabbed: {pad.grabbed})")
            connected = None
            gadget.send_report(pad.report())

            while not pad.exit_requested and not stopping:
                now_connected = gadget.enabled.is_set()
                if now_connected != connected:
                    connected = now_connected
                    if connected:
                        screen.show("Controller mode: CONNECTED", HELP_TEXT.split("\n"))
                        gadget.send_report(pad.report())
                    else:
                        screen.show("Controller mode",
                                    WAITING_TEXT[args.transport] + ["", "MENU hold = quit"])

                report = pad.poll(timeout=0.2)
                tap = pad.take_tap()
                if tap is not None:
                    gadget.send_report(pad.report(tap))
                    time.sleep(0.04)
                    gadget.send_report(pad.report())
                elif report is not None:
                    gadget.send_report(report)

            log("exiting, restoring USB")
            screen.show("Controller mode", ["Shutting down..."])
    except Exception as e:
        screen.show("Controller mode", ["ERROR", str(e)[:60]])
        print(f"controller mode failed: {e}", file=sys.stderr)
        for msg in messages[-10:]:
            print("  " + msg, file=sys.stderr)
        time.sleep(3)
        return 1

    screen.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
