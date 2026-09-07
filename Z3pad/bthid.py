"""Bluetooth HID gamepad: the wireless counterpart to hidgadget.

Bluetooth classic HID puts the report descriptor in an SDP record rather than on
a control endpoint, then carries reports over two L2CAP channels: PSM 17 for
control and PSM 19 for interrupt (where input reports go). BlueZ publishes the
SDP record for us once we register a profile, and pairing/encryption is its job
too, so this module only has to own the sockets and speak HIDP.

One wrinkle drives a lot of the code below: BlueZ ships an "input" plugin that
implements the HID *host* role, and it already listens on PSM 17 and 19 so the
handheld can use its own Bluetooth controllers. It has to be turned off before we
can take the device role, which means restarting bluetoothd. That change is
reverted on teardown.

Exposes the same interface as hidgadget.UsbHidGamepad so controller.py can drive
either transport.
"""

import fcntl
import os
import socket
import struct
import subprocess
import threading
import time

from hidreport import IDLE_REPORT, REPORT_DESC, REPORT_LEN  # noqa: F401

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
PROFILE_PATH = "/org/bluez/z3pad"
AGENT_PATH = "/org/bluez/z3pad/agent"

CONTROL_PSM = 17
INTERRUPT_PSM = 19

# HIDP transaction headers (Bluetooth HID profile 1.1, section 7.4)
HIDP_TRANS_HANDSHAKE = 0x00
HIDP_TRANS_GET_REPORT = 0x40
HIDP_TRANS_SET_REPORT = 0x50
HIDP_TRANS_GET_PROTOCOL = 0x60
HIDP_TRANS_SET_PROTOCOL = 0x70
HIDP_TRANS_DATA = 0xA0
HIDP_DATA_RTYPE_INPUT = 0x01
HIDP_HANDSHAKE_OK = 0x00
HIDP_HANDSHAKE_ERR_UNSUPPORTED = 0x03
INPUT_REPORT_HEADER = HIDP_TRANS_DATA | HIDP_DATA_RTYPE_INPUT  # 0xA1

# Class of device: peripheral major class, gamepad minor class.
GAMEPAD_CLASS = "0x002508"

SYSTEMD_DROPIN_DIR = "/etc/systemd/system/bluetooth.service.d"
SYSTEMD_DROPIN = os.path.join(SYSTEMD_DROPIN_DIR, "z3pad-noinput.conf")
LOCK_FILE = "/run/z3pad-bt.lock"

SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_MEDIUM = 2

DEVICE_NAME = "RG35XX Plus Gamepad"


def sdp_record(name=DEVICE_NAME):
    """The HID SDP record BlueZ will publish, carrying our report descriptor."""
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001">
    <sequence><uuid value="0x1124" /></sequence>
  </attribute>
  <attribute id="0x0004">
    <sequence>
      <sequence><uuid value="0x0100" /><uint16 value="0x0011" /></sequence>
      <sequence><uuid value="0x0011" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005">
    <sequence><uuid value="0x1002" /></sequence>
  </attribute>
  <attribute id="0x0006">
    <sequence>
      <uint16 value="0x656e" /><uint16 value="0x006a" /><uint16 value="0x0100" />
    </sequence>
  </attribute>
  <attribute id="0x0009">
    <sequence>
      <sequence><uuid value="0x1124" /><uint16 value="0x0101" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x000d">
    <sequence>
      <sequence>
        <sequence><uuid value="0x0100" /><uint16 value="0x0013" /></sequence>
        <sequence><uuid value="0x0011" /></sequence>
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0100"><text value="{name}" /></attribute>
  <attribute id="0x0101"><text value="Z3 controller mode" /></attribute>
  <attribute id="0x0102"><text value="Anbernic" /></attribute>
  <attribute id="0x0200"><uint16 value="0x0100" /></attribute>
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>
  <attribute id="0x0202"><uint8 value="0x08" /></attribute>
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>
  <attribute id="0x0204"><boolean value="false" /></attribute>
  <attribute id="0x0205"><boolean value="true" /></attribute>
  <attribute id="0x0206">
    <sequence>
      <sequence>
        <uint8 value="0x22" />
        <text encoding="hex" value="{REPORT_DESC.hex()}" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0207">
    <sequence>
      <sequence><uint16 value="0x0409" /><uint16 value="0x0100" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0209"><boolean value="true" /></attribute>
  <attribute id="0x020a"><boolean value="true" /></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>
  <attribute id="0x020d"><boolean value="true" /></attribute>
  <attribute id="0x020e"><boolean value="false" /></attribute>
