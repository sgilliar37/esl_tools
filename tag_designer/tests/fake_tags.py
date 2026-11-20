"""
Simulated tags standing in for the `bleak` Bluetooth library, so the designer
can be tried (./"Tag Designer.command" --demo) and tested without hardware.

Each simulated tag follows the firmware's picture service exactly as written in
ATC_TLSR_Paper's epd_ble_service.c (plus this project's v2 changes), and turns
its 4000-byte buffer into the pixels the ST7305 driver (lcd_st7305.c) would put
on the glass. What each tag "shows" is saved as a PNG in tests/demo_screens/.
"""
import asyncio
import os
import struct
import sys
import types
import zlib

EPD_SERVICE = "13187b10-eba9-a3ba-044e-83d3217d9a38"
EPD_CHAR = "4b646063-6264-f3a7-8941-e65356ea82fe"
RXTX_CHAR = "00001f1f-0000-1000-8000-00805f9b34fb"
SCREEN_CHAR = "4b646064-6264-f3a7-8941-e65356ea82fe"
WINDOW = 180
SCREENS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_screens")


def write_png(path, rows):
    """rows: list of lists of 0 (black) / 1 (white)."""
    h, w = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + bytes(255 if p else 0 for p in r) for r in rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def lcd_pixels(buf):
    """What lcd_st7305_display() puts on the 250x122 glass for the buffer
    (same arithmetic as the C code): rows[y][x], 1 = white."""
    def img_white(x, y):
        return 1 if buf[(249 - x) * 16 + (y >> 3)] & (0x80 >> (y & 7)) else 0
    return [[img_white(x, y) for x in range(250)] for y in range(122)]


class LockModel:
    """tag_lock.c (firmware v3): the password lock, command for command."""
    def __init__(self, window):
        self.window, self.key, self.salt = window, None, bytes(16)
        self.total_failures, self.last_failure, self.time = 0, 0, 1000
        self.reset()

    def reset(self):  # every connect and disconnect
        self.open, self.chal, self.session, self.failures = False, None, bytes(16), 0

    def state(self):
        return 0 if self.key is None else (2 if self.open else 1)

    def allows(self):
        return self.key is None or self.open

    def cmd(self, p):
        import tag_lock as L
        c = p[0]
        if c == 0x10:
            ok = self.failures < 3 and not (self.total_failures >= 6 and self.time - self.last_failure < 30)
            ch = os.urandom(16)
            self.chal = ch if ok else None
            self.window[0:34] = bytes([0x10, self.state()]) + (ch if ok else bytes(16)) + self.salt
        elif c == 0x11:
            if len(p) >= 17 and self.key is not None and self.chal is not None:
                if L.aes128(self.key, self.chal) == bytes(p[1:17]):
                    self.open, self.session, self.total_failures = True, self.chal, 0
                else:
                    self.open = False
                    self.failures += 1
                    self.total_failures += 1
                    self.last_failure = self.time
            self.chal = None
        elif c == 0x12 and len(p) >= 33:
            if self.key is None:
                self.key, self.salt, self.open, self.session = bytes(p[1:17]), bytes(p[17:33]), True, bytes(16)
            elif self.open:
                mask = L.aes128(self.key, bytes(b ^ 0x5A for b in self.session))
                self.key, self.salt = bytes(a ^ b for a, b in zip(p[1:17], mask)), bytes(p[17:33])
        elif c == 0x13:
            if self.key is not None and self.open:
                self.key, self.salt = None, bytes(16)
        elif c not in (0x10, 0x11, 0x12, 0x13):
            return False
        return True


class SimTag:
    def __init__(self, name, address, kind="port", version=3, mac=None, fail_connects=0):
        self.name, self.address, self.kind, self.version = name, address, kind, version
        self.mac = mac or bytes.fromhex("A040FA" + (name[4:] if kind == "port" else "000000"))
        self.fail_connects = fail_connects
        self.battery, self.battery_mv = 87, 2950
        self.epd_buffer = bytearray(5000)  # epd_buffer_size in the firmware
        self.window = bytearray(WINDOW)
        self.byte_pos = 0
        self.custom_image = False
        self.screen = None      # last buffer put on the glass
        self.shows = "clock"
        self.time_set = None
        self.writes = 0
        self.refused = 0  # writes a locked tag ignored
        self.lock = LockModel(self.window) if kind == "port" and version >= 3 else None
        self.draw_clock()

    def draw_clock(self):
        """Stand-in for the firmware's clock screen: white, with a black band
        across the middle (the real one draws the time there)."""
        buf = bytearray(b"\xff" * 5000)
        for x in range(40, 210):
            for yb in range(5, 10):
                buf[(249 - x) * 16 + yb] = 0x00
        self.epd_buffer[:] = buf
        self.screen = bytes(buf[:4000])
        self.shows = "clock"

    # epd_ble_handle_write, line for line
    def epd_write(self, payload):
        self.writes += 1
        n = len(payload)
        if n < 1:
            return
        if self.lock:
            if self.lock.cmd(payload):
                return
            if not self.lock.allows():
                self.refused += 1
                return
        op = payload[0]
        if op == 0x00 and n >= 2:
            if self.version >= 2:
                self.custom_image = True
            self.epd_buffer[:] = bytes([payload[1]]) * len(self.epd_buffer)
        elif op == 0x01:
            if self.version >= 2:
                self.custom_image = True
            self.screen = bytes(self.epd_buffer[:4000])
            self.shows = "picture"
            self.save_png()
        elif op == 0x02 and n >= 3:
            self.byte_pos = payload[1] << 8 | payload[2]
        elif op == 0x03:
            if self.byte_pos + n - 1 >= len(self.epd_buffer) + 1:
                return
            self.epd_buffer[self.byte_pos:self.byte_pos + n - 1] = payload[1:]
            self.byte_pos += n - 1
        elif op == 0x05 and n >= 2 and self.version >= 2:
            self.custom_image = bool(payload[1])
            if not self.custom_image:
                self.draw_clock()
        elif op == 0x06 and n >= 3 and self.version >= 2:
            pos = payload[1] << 8 | payload[2]
            if pos + WINDOW <= len(self.epd_buffer):
                self.window[:] = self.epd_buffer[pos:pos + WINDOW]

    def save_png(self):
        try:
            os.makedirs(SCREENS, exist_ok=True)
            write_png(os.path.join(SCREENS, self.name + ".png"), lcd_pixels(self.screen))
        except OSError:
            pass


