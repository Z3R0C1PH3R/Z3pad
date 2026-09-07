"""USB HID gamepad gadget for kernels without usb_f_hid.

The RG35XX Plus stock kernel (4.9.170) has libcomposite and configfs built in
but no HID gadget function, so /dev/hidg0 can never appear. FunctionFS is
present though, and it hands the raw USB interface to userspace: we supply the
interface, HID class and endpoint descriptors ourselves, answer the host's HID
control requests on ep0, and push input reports down the interrupt endpoint.
That gives a real USB gamepad with no kernel changes.
"""

import fcntl
import os
import select
import struct
import subprocess
import threading
import time

from hidreport import (  # noqa: F401  re-exported for callers of this module
    HAT_CENTER,
    HID_DESCRIPTOR,
    HID_DT_HID,
    HID_DT_REPORT,
    IDLE_REPORT,
    REPORT_DESC,
    REPORT_LEN,
)

CONFIGFS = "/sys/kernel/config/usb_gadget"
GADGET = "z3pad"
FFS_NAME = "z3pad"
FFS_MOUNT = "/dev/ffs-" + FFS_NAME
LOCK_FILE = "/run/z3pad.lock"

# include/uapi/linux/usb/functionfs.h
DESCS_MAGIC_V2 = 3
STRINGS_MAGIC = 2
HAS_FS_DESC = 1
HAS_HS_DESC = 2

EVENT_NAMES = ["BIND", "UNBIND", "ENABLE", "DISABLE", "SETUP", "SUSPEND", "RESUME"]
EVENT_SIZE = 12  # 8-byte setup packet, 1-byte type, 3 bytes padding

HID_GET_REPORT = 0x01
HID_GET_IDLE = 0x02
HID_GET_PROTOCOL = 0x03
HID_SET_REPORT = 0x09
HID_SET_IDLE = 0x0A
HID_SET_PROTOCOL = 0x0B
USB_REQ_GET_DESCRIPTOR = 0x06


def _descriptors():
    """The FunctionFS descriptor blob: interface, HID class descriptor, endpoint."""
    interface = struct.pack(
        "<BBBBBBBBB",
        9, 0x04,  # bLength, INTERFACE
        0, 0,     # bInterfaceNumber (renumbered by the kernel), bAlternateSetting
        1,        # bNumEndpoints
        0x03,     # bInterfaceClass: HID
        0x00,     # bInterfaceSubClass: no boot protocol
        0x00,     # bInterfaceProtocol
        0,        # iInterface
    )

    def endpoint(interval):
        return struct.pack(
            "<BBBBHB",
            7, 0x05,
            0x81,   # IN endpoint 1
            0x03,   # interrupt
            16,     # wMaxPacketSize
            interval,
        )

    # Full speed counts bInterval in milliseconds; high speed counts it as
    # 2^(n-1) microframes, so 1 and 4 both mean "poll me every millisecond".
    full_speed = interface + HID_DESCRIPTOR + endpoint(1)
    high_speed = interface + HID_DESCRIPTOR + endpoint(4)
    body = struct.pack("<II", 3, 3) + full_speed + high_speed
    head = struct.pack("<III", DESCS_MAGIC_V2, 12 + len(body), HAS_FS_DESC | HAS_HS_DESC)
    return head + body


def _strings():
    """No function-level strings; the descriptors above reference none."""
    return struct.pack("<IIII", STRINGS_MAGIC, 16, 0, 0)


def _write_attr(path, value):
    with open(path, "w") as f:
        f.write(value)


def _mkdir(path):
    try:
        os.mkdir(path)
    except FileExistsError:
        pass


def _quiet(fn, *args):
    try:
        fn(*args)
    except OSError:
        pass


def udc_name():
    udcs = sorted(os.listdir("/sys/class/udc"))
    if not udcs:
        raise RuntimeError("no USB device controller; this kernel cannot do gadget mode")
    return udcs[0]