</record>
"""


def _quiet(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def bluetoothd_cmdline():
    """The running bluetoothd's argv, or None if it is not running."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                parts = [p.decode() for p in f.read().split(b"\x00") if p]
        except OSError:
            continue
        if parts and os.path.basename(parts[0]) == "bluetoothd":
            return parts
    return None


def input_plugin_disabled(argv=None):
    """Whether bluetoothd was started with its HID host plugin turned off."""
    argv = argv if argv is not None else (bluetoothd_cmdline() or [])
    for i, arg in enumerate(argv):
        if arg.startswith("--noplugin="):
            disabled = arg.split("=", 1)[1]
        elif arg in ("-P", "--noplugin") and i + 1 < len(argv):
            disabled = argv[i + 1]
        else:
            continue
        if "input" in disabled.split(",") or disabled == "*":
            return True
    return False


def missing_dependency():
    """Which python module is missing for the D-Bus work, if any."""
    for module, package in (("dbus", "python3-dbus"), ("gi", "python3-gi")):
        try:
            __import__(module)
        except ImportError:
            return package
    return None


class BluetoothHidGamepad:
    """A Bluetooth classic HID gamepad.

    Use as a context manager: teardown re-enables BlueZ's input plugin and undoes
    the adapter changes, so the handheld can pair with its own controllers again.
    """

    def __init__(self, log=lambda msg: None, name=DEVICE_NAME):
        self.log = log
        self.name = name
        self.enabled = threading.Event()
        self._lock = None
        self._bus = None
        self._adapter = None
        self._adapter_path = None
        self._profile = None
        self._agent = None
        self._mainloop = None
        self._loop_thread = None
        self._sockets = {}      # psm -> connected socket
        self._servers = {}      # psm -> listening socket we own
        self._restore_dropin = False
        self._restore_props = {}
        self._last_report = IDLE_REPORT
        self._stop = threading.Event()

    # -- setup ---------------------------------------------------------------

    def __enter__(self):
        self._acquire_lock()
        try:
            self.setup()
        except Exception:
            self.teardown()
            self._release_lock()
            raise
        return self

    def __exit__(self, *exc):
        self.teardown()
        self._release_lock()
        return False

    def _acquire_lock(self):
        self._lock = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            self._lock = None
            raise RuntimeError("bluetooth controller mode is already running")

    def setup(self):
        missing = missing_dependency()
        if missing:
            raise RuntimeError(f"missing python module, install it with: apt install {missing}")
        try:
            adapters = os.listdir("/sys/class/bluetooth")
        except OSError:
            adapters = []
        if not adapters:
            raise RuntimeError("no Bluetooth adapter found")

        # Restarting bluetoothd is disruptive, so only do it if BlueZ actually
        # gets in the way. On stock firmware the HID PSMs turn out to be free and
        # the first attempt succeeds, leaving Bluetooth untouched.
        for attempt in (1, 2):
            try:
                self._bind_psms()
                self._dbus_setup()
                break
            except Exception as e:
                self._reset_dbus()
                self._close_servers()
                if attempt == 2 or self._restore_dropin:
                    raise
                self.log(f"first attempt failed ({e}); disabling the bluez input plugin")
                self._free_hid_psms()
        self._start_loop()
        # Advertise as a gamepad so the PC shows the right icon, and make sure the
        # radio answers both inquiry and page scans.
        hci = adapters[0]
        subprocess.run(["hciconfig", hci, "class", GAMEPAD_CLASS], capture_output=True)
        subprocess.run(["hciconfig", hci, "piscan"], capture_output=True)
        self.log(f'discoverable as "{self.name}", waiting for a PC to pair')

    def _free_hid_psms(self):
        """Stop BlueZ owning PSM 17/19 for the HID host role."""
        argv = bluetoothd_cmdline()
        if argv is None:
            raise RuntimeError("bluetoothd is not running")
        if input_plugin_disabled(argv):
            self.log("bluez input plugin already disabled")
            return
        binary = argv[0]
        os.makedirs(SYSTEMD_DROPIN_DIR, exist_ok=True)
        with open(SYSTEMD_DROPIN, "w") as f:
            f.write("# Added by Z3apps controller mode; removed again on exit.\n"
                    "[Service]\nExecStart=\n"
                    f"ExecStart={binary} --noplugin=input\n")
        self._restore_dropin = True
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "restart", "bluetooth"], capture_output=True)
        self.log("restarted bluetoothd without its input plugin")
        for _ in range(50):
            if input_plugin_disabled() and os.listdir("/sys/class/bluetooth"):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("bluetoothd did not come back up without the input plugin")

    def _dbus_setup(self):
        """Connect to BlueZ and register everything, with the main loop stopped.

        dbus-python dislikes a blocking call racing a loop that is dispatching on
        another thread, so all outbound calls happen here and the loop only starts
        afterwards, when we merely need to listen for BlueZ calling us back.
        """
        self._start_dbus()
        self._prepare_adapter()
        self._register_agent()
        self._register_profile()

    def _reset_dbus(self):
        """Drop D-Bus state so a retry can start over cleanly."""
        self._unregister_dbus()
        self._bus = None
        self._mainloop = None

    def _close_servers(self):
        for server in list(self._servers.values()):
            _quiet(server.close)
        self._servers.clear()

    def _start_dbus(self):
        import dbus
        import dbus.mainloop.glib
        import dbus.service
        from gi.repository import GLib

        dbus.mainloop.glib.threads_init()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SystemBus()
        self._mainloop = GLib.MainLoop()

    def _start_loop(self):
        """Serve BlueZ's callbacks (NewConnection, pairing) on a background thread."""
        self._loop_thread = threading.Thread(target=self._mainloop.run, daemon=True)
        self._loop_thread.start()

    def _adapter_iface(self):
        import dbus
        obj = self._bus.get_object("org.bluez", self._adapter_path)
        return dbus.Interface(obj, "org.freedesktop.DBus.Properties")

    def _prepare_adapter(self):
        import dbus
        manager = dbus.Interface(self._bus.get_object("org.bluez", "/"),
                                 "org.freedesktop.DBus.ObjectManager")
        for path, ifaces in manager.GetManagedObjects().items():
            if "org.bluez.Adapter1" in ifaces:
                self._adapter_path = path
                break
        if not self._adapter_path:
            raise RuntimeError("BlueZ exposes no adapter on D-Bus")

        props = self._adapter_iface()
        for key, value in (("Powered", True), ("Pairable", True),
                           ("Discoverable", True), ("DiscoverableTimeout", dbus.UInt32(0)),
                           ("PairableTimeout", dbus.UInt32(0)), ("Alias", self.name)):
            # Straight after a bluetoothd restart the adapter can still be
            # settling and reject the first write, so give it a couple of tries.
            for attempt in range(3):
                try:
                    self._restore_props.setdefault(key, props.Get("org.bluez.Adapter1", key))
                    props.Set("org.bluez.Adapter1", key, value)
                    break
                except dbus.DBusException as e:
                    if attempt == 2:
                        self.log(f"could not set adapter {key}: "
                                 f"{e.get_dbus_message() or e.get_dbus_name()}")
                    else:
                        time.sleep(0.4)

    def _register_agent(self):
        """A NoInputNoOutput agent so pairing needs no confirmation on the handheld."""
        import dbus
        import dbus.service

        gamepad = self

        class Agent(dbus.service.Object):
            @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
            def Release(self):
                pass

            @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
            def AuthorizeService(self, device, uuid):
                gamepad.log(f"authorised {uuid} for {device}")

            @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
            def RequestAuthorization(self, device):
                gamepad.log(f"authorised pairing with {device}")

            @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
            def RequestConfirmation(self, device, passkey):
                gamepad.log(f"confirmed passkey {passkey} for {device}")

            @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
            def RequestPinCode(self, device):
                return "0000"

            @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
            def RequestPasskey(self, device):
                return dbus.UInt32(0)

            @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
            def DisplayPasskey(self, device, passkey, entered):
                pass

            @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
            def DisplayPinCode(self, device, pincode):
                pass

            @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
            def Cancel(self):
                pass

        self._agent = Agent(self._bus, AGENT_PATH)
        manager = dbus.Interface(self._bus.get_object("org.bluez", "/org/bluez"),
                                 "org.bluez.AgentManager1")
        manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        _quiet(manager.RequestDefaultAgent, AGENT_PATH)
        self.log("pairing agent registered")

    def _register_profile(self):
        """Publish the HID SDP record. BlueZ may also open the L2CAP servers."""
        import dbus
        import dbus.service

        gamepad = self

        class Profile(dbus.service.Object):
            @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
            def Release(self):
                pass

            @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}",
                                 out_signature="")
            def NewConnection(self, device, fd, properties):
                gamepad._adopt_fd(fd.take(), str(device))

            @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
            def RequestDisconnection(self, device):
                gamepad.log(f"{device} disconnected")
                gamepad._drop_connections()

        self._profile = Profile(self._bus, PROFILE_PATH)
        manager = dbus.Interface(self._bus.get_object("org.bluez", "/org/bluez"),
                                 "org.bluez.ProfileManager1")
        options = {
            "Name": self.name,
            "Role": "server",
            "Service": HID_UUID,
            "ServiceRecord": sdp_record(self.name),
            "RequireAuthentication": dbus.Boolean(True),
            "RequireAuthorization": dbus.Boolean(False),
        }
        manager.RegisterProfile(PROFILE_PATH, HID_UUID, options)
        self.log("HID SDP record published")

    def _bind_psms(self):
        """Listen on both HID PSMs, raising if either is taken.

        A busy PSM means BlueZ's input plugin holds it for the HID host role, so
        the caller retries after turning that plugin off.
        """
        busy = []
        for psm in (CONTROL_PSM, INTERRUPT_PSM):
            server = None
            try:
                server = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                                       socket.BTPROTO_L2CAP)
                _quiet(server.setsockopt, SOL_BLUETOOTH, BT_SECURITY,
                       struct.pack("BB", BT_SECURITY_MEDIUM, 0))
                server.bind((socket.BDADDR_ANY, psm))
                server.listen(1)
            except OSError as e:
                if server is not None:
                    _quiet(server.close)
                busy.append(f"psm {psm} ({e.strerror})")
                continue
            server.settimeout(0.5)
            self._servers[psm] = server
            threading.Thread(target=self._accept_loop, args=(psm, server),
                             daemon=True).start()
            self.log(f"listening on psm {psm}")
        if busy:
            raise RuntimeError("HID channels unavailable: " + ", ".join(busy))

    def _accept_loop(self, psm, server):
        while not self._stop.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._attach(psm, conn, str(addr[0]))

    def _adopt_fd(self, fd, device):
        """Wrap a socket BlueZ handed us and work out which channel it is."""
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                             socket.BTPROTO_L2CAP, fileno=fd)
        try:
            psm = sock.getsockname()[1]
            if psm not in (CONTROL_PSM, INTERRUPT_PSM):
                raise ValueError(psm)
        except Exception:
            # Control always connects before interrupt, so fall back to order.
            psm = CONTROL_PSM if CONTROL_PSM not in self._sockets else INTERRUPT_PSM
        self._attach(psm, sock, device)

    def _attach(self, psm, sock, device):
        old = self._sockets.get(psm)
        if old is not None:
            _quiet(old.close)
        self._sockets[psm] = sock
        channel = "control" if psm == CONTROL_PSM else "interrupt"
        self.log(f"{channel} channel connected from {device}")
        if psm == CONTROL_PSM:
            threading.Thread(target=self._control_loop, args=(sock,), daemon=True).start()
        if psm == INTERRUPT_PSM:
            self.enabled.set()
            self.send_report(self._last_report)

    def _drop_connections(self):
        self.enabled.clear()
        for psm, sock in list(self._sockets.items()):
            _quiet(sock.close)
            self._sockets.pop(psm, None)

    def _control_loop(self, sock):
        """Answer the few HIDP control requests a host may send."""
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data = sock.recv(64)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            header = data[0]
            transaction = header & 0xF0
            if transaction == HIDP_TRANS_GET_REPORT:
                _quiet(sock.send, bytes([INPUT_REPORT_HEADER]) + self._last_report)
            elif transaction in (HIDP_TRANS_SET_REPORT, HIDP_TRANS_SET_PROTOCOL):
                _quiet(sock.send, bytes([HIDP_TRANS_HANDSHAKE | HIDP_HANDSHAKE_OK]))
            elif transaction == HIDP_TRANS_GET_PROTOCOL:
                _quiet(sock.send, bytes([INPUT_REPORT_HEADER, 0x01]))
            else:
                _quiet(sock.send,
                       bytes([HIDP_TRANS_HANDSHAKE | HIDP_HANDSHAKE_ERR_UNSUPPORTED]))
        self.enabled.clear()

    # -- input reports -------------------------------------------------------

    def send_report(self, report):
        """Push one input report. Returns False if no host is connected."""
        self._last_report = report
        sock = self._sockets.get(INTERRUPT_PSM)
        if sock is None or not self.enabled.is_set():
            return False
        try:
            sock.send(bytes([INPUT_REPORT_HEADER]) + report)
            return True
        except OSError:
            self.enabled.clear()
            return False

    def wait_for_host(self, timeout=None):
        return self.enabled.wait(timeout)

    # -- teardown ------------------------------------------------------------

    def teardown(self):
        """Undo everything, in a try/finally chain.

        Restoring bluetoothd matters most: if the drop-in were left behind, the
        handheld could not pair with its own controllers any more. So it happens
        in a finally block, whatever the D-Bus cleanup does.
        """
        self._stop.set()
        try:
            self._drop_connections()
            for server in list(self._servers.values()):
                _quiet(server.close)
            self._servers.clear()
            # Stop the loop before unregistering, so those blocking calls run
            # without a dispatching loop racing them, mirroring setup().
            if self._mainloop is not None:
                _quiet(self._mainloop.quit)
            if self._loop_thread is not None:
                self._loop_thread.join(timeout=2)
            self._unregister_dbus()
        finally:
            self._mainloop = None
            self._loop_thread = None
            self._bus = None
            self._restore_bluetoothd()
            self.enabled.clear()
            self._stop.clear()

    def _unregister_dbus(self):
        if self._bus is None:
            return
        import dbus

        def iface(name):
            try:
                return dbus.Interface(self._bus.get_object("org.bluez", "/org/bluez"), name)
            except Exception:
                return None

        if self._profile is not None:
            manager = iface("org.bluez.ProfileManager1")
            if manager is not None:
                _quiet(manager.UnregisterProfile, PROFILE_PATH)
            _quiet(self._profile.remove_from_connection)
            self._profile = None
        if self._agent is not None:
            manager = iface("org.bluez.AgentManager1")
            if manager is not None:
                _quiet(manager.UnregisterAgent, AGENT_PATH)
            _quiet(self._agent.remove_from_connection)
            self._agent = None
        if self._adapter_path and self._restore_props:
            props = _quiet(self._adapter_iface)
            if props is not None:
                for key, value in self._restore_props.items():
                    _quiet(props.Set, "org.bluez.Adapter1", key, value)
        self._restore_props.clear()

    def _restore_bluetoothd(self):
        """Give the handheld its own HID host role back."""
        if not self._restore_dropin:
            return
        self._restore_dropin = False
        _quiet(os.remove, SYSTEMD_DROPIN)
        _quiet(os.rmdir, SYSTEMD_DROPIN_DIR)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "restart", "bluetooth"], capture_output=True)
        self.log("restored bluetoothd with its input plugin")

    def _release_lock(self):
        if self._lock is not None:
            _quiet(fcntl.flock, self._lock, fcntl.LOCK_UN)
            _quiet(self._lock.close)
            self._lock = None


