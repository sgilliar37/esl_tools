"""
A stand-in for the `bleak` Bluetooth library with simulated tags, so the whole
updater (scan, connect, send, retry, wait for restart, identify) runs without
Bluetooth. Install with `install(tag, bystanders)`; it registers itself as `bleak`.

It copies what macOS did on the first real run:
  * the port names itself only in the scan response, which macOS reports as a
    separate packet (here the unnamed advert comes last, the worst case);
  * the Mac's own cached name for a tag (BLEDevice.name) is the name it had
    when the Mac last connected, so it is stale after an update;
  * other tags nearby advertise too, and some may be other kinds of ESL;
  * a scan can miss the scan response altogether (scan_response=False), and the
    first scan after starting can hear nothing at all (quiet_scans).
"""
import sys
import types

from sim_tag import StockReceiver, AtcLibReceiver

OTA_SVC = "00010203-0405-0607-0809-0a0b0c0d1912"
SERVICES = {
    "stock": ["00001800-0000-1000-8000-00805f9b34fb", "0000fef0-0000-1000-8000-00805f9b34fb", OTA_SVC],
    "port": ["00001800-0000-1000-8000-00805f9b34fb", "0000180f-0000-1000-8000-00805f9b34fb",
             "0000181a-0000-1000-8000-00805f9b34fb", OTA_SVC, "00001f10-0000-1000-8000-00805f9b34fb"],
    "other": ["00001800-0000-1000-8000-00805f9b34fb", "0000fff0-0000-1000-8000-00805f9b34fb"],
}


class SimTag:
    """The tag being updated. It runs whichever firmware its flash boots, and
    restarts after every finished or failed update, like the real one."""

    def __init__(self, flash, address="MAC-UUID-1", connect_timeouts=0, drop_on_attempt=None,
                 restart_scans=0, scan_response=True):
        self.flash = flash
        self.address = address
        self.connect_timeouts = connect_timeouts      # first N connects time out
        self.drop_on_attempt = drop_on_attempt or {}  # attempt number -> packet index
        self.restart_scans = restart_scans            # scans it stays silent while restarting
        self.scan_response = scan_response            # False: the Mac never hears the port's name
        self._silent_left = None
        self.connections = 0
        self.cached_name = None                       # what the Mac remembers
        self.boot()

    def boot(self):
        fl = self.flash
        bank = fl.boot_bank()
        self.mac = bytes(fl.mem[0x76000:0x76006])
        if bank == 0 and fl.mem[0:4] == bytes.fromhex("56800000"):
            # stock firmware: clears its spare bank at start-up; name from the ID at 0x7F000
            if fl.mem[0x20000:0x20000 + 0x1F000] != b"\xff" * 0x1F000:
                fl.erase(0x20000, 0x20000)
            self.name = "NEMR%08X" % int.from_bytes(fl.mem[0x7F000:0x7F004], "little")
            self.kind = "stock"
            self.receiver = StockReceiver(fl, new_bank=0x20000)
        else:
            # the port: the SDK library picks the other bank and clears it
            other = 0 if bank == 0x20000 else 0x20000
            if fl.mem[other:other + 12] != b"\xff" * 12:
                fl.erase(other, 0x20000)
            self.name = "ESL_%02X%02X%02X" % (self.mac[2], self.mac[1], self.mac[0])
            self.kind = "port"
            self.receiver = AtcLibReceiver(fl, new_bank=other)

    def visible(self):
        """Called once per scan: False while the tag is still restarting."""
        if not self.receiver.rebooted:
            return True
        if self._silent_left is None:
            self._silent_left = self.restart_scans
        if self._silent_left > 0:
            self._silent_left -= 1
            return False
        self._silent_left = None
        self.boot()
        return True


class Bystander:
    """Another device nearby: kind "stock", "port" or "other" (an ESL with
    firmware of its own)."""

    def __init__(self, name, address, kind=None, mac=b"\x2b\x06\x8b\xfa\x40\xa0"):
        self.name, self.address, self.mac, self.cached_name = name, address, mac, None
        self.kind = kind or ("port" if name.startswith("ESL_") else "stock")

    def visible(self):
        return True


