# Z3pad
Turns an RG35XX into a gamepad for your PC

Run it, connect the handheld to a computer by USB or Bluetooth, and its own buttons become a normal 16-button HID gamepad. Quit and the device goes straight back to normal.

Tested on the RG35XX Plus, on both the stock Anbernic firmware and [cbepx-me's StockOS Modification](https://github.com/cbepx-me/RG35XX-Plus-Stock-OS-Modification). It needs no packages of any kind: everything it uses is already on the stock image, and the on-screen menu is drawn with nothing but the Python standard library.

## Installation/Updating The App

There are two ways to do this. Both end up with a **Z3pad** entry in your APPS menu.

### With WiFi

1. Copy the [install-Z3pad.sh](https://github.com/Z3R0C1PH3R/Z3pad/releases/latest/download/install-Z3pad.sh) file into your Roms/APPS folder and run it from the APPS menu, after making sure the **WIFI is connected** and the **correct time** is set in settings.
2. After a minute your device will restart, which means the install/update was successful, and you may remove the install-Z3pad.sh file. If it doesnt restart and just exits then the install failed, check `Z3pad-install-logfile.txt` in the APPS folder.

Run the same script again any time to update to the latest version. Your button calibration is kept.

### By hand, no WiFi needed

1. Download the repository ([zip](https://github.com/Z3R0C1PH3R/Z3pad/archive/refs/heads/main.zip)) and unpack it on your computer.
2. Copy the `Z3pad` folder and the `Z3pad.sh` file into your Roms/APPS folder, so that you end up with `Roms/APPS/Z3pad.sh` and `Roms/APPS/Z3pad/`.
3. Put the card back in and restart the device.

Thats the whole install, there is nothing to download or compile. To update later, replace the `Z3pad` folder with a newer one, but keep your `Z3pad/padmap.json` if you have calibrated your buttons.

## Usage

1. Start the Z3pad app from the APPS menu. A menu appears, use the Dpad to move and A to select.

| Menu entry        | What it does                                                    |
|-------------------|-----------------------------------------------------------------|
| USB gamepad       | Connect to a PC with a USB-C **data** cable, works immediately   |
| Bluetooth gamepad | Pair from the PC's Bluetooth settings, appears as "Z3pad"        |
| Calibrate buttons | Press each button when asked, to record your unit's layout       |
| Diagnostics       | Shows whether your kernel and Bluetooth stack can do this        |

2. Pick USB gamepad or Bluetooth gamepad. The screen tells you when the PC has connected.
3. Every button is now sent to the PC, so **MENU is the only way out**. Hold it for a second to quit and put the device back to normal.
4. Test it at [hardwaretester.com/gamepad](https://hardwaretester.com/gamepad).

While a gamepad mode is running your buttons map like this:

| Handheld        | PC gamepad                     |
|-----------------|--------------------------------|
| A B X Y         | Face buttons 1-4               |
| L1 R1 L2 R2     | Shoulders and triggers         |
| SELECT START    | Select and Start               |
| Dpad            | Both a hat switch and the left stick |
| Volume -/+      | Left and right stick clicks    |
| MENU tap        | Guide button                   |
| MENU hold 1s    | Quit and restore the device    |

NOTE: If some buttons come out in the wrong place on the PC, run **Calibrate buttons** once. It saves your layout to `Z3pad/padmap.json`, which then overrides the defaults and survives updates.

## Diagnostics

The Diagnostics entry reports whether your kernel can do this at all. The first two lines are the answer, the list below them is only for troubleshooting. Use the Dpad to page and B to go back.

**A `usb_f_hid` WARN and a `native USB HID` FAIL are normal and do not mean anything is broken.** These handhelds ship without the `usb_f_hid` kernel module, so the usual `/dev/hidg0` route does not exist. Z3pad builds the USB gamepad on FunctionFS instead, which needs no kernel changes and works fine. As long as the top line says `USB gamepad: works`, you are good.

For the full, wordy version of the same report, run `Z3pad/hiddiag.py` over SSH.

## How It Works

1. **USB** uses **FunctionFS** rather than the usual `hid.*` gadget function, because the 4.9.170 kernel on these devices has no `usb_f_hid`. `hidgadget.py` creates an `ffs.*` gadget, writes the USB and HID report descriptors from userspace, answers the host's control requests itself, and pushes reports down the interrupt endpoint. Nothing needs installing on the PC.
2. **Bluetooth** publishes an HID SDP record through BlueZ's D-Bus `ProfileManager1` and serves the HID control (PSM 17) and interrupt (PSM 19) L2CAP channels. BlueZ's own `input` plugin claims the HID UUID, so if registration is refused, `bthid.py` restarts `bluetoothd` with `--noplugin=input` and puts it back exactly as it was on exit. This means the handheld cannot use its own Bluetooth controllers while Bluetooth gamepad mode is running.
3. Buttons are read from the handheld's evdev node and grabbed with `EVIOCGRAB`, so presses go to the PC instead of leaking into the launcher underneath.
4. Only one instance can run at a time (`flock`), because two gadgets fighting over the same USB controller leaves it in a broken state.
5. The screen is drawn by `display.py`, a standard-library port of the [Z3apps](https://github.com/Z3R0C1PH3R/Z3apps) framebuffer code. Z3apps uses numpy for this, but a 34 MB dependency just to draw text would mean no offline install, so this copies glyph rows out of `font32.bin` straight into an `mmap` of `/dev/fb0`. The output is pixel identical.

## Known Issues

1. Bluetooth pairing has to restart `bluetoothd`, so any Bluetooth controllers paired to the handheld itself will disconnect while Bluetooth gamepad mode is running. They come back when you quit.
2. The Dpad is reported as both a hat switch and the left stick, because different PC games read only one or the other. A few games will see this as the stick drifting when you use the Dpad.

## Credits

The framebuffer display code and `font32.bin` come from [Z3apps](https://github.com/Z3R0C1PH3R/Z3apps). Licensed GPL-3.0.

##### If you like my work and want to say thanks, or encourage me to do more, you can [buy me a coffee](https://buymeacoffee.com/z3r0c1ph3r) or a [ko-fi!](https://ko-fi.com/z3r0c1ph3r)