def default_tags():
    return [
        SimTag("ESL_8079EB", "SIM-0001", mac=bytes.fromhex("A040FA8079EB")),
        SimTag("ESL_8B062B", "SIM-0002", mac=bytes.fromhex("A040FA8B062B")),
        SimTag("ESL_7A1C03", "SIM-0003", version=1, mac=bytes.fromhex("A040FA7A1C03")),
        SimTag("NEMR62616005", "SIM-0004", kind="stock"),
    ]


class _Dev:
    def __init__(self, t):
        self.address, self.name, self.details = t.address, None, None


class _Adv:
    def __init__(self, name, sdata=None, mdata=None):
        self.local_name, self.rssi = name, -60
        self.service_uuids, self.service_data, self.manufacturer_data = [], sdata or {}, mdata or {}


class _Char:
    def __init__(self, uuid):
        self.uuid, self.properties = uuid, ["read", "write"]


class _Services:
    def __init__(self, t):
        self.t = t

    def __iter__(self):
        return iter([])

    def get_characteristic(self, uuid):
        uuid = uuid.lower()
        if self.t.kind == "port" and uuid in (EPD_CHAR, RXTX_CHAR):
            return _Char(uuid)
        if self.t.kind == "port" and self.t.version >= 2 and uuid == SCREEN_CHAR:
            return _Char(uuid)
        return None


def make_module(tags, delay=0.0):
    class BleakScanner:
        def __init__(self, detection_callback=None, **kw):
            self.cb = detection_callback

        async def start(self):
            for t in tags:
                if t.kind == "port":
                    # ATC advert: MAC, temperature x10, (unused), battery %, battery mV, counter
                    sd = {"0000181a-0000-1000-8000-00805f9b34fb": t.mac + bytes([0, 0, 0, t.battery]) +
                          t.battery_mv.to_bytes(2, "big") + b"\x01"}
                    self.cb(_Dev(t), _Adv(t.name))
                    self.cb(_Dev(t), _Adv(None, sdata=sd))
                else:
                    self.cb(_Dev(t), _Adv(t.name, mdata={0x5053: b"\xa0\x01"}))

        async def stop(self):
            pass

    class BleakClient:
        def __init__(self, dev, timeout=10.0):
            self.t = next(t for t in tags if t.address == dev.address)
            self.is_connected = False
            self.mtu_size = 185

        async def connect(self):
            if self.t.fail_connects:
                self.t.fail_connects -= 1
                raise TimeoutError("simulated connect timeout")
            self.is_connected = True
            self.services = _Services(self.t)
            if self.t.lock:
                self.t.lock.reset()

        async def disconnect(self):
            self.is_connected = False
            if self.t.lock:
                self.t.lock.reset()

        async def read_gatt_char(self, char):
            assert self.is_connected
            if char.uuid == EPD_CHAR:
                if self.t.lock:
                    return bytearray([3, self.t.lock.state()])
                return bytearray([self.t.version if self.t.version >= 2 else 0])
            if char.uuid == SCREEN_CHAR:
                # longer than one response: CoreBluetooth fetches the rest with Read Blob
                return bytearray(self.t.window)
            return bytearray(1)

        async def write_gatt_char(self, char, data, response=False):
            assert self.is_connected
            data = bytes(data)
            assert len(data) <= self.mtu_size - 3, "write longer than the MTU allows"
            if delay:
                await asyncio.sleep(delay)
            if char.uuid == EPD_CHAR:
                self.t.epd_write(data)
            elif char.uuid == RXTX_CHAR and self.t.lock and not self.t.lock.allows():
                self.t.refused += 1
            elif char.uuid == RXTX_CHAR and data[0] == 0xDD:
                self.t.time_set = int.from_bytes(data[1:5], "big")

    mod = types.ModuleType("bleak")
    mod.BleakScanner, mod.BleakClient = BleakScanner, BleakClient
    return mod


def install(tags=None, delay=0.004):
    tags = tags if tags is not None else default_tags()
    sys.modules["bleak"] = make_module(tags, delay)
    return tags