class _Dev:
    def __init__(self, address, cached_name):
        self.address = address
        self.name = cached_name
        self.details = None


class _Adv:
    def __init__(self, local_name, service_data=None, manufacturer_data=None):
        self.local_name = local_name
        self.rssi = -60
        self.service_uuids = []
        self.service_data = service_data or {}
        self.manufacturer_data = manufacturer_data or {}


def _adverts(t):
    """The packets one advertising event produces, in the order macOS reports them."""
    if t.kind == "port":
        data = bytes(reversed(t.mac)) + bytes([0, 0, 0xBB, 87, 0x0B, 0xB8, 1])
        packets = [_Adv(None, service_data={"0000181a-0000-1000-8000-00805f9b34fb": data})]
        if getattr(t, "scan_response", True):
            packets.insert(0, _Adv(t.name))  # scan response: only the name
        return packets
    if t.kind == "stock":
        return [_Adv(t.name, manufacturer_data={0x5053: bytes([0xA0, 1, 0x81, 1, 0x40])})]
    return [_Adv(t.name)]


class _Char:
    uuid = "00010203-0405-0607-0809-0a0b0c0d2b12"
    properties = ["read", "write-without-response", "notify"]


class _Svc:
    def __init__(self, uuid):
        self.uuid = uuid


class _Services:
    def __init__(self, kind):
        self.kind = kind

    def __iter__(self):
        return iter([_Svc(u) for u in SERVICES[self.kind]])

    def get_characteristic(self, uuid):
        if OTA_SVC in SERVICES[self.kind] and uuid.lower() == _Char.uuid:
            return _Char()
        return None


def make_module(tag, bystanders=(), quiet_scans=0):
    everyone = [tag] + list(bystanders)
    quiet = [quiet_scans]

    class BleakScanner:
        def __init__(self, detection_callback=None, **kwargs):
            self.cb = detection_callback

        async def start(self):
            if quiet[0] > 0:  # this scan hears nothing
                quiet[0] -= 1
                return
            for t in everyone:
                if not t.visible():
                    continue
                dev = _Dev(t.address, t.cached_name)
                for adv in _adverts(t):
                    self.cb(dev, adv)

        async def stop(self):
            pass

    class BleakClient:
        def __init__(self, dev, timeout=10.0):
            self.target = next(t for t in everyone if t.address == dev.address)
            self.connected = False
            self.notify = None

        async def connect(self):
            t = self.target
            if t is tag:
                if tag.receiver.rebooted:
                    raise TimeoutError()  # still restarting: nothing answers
                tag.connections += 1
                if tag.connect_timeouts:
                    tag.connect_timeouts -= 1
                    raise TimeoutError()
                self.rx = tag.receiver
                self.seen = 0
                self.drop = tag.drop_on_attempt.get(tag.connections)
            t.cached_name = t.name  # macOS keeps the name it saw while connected
            self.services = _Services(t.kind)
            self.connected = True

        async def disconnect(self):
            self.connected = False

        @property
        def is_connected(self):
            if self.target is not tag:
                return self.connected
            return self.connected and not self.rx.rebooted

        async def start_notify(self, char, callback):
            self.notify = callback

        async def write_gatt_char(self, char, data, response=False):
            assert self.target is tag, "the updater wrote to a tag it was not meant to touch"
            if not self.is_connected:
                raise ConnectionError("disconnected")
            op = int.from_bytes(bytes(data[:2]), "little")
            if self.drop is not None and op == self.drop:
                self.drop = None
                return
            self.rx.write(bytes(data))
            for n in self.rx.notifications[self.seen:]:
                if self.notify:
                    self.notify(char, bytearray(n))
            self.seen = len(self.rx.notifications)

        async def read_gatt_char(self, char):
            if not self.is_connected:
                raise ConnectionError("disconnected")
            return bytearray(b"\x00")

    mod = types.ModuleType("bleak")
    mod.BleakScanner = BleakScanner
    mod.BleakClient = BleakClient
    return mod


def install(tag, bystanders=(), quiet_scans=0):
    sys.modules["bleak"] = make_module(tag, bystanders, quiet_scans)