class UsbHidGamepad:
    """A USB HID gamepad exposed over FunctionFS.

    Use as a context manager so the configfs gadget is always torn down and the
    port goes back to its normal behaviour, even if the app crashes.
    """

    def __init__(self, log=lambda msg: None):
        self.log = log
        self.gadget_dir = os.path.join(CONFIGFS, GADGET)
        self.func_dir = os.path.join(self.gadget_dir, "functions", "ffs." + FFS_NAME)
        self.config_dir = os.path.join(self.gadget_dir, "configs", "c.1")
        self.ep0 = None
        self.ep1 = None
        self._stop = threading.Event()
        self._ep0_thread = None
        self._lock = None
        self._last_report = IDLE_REPORT
        self.enabled = threading.Event()

    # -- setup ---------------------------------------------------------------

    def __enter__(self):
        self._acquire_lock()
        self.teardown()  # clear anything a previous crashed run left behind
        try:
            self.setup()
        except Exception:
            self.teardown()
            self._release_lock()
            raise
        return self

    def _acquire_lock(self):
        """Refuse to run twice over.

        A second instance would find the first one's FunctionFS mount already
        active and fail deep inside ep0 with a confusing ESRCH, so catch it here.
        """
        self._lock = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            self._lock = None
            raise RuntimeError("controller mode is already running")

    def __exit__(self, *exc):
        self.teardown()
        self._release_lock()
        return False

    def setup(self):
        self._create_gadget()
        self._mount_functionfs()
        self._open_ep0()
        self._bind()
        self.ep1 = os.open(os.path.join(FFS_MOUNT, "ep1"), os.O_WRONLY)
        self.log("gadget bound to " + udc_name())

    def _create_gadget(self):
        _mkdir(self.gadget_dir)
        # 1d6b:0104 is the Linux Foundation multifunction gadget ID, which every
        # host already treats as a generic composite device.
        _write_attr(self.gadget_dir + "/idVendor", "0x1d6b")
        _write_attr(self.gadget_dir + "/idProduct", "0x0104")
        _write_attr(self.gadget_dir + "/bcdDevice", "0x0100")
        _write_attr(self.gadget_dir + "/bcdUSB", "0x0200")
        strings = self.gadget_dir + "/strings/0x409"
        _mkdir(strings)
        _write_attr(strings + "/manufacturer", "Anbernic")
        _write_attr(strings + "/product", "RG35XX Plus Gamepad")
        _write_attr(strings + "/serialnumber", "z3pad0001")

        _mkdir(self.config_dir)
        _mkdir(self.config_dir + "/strings/0x409")
        _write_attr(self.config_dir + "/strings/0x409/configuration", "HID gamepad")
        _write_attr(self.config_dir + "/MaxPower", "100")

        _mkdir(self.func_dir)
        link = os.path.join(self.config_dir, "ffs." + FFS_NAME)
        if not os.path.islink(link):
            os.symlink(self.func_dir, link)

    def _mount_functionfs(self):
        _mkdir(FFS_MOUNT)
        subprocess.run(
            ["mount", "-t", "functionfs", FFS_NAME, FFS_MOUNT],
            check=True, capture_output=True,
        )

    def _open_ep0(self):
        """Writing descriptors then strings to ep0 activates the function."""
        self.ep0 = os.open(os.path.join(FFS_MOUNT, "ep0"), os.O_RDWR)
        os.write(self.ep0, _descriptors())
        os.write(self.ep0, _strings())
        self._ep0_thread = threading.Thread(target=self._ep0_loop, daemon=True)
        self._ep0_thread.start()

    def _bind(self):
        _write_attr(self.gadget_dir + "/UDC", udc_name())

    # -- ep0 control traffic -------------------------------------------------

    def _ep0_loop(self):
        poller = select.poll()
        poller.register(self.ep0, select.POLLIN)
        while not self._stop.is_set():
            if not poller.poll(200):
                continue
            try:
                data = os.read(self.ep0, EVENT_SIZE * 8)
            except OSError:
                continue
            for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                self._handle_event(data[i:i + EVENT_SIZE])

    def _handle_event(self, event):
        req_type, request, value, index, length = struct.unpack("<BBHHH", event[:8])
        etype = event[8]
        name = EVENT_NAMES[etype] if etype < len(EVENT_NAMES) else str(etype)
        if etype == 2:  # ENABLE: the host has configured us
            self.enabled.set()
            self.log("host connected")
        elif etype in (1, 3):  # UNBIND, DISABLE
            self.enabled.clear()
            self.log("host disconnected")
        elif etype == 4:  # SETUP
            self._handle_setup(req_type, request, value, index, length)
            return
        else:
            self.log("usb " + name.lower())

    def _handle_setup(self, req_type, request, value, index, length):
        is_in = bool(req_type & 0x80)
        reply = None

        if is_in and request == USB_REQ_GET_DESCRIPTOR:
            desc_type = value >> 8
            if desc_type == HID_DT_REPORT:
                reply = REPORT_DESC
            elif desc_type == HID_DT_HID:
                reply = HID_DESCRIPTOR
        elif is_in and request == HID_GET_REPORT:
            reply = self._last_report
        elif is_in and request == HID_GET_IDLE:
            reply = b"\x00"
        elif is_in and request == HID_GET_PROTOCOL:
            reply = b"\x01"
        elif not is_in and request in (HID_SET_REPORT, HID_SET_IDLE, HID_SET_PROTOCOL):
            reply = b""  # nothing to configure, just complete the transfer

        if reply is None:
            # FunctionFS stalls the pending request if userspace touches ep0 in
            # the opposite direction to the one the host asked for.
            self.log(f"stalling unsupported setup {req_type:02x}/{request:02x}")
            if is_in:
                _quiet(os.read, self.ep0, 0)
            else:
                _quiet(os.write, self.ep0, b"\x00")
            return

        try:
            if is_in:
                os.write(self.ep0, reply[:length])
            else:
                os.read(self.ep0, length)
        except OSError as e:
            self.log(f"setup {request:02x} failed: {e}")

    # -- input reports -------------------------------------------------------

    def send_report(self, report):
        """Push one input report. Returns False if the host is not listening."""
        self._last_report = report
        if self.ep1 is None or not self.enabled.is_set():
            return False
        try:
            os.write(self.ep1, report)
            return True
        except OSError:
            return False

    def wait_for_host(self, timeout=None):
        return self.enabled.wait(timeout)

    # -- teardown ------------------------------------------------------------

    def teardown(self):
        """Undo everything in reverse so USB goes back to its normal state."""
        self._stop.set()
        if self._ep0_thread:
            self._ep0_thread.join(timeout=1)
            self._ep0_thread = None

        # Unbind first: while a UDC is attached the rest of the gadget is busy.
        if os.path.exists(self.gadget_dir + "/UDC"):
            _quiet(_write_attr, self.gadget_dir + "/UDC", "\n")

        for fd in (self.ep1, self.ep0):
            if fd is not None:
                _quiet(os.close, fd)
        self.ep1 = self.ep0 = None

        if os.path.ismount(FFS_MOUNT):
            for attempt in range(6):
                args = ["umount", FFS_MOUNT] if attempt < 5 else ["umount", "-l", FFS_MOUNT]
                if subprocess.run(args, capture_output=True).returncode == 0:
                    break
                time.sleep(0.2)

        _quiet(os.unlink, os.path.join(self.config_dir, "ffs." + FFS_NAME))
        _quiet(os.rmdir, self.func_dir)
        _quiet(os.rmdir, self.config_dir + "/strings/0x409")
        _quiet(os.rmdir, self.config_dir)
        _quiet(os.rmdir, self.gadget_dir + "/strings/0x409")
        _quiet(os.rmdir, self.gadget_dir)
        _quiet(os.rmdir, FFS_MOUNT)
        self.enabled.clear()
        self._stop.clear()

    def _release_lock(self):
        if self._lock is not None:
            _quiet(fcntl.flock, self._lock, fcntl.LOCK_UN)
            _quiet(self._lock.close)
            self._lock = None
