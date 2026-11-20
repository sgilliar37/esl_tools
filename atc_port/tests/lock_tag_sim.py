"""Tags with the password lock, for tests without hardware.

PyLockTag mirrors src/tag_lock.c in Python. CLockTag runs the real tag_lock.c,
compiled for this computer (LOCK_HOST=path to the host build). FakeClient is
just enough of bleak's BleakClient for tag_lock.py and the Tag Designer."""
import os
import subprocess

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tag_lock as L  # noqa: E402


class PyLockTag:
    def __init__(self):
        self.flash = None  # (key, salt) or None
        self.window = bytearray(180)
        self.total_failures, self.last_failure, self.time = 0, 0, 1000
        self.reset()

    def reset(self):  # connect / disconnect
        self.open = False
        self.chal = None
        self.session = bytes(16)
        self.failures = 0

    def state(self):
        if self.flash is None:
            return 0
        return 2 if self.open else 1

    def allows(self):
        return self.flash is None or self.open

    def status(self):
        return bytes([3, self.state()])

    def cooling(self):
        return self.total_failures >= 6 and self.time - self.last_failure < 30

    def cmd(self, p):
        c = p[0]
        salt = self.flash[1] if self.flash else bytes(16)
        if c == 0x10:
            ch = os.urandom(16)
            ok = self.failures < 3 and not self.cooling()
            self.chal = ch if ok else None
            self.window[0:34] = bytes([0x10, self.state()]) + (ch if ok else bytes(16)) + salt
        elif c == 0x11:
            if len(p) >= 17 and self.flash is not None and self.chal is not None:
                if L.aes128(self.flash[0], self.chal) == bytes(p[1:17]):
                    self.open, self.session, self.total_failures = True, self.chal, 0
                else:
                    self.open = False
                    self.failures += 1
                    self.total_failures += 1
                    self.last_failure = self.time
            self.chal = None
        elif c == 0x12:
            if len(p) >= 33:
                if self.flash is None:
                    self.flash, self.open, self.session = (bytes(p[1:17]), bytes(p[17:33])), True, bytes(16)
                elif self.open:
                    mask = L.aes128(self.flash[0], bytes(b ^ 0x5A for b in self.session))
                    self.flash = (bytes(a ^ b for a, b in zip(p[1:17], mask)), bytes(p[17:33]))
        elif c == 0x13:
            if self.flash is not None and self.open:
                self.flash = None
        else:
            return False
        return True

    def set_time(self, t):
        self.time = t


class CLockTag:
    """The firmware's own tag_lock.c, through a small host program."""
    def __init__(self, path):
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self._call("init")
        self.window = bytearray(180)
        self._st = bytes([3, 0])
        self._allows = True

    def _call(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        return self.p.stdout.readline().split()

    def reset(self):
        self._call("conn")
        self.cmd(b"\x00")  # refresh the cached status (00 is not a lock command)

    def cmd(self, p):
        out = self._call("cmd " + bytes(p).hex())
        handled, allows, _, st, win, flash = out
        self._allows = allows == "1"
        self._st = bytes.fromhex(st)
        self.window[0:34] = bytes.fromhex(win)
        self.flash_bytes = bytes.fromhex(flash)
        return handled == "1"

    def allows(self):
        return self._allows

    def status(self):
        return self._st

    def set_time(self, t):
        self._call("time %d" % t)

    def fail_writes(self, n):
        self._call("failwrites %d" % n)

    def tear(self):  # damage the stored records (a torn write), then reboot
        self._call("tear")
        self._call("init")
        self.cmd(b"\x00")

    def erase_key_sector(self):  # what the USB-UART recovery does
        self._call("erase")
        self._call("init")
        self.cmd(b"\x00")


def make_tag():
    host = os.environ.get("LOCK_HOST")
    return CLockTag(host) if host else PyLockTag()


class _Char:
    def __init__(self, uuid):
        self.uuid = uuid
        self.properties = ["read", "write"]


class _Services:
    def __init__(self):
        self.c = {u: _Char(u) for u in (L.EPD_CHAR, L.SCREEN_CHAR)}

    def get_characteristic(self, u):
        return self.c.get(u)


class FakeClient:
    """Connection to a lock tag; also records picture writes that got through."""
    def __init__(self, tag):
        self.tag = tag
        self.services = _Services()
        self.accepted = []
        tag.reset()

    async def write_gatt_char(self, char, data, response=False):
        u = getattr(char, "uuid", char)
        if u == L.EPD_CHAR:
            if self.tag.cmd(bytes(data)):
                return
            if self.tag.allows():
                self.accepted.append(bytes(data))

    async def read_gatt_char(self, char):
        u = getattr(char, "uuid", char)
        if u == L.EPD_CHAR:
            return bytearray(self.tag.status())
        return bytearray(self.tag.window)

    def close(self):
        self.tag.reset()