def self_check():
    """Report on every prerequisite without changing anything."""
    ok = True

    def line(status, label, detail=""):
        print(f"[{status:^4}] {label}" + (f": {detail}" if detail else ""))

    missing = missing_dependency()
    if missing:
        line("FAIL", "python modules", f"missing, install with: apt install {missing}")
        ok = False
    else:
        line("PASS", "python modules", "dbus and gi both importable")

    try:
        adapters = sorted(os.listdir("/sys/class/bluetooth"))
    except OSError:
        adapters = []
    if adapters:
        line("PASS", "adapter", ", ".join(adapters))
    else:
        line("FAIL", "adapter", "none under /sys/class/bluetooth")
        ok = False

    argv = bluetoothd_cmdline()
    if argv is None:
        line("FAIL", "bluetoothd", "not running")
        ok = False
    elif input_plugin_disabled(argv):
        line("PASS", "bluetoothd", "already running without the input plugin")
    else:
        line("WARN", "bluetoothd", f"input plugin active; will restart it ({' '.join(argv)})")

    # Whether the HID PSMs are free tells us which side will own the sockets.
    for psm, role in ((CONTROL_PSM, "control"), (INTERRUPT_PSM, "interrupt")):
        s = None
        try:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                              socket.BTPROTO_L2CAP)
            s.bind((socket.BDADDR_ANY, psm))
            line("PASS", f"psm {psm} ({role})", "free, we can listen on it")
        except PermissionError:
            line("WARN", f"psm {psm} ({role})", "needs root; rerun as root to tell")
        except OSError as e:
            line("WARN", f"psm {psm} ({role})",
                 f"{e.strerror}; bluez holds it, expect a handover instead")
        finally:
            if s is not None:
                _quiet(s.close)

    rc = subprocess.run(["bluetoothctl", "--version"], capture_output=True, text=True)
    line("PASS" if rc.returncode == 0 else "WARN", "bluez version",
         rc.stdout.strip() or "unknown")

    print(f"\nSDP record: {len(sdp_record())} chars, "
          f"report descriptor {len(REPORT_DESC)} bytes, report {REPORT_LEN} bytes")
    print("verdict:", "Bluetooth HID looks usable" if ok else "Bluetooth HID cannot work yet")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(self_check())
