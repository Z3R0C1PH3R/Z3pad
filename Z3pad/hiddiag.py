#!/usr/bin/env python3
"""Standalone diagnostic: can this handheld present itself as a HID gamepad?

Checks, in order of preference, the three transports a controller mode could use:

  1. usb-hid   native USB HID gadget function (configfs hid.*, gives /dev/hidgN)
  2. usb-ffs   USB HID built by hand on top of FunctionFS (configfs ffs.*)
  3. bt-hid    Bluetooth classic HID device profile over L2CAP

Stdlib only, no imports from the rest of Z3apps, so it can be copied to a device
on its own. Run as root. Exits 0 if some transport is usable, 1 if none is.
"""

import os
import socket
import subprocess
import sys

CONFIGFS = "/sys/kernel/config"
GADGET_DIR = CONFIGFS + "/usb_gadget"
PROBE_NAME = "z3hiddiag"

RELEASE = os.uname().release

# --brief trades the explanations for something that fits a 30-column handheld
# screen, so the launcher can show results without endless paging.
BRIEF = False
RESULTS = []


def out(status, label, detail="", short=None):
    RESULTS.append((status, short or label))
    if not BRIEF:
        print(f"[{status:^4}] {label}" + (f": {detail}" if detail else ""))


def note(text):
    if not BRIEF:
        print(f"         note: {text}")


def say(text):
    if not BRIEF:
        print(text)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def module_state(name):
    """Where a kernel module lives: 'builtin', a .ko path, or None."""
    for base in ("/lib/modules", "/usr/lib/modules"):
        root = os.path.join(base, RELEASE)
        if name in read(os.path.join(root, "modules.builtin")):
            return "builtin"
        for dirpath, _, files in os.walk(root, followlinks=False):
            for fn in files:
                if fn.startswith(name + ".ko"):
                    return os.path.join(dirpath, fn)
    return None


def check_udc():
    """A USB device controller is required for any USB gadget."""
    try:
        udcs = sorted(os.listdir("/sys/class/udc"))
    except OSError:
        udcs = []
    if not udcs:
        out("FAIL", "USB device controller", "/sys/class/udc is empty; USB gadget mode impossible", short="USB controller")
        return None
    udc = udcs[0]
    state = read(f"/sys/class/udc/{udc}/state", "unknown")
    out("PASS", "USB device controller", f"{udc} (state: {state})", short="USB controller")
    if state == "not attached":
        note("no USB host is plugged in right now, which is expected while idle")
    return udc


def check_configfs():
    """configfs must be mounted for the gadget API to exist."""
    if "configfs" not in read("/proc/filesystems"):
        out("FAIL", "configfs", "not supported by this kernel")
        return False
    if not os.path.isdir(GADGET_DIR):
        try:
            subprocess.run(["mount", "-t", "configfs", "none", CONFIGFS], check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            out("FAIL", "configfs", f"could not mount at {CONFIGFS}: {e}")
            return False
    if not os.path.isdir(GADGET_DIR):
        out("FAIL", "configfs", f"mounted but {GADGET_DIR} is missing (no CONFIG_USB_CONFIGFS)")
        return False
    out("PASS", "configfs", f"usb_gadget API at {GADGET_DIR}")
    return True


def check_libcomposite():
    state = module_state("libcomposite")
    if state == "builtin":
        out("PASS", "libcomposite", "built into the kernel, no modprobe needed")
        return True
    if state:
        rc = subprocess.run(["modprobe", "libcomposite"], capture_output=True, text=True)
        if rc.returncode:
            out("FAIL", "libcomposite", f"module found but modprobe failed: {rc.stderr.strip()}")
            return False
        out("PASS", "libcomposite", f"loaded from {state}")
        return True
    out("FAIL", "libcomposite", "neither built in nor available as a module")
    return False


def probe_function(fn_name):
    """Ask configfs whether a gadget function type exists.

    configfs reports an unknown function type as ENOENT on mkdir, so creating a
    throwaway gadget and trying to make the function directory is the only
    reliable way to enumerate what this kernel actually supports.
    """
    gadget = os.path.join(GADGET_DIR, PROBE_NAME)
    fn_dir = os.path.join(gadget, "functions", fn_name)
    created_gadget = False
    try:
        if not os.path.isdir(gadget):
            os.mkdir(gadget)
            created_gadget = True
        try:
            os.mkdir(fn_dir)
        except FileExistsError:
            pass
        except OSError as e:
            return False, e.strerror
        return True, ""
    except OSError as e:
        return False, f"could not create probe gadget: {e.strerror}"
    finally:
        for path in (fn_dir, gadget if created_gadget else None):
            if path:
                try:
                    os.rmdir(path)
                except OSError:
                    pass


def check_hid_function():
    """The make-or-break check: a real USB HID gadget function."""
    state = module_state("usb_f_hid")
    if state == "builtin":
        out("PASS", "usb_f_hid", "built into the kernel")
    elif state:
        subprocess.run(["modprobe", "usb_f_hid"], capture_output=True)
        out("PASS", "usb_f_hid", f"module at {state}")
    else:
        out("WARN", "usb_f_hid", "not built in and no usb_f_hid.ko on disk")

    ok, err = probe_function("hid.diag")
    if ok:
        out("PASS", "HID gadget function", "configfs accepts hid.* -> /dev/hidgN available", short="native USB HID")
    else:
        out("FAIL", "HID gadget function", f"configfs rejects hid.* ({err})", short="native USB HID")
    return ok


def check_ffs_function():
    """FunctionFS lets userspace implement HID without usb_f_hid."""
    ok, err = probe_function("ffs.diag")
    if ok:
        out("PASS", "FunctionFS function", "configfs accepts ffs.* -> HID can be built in userspace", short="FunctionFS")
    else:
        out("FAIL", "FunctionFS function", f"configfs rejects ffs.* ({err})", short="FunctionFS")
    return ok


def check_joypad():
    """Find the evdev node for the handheld's own buttons."""
    blocks = read("/proc/bus/input/devices").split("\n\n")
    found = []
    for block in blocks:
        name = handlers = ""
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1]
        events = [h for h in handlers.split() if h.startswith("event")]
        if not events:
            continue
        # The joypad is the node carrying both buttons and the d-pad; on stock
        # Anbernic that is "ANBERNIC-keys", on other builds "retrogame_joypad".
        is_pad = "js" in handlers or any(
            k in name.lower() for k in ("anbernic", "retrogame", "joypad", "gamepad")
        )
        if is_pad:
            found.append((name, "/dev/input/" + events[0]))
    if not found:
        out("FAIL", "handheld joypad", "no evdev node looks like a gamepad", short="joypad")
        return None
    name, dev = found[0]
    out("PASS", "handheld joypad", f'"{name}" at {dev}', short="joypad")
    for name, dev in found[1:]:
        say(f'         also: "{name}" at {dev}')
    return dev


