# Z3pad

Turn an Anbernic RG35XX Plus into a USB or Bluetooth gamepad for your PC.

Run it, plug the handheld into a computer (or pair it over Bluetooth), and its
own buttons become a standard 16-button HID gamepad. Quit and the device goes
back to behaving normally.

Built for [cbepx-me's StockOS Modification](https://github.com/cbepx-me/RG35XX-Plus-Stock-OS-Modification)
on the RG35XX Plus/H/2024/SP (H700). It should work on any rooted Linux handheld
whose kernel has USB gadget FunctionFS or a working Bluetooth stack.

## Install

Z3pad needs no packages at all. Everything it uses — python3, `dbus`, `gi` — is
already on the stock modified image, and the framebuffer drawing is pure
standard library, so there is nothing to download and no internet required.

**Offline (recommended):** copy the `Z3pad` folder and `Z3pad.sh` into your
`Roms/APPS` folder, then restart the device. That's the whole install.

**From GitHub:** copy `install-Z3pad.sh` into `Roms/APPS` and run it from the
APPS menu with WiFi connected and the clock set correctly (a wrong clock breaks
HTTPS). It downloads the rest, reports what your kernel supports, and reboots.
If it exits without rebooting the install failed — read
`Z3pad-install-logfile.txt` next to the script. The same script also works
offline: if a `Z3pad` folder is already sitting beside it, it skips the download.

Reinstalling keeps your calibration (`Z3pad/padmap.json`).

## Usage

Launch **Z3pad** from the APPS menu and pick a mode with the D-pad and A:

| Mode              | What it does                                                  |
|-------------------|---------------------------------------------------------------|
| USB gamepad       | Plug into a PC by cable and it appears as a wired gamepad      |
| Bluetooth gamepad | Pair from the PC's Bluetooth settings as "Z3pad"               |
| Calibrate buttons | Press each button in turn to record your device's button codes |
| Diagnostics       | Report which HID transports this kernel supports               |

While a gamepad mode is running, every button is forwarded to the PC, so **MENU
is the only way out** — hold it to quit and restore the device.

Test it at [hardwaretester.com/gamepad](https://hardwaretester.com/gamepad).

### Button mapping

| Handheld | PC gamepad          |
|----------|---------------------|
| A        | Button 1 (A / cross)|
| B        | Button 2 (B / circle)|
| X        | Button 4 (X / square)|
| Y        | Button 3 (Y / triangle)|
| L1 / R1  | Shoulders           |
| L2 / R2  | Triggers            |
| SELECT   | Back / Select       |
| START    | Start               |
| FN       | Guide / Home        |
| D-pad    | Hat switch and X/Y axes |

If your unit reports different codes, run **Calibrate buttons** once. It writes
`Z3pad/padmap.json`, which overrides the defaults and survives reinstalls.

## How it works

- **USB** goes through **FunctionFS**, not the usual `hid.*` gadget function.
  The stock 4.9.170 kernel has no `usb_f_hid`, so `/dev/hidg0` does not exist.
  Instead `hidgadget.py` creates an `ffs.*` gadget, writes the USB descriptors
  and the HID report descriptor from userspace, answers the host's control
  requests itself, and pushes reports down the interrupt endpoint.
- **Bluetooth** publishes an HID SDP record through BlueZ's D-Bus
  `ProfileManager1` and serves the HID control (PSM 17) and interrupt (PSM 19)
  L2CAP channels. BlueZ's own `input` plugin claims the HID UUID, so if
  registration is refused, `bthid.py` restarts `bluetoothd` with
  `--noplugin=input` and puts it back exactly as it was on exit.
- Buttons are read straight from the handheld's evdev node, grabbed with
  `EVIOCGRAB` so presses go to the PC instead of the launcher underneath.
- Only one instance can run at a time (`flock`), because two gadgets fighting
  over the same UDC leaves USB in a broken state.

- The screen is drawn by `display.py`, a stdlib-only port of the Z3apps
  framebuffer code. Z3apps uses numpy, but a 34 MB dependency to blit text is
  not worth an online-only install, so this copies glyph rows out of
  `font32.bin` into an `mmap` of `/dev/fb0` instead. Output is pixel-identical.

Bluetooth mode needs `python3-dbus` and `python3-gi`, both of which are already
installed on the stock modified image. Nothing else is required.

Run `Z3pad/hiddiag.py` on its own over SSH for the full, verbose support report.

## Credits

The framebuffer display code (`display.py`, `font32.bin`) comes from
[Z3apps](https://github.com/Z3R0C1PH3R/Z3apps). Licensed GPL-3.0.