def check_bluetooth():
    """Bluetooth classic HID device role, the wireless alternative to USB."""
    try:
        adapters = sorted(os.listdir("/sys/class/bluetooth"))
    except OSError:
        adapters = []
    if not adapters:
        out("FAIL", "Bluetooth adapter", "no adapter under /sys/class/bluetooth", short="BT adapter")
        return False
    if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_L2CAP"):
        out("FAIL", "Bluetooth L2CAP", "this python3 was built without AF_BLUETOOTH support", short="BT L2CAP")
        return False
    if "L2CAP" not in read("/proc/net/protocols"):
        out("FAIL", "Bluetooth L2CAP", "L2CAP not registered by the kernel", short="BT L2CAP")
        return False
    rc = subprocess.run(["systemctl", "is-active", "bluetooth"], capture_output=True, text=True)
    daemon = rc.stdout.strip() or "unknown"
    out("PASS", "Bluetooth adapter", f"{adapters[0]}, L2CAP present, bluetoothd {daemon}", short="BT adapter")
    if daemon != "active":
        note("bluetoothd must be running to publish the HID SDP record")

    # Taking the HID device role needs D-Bus to talk to BlueZ.
    missing = [pkg for mod, pkg in (("dbus", "python3-dbus"), ("gi", "python3-gi"))
               if not _importable(mod)]
    if missing:
        out("WARN", "Bluetooth python modules",
            f"missing, install with: apt install {' '.join(missing)}", short="BT py modules")
        return False
    out("PASS", "Bluetooth python modules", "dbus and gi both present", short="BT py modules")
    return True


def _importable(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def print_brief(usb, bt, joypad):
    print(f"kernel {RELEASE}")
    print("")
    for status, label in RESULTS:
        print(f"{label[:16]:<17}{status}")
    print("")
    print(f"USB gamepad:  {'works' if usb and joypad else 'no'}")
    print(f"BT gamepad:   {'works' if bt and joypad else 'no'}")
    if not joypad:
        print("")
        print("No joypad found, so there")
        print("is nothing to read.")


def main():
    global BRIEF
    BRIEF = "--brief" in sys.argv

    if os.geteuid() != 0:
        print("This diagnostic must run as root.")
        return 1

    say(f"Z3 controller mode diagnostic on kernel {RELEASE} ({os.uname().machine})\n")

    say("-- USB gadget prerequisites --")
    udc = check_udc()
    configfs = check_configfs() if udc else False
    composite = check_libcomposite() if configfs else False

    say("\n-- HID transports --")
    hid = ffs = False
    if composite:
        hid = check_hid_function()
        if not hid:
            ffs = check_ffs_function()
    bt = check_bluetooth()

    say("\n-- Input source --")
    joypad = check_joypad()

    if BRIEF:
        print_brief(hid or ffs, bt, joypad)
        return 0 if (hid or ffs or bt) and joypad else 1

    print("\n-- Verdict --")
    if hid:
        transport = "usb-hid"
        print("Native USB HID gadget is supported. Create a gadget with a hid.* function")
        print("and write reports to /dev/hidg0.")
    elif ffs:
        transport = "usb-ffs"
        print("No usb_f_hid in this kernel, so /dev/hidg0 is NOT available.")
        print("FunctionFS is present though, so the HID interface, its report descriptor")
        print("and the interrupt endpoint can all be driven from userspace instead.")
    elif bt:
        transport = "bt-hid"
        print("No usable USB HID path. Bluetooth is available, so the device can instead")
        print("pair with the PC as a wireless HID gamepad.")
    else:
        print("UNSUPPORTED: this kernel offers no HID gadget function, no FunctionFS and")
        print("no usable Bluetooth stack. Controller mode would need a rebuilt kernel with")
        print("CONFIG_USB_CONFIGFS_F_HID=y (or =m) before it can work.")
        return 1

    if bt and transport != "bt-hid":
        print("Bluetooth HID is also available as a wireless alternative.")
    if not joypad:
        print("WARNING: no joypad evdev node found, so there is nothing to read buttons from.")
        return 1

    print(f"\ntransport={transport} joypad={joypad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
