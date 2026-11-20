"""
Simulated OTA receivers, written from the disassembly of
  * the stock PickSmart firmware's otaWrite (Telink SDK 3.4-style, at 0x107a0), and
  * otaWrite in ATC_TLSR_Paper's liblt_8258.a (older Telink SDK).
They model the checks that decide accept/reject, the flash writes into the spare
bank, and the boot-flag switch, so the updater's packet stream can be tested
without hardware.
"""
import struct
import zlib

FLASH = 0x80000


def half_crc(reg, data):
    # Telink crc32_half_cal over nibbles == reflected CRC-32 without final XOR
    return zlib.crc32(data, reg ^ 0xFFFFFFFF) ^ 0xFFFFFFFF


def crc16(data):
    crc = 0xFFFF
    for b in data:
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if (crc ^ b) & 1 else 0)
            b >>= 1
    return crc


class Flash:
    def __init__(self, image0):
        self.mem = bytearray(b"\xff" * FLASH)
        self.mem[:len(image0)] = image0

    def write(self, addr, data):  # NOR flash: can only clear bits
        for i, b in enumerate(data):
            self.mem[addr + i] &= b

    def erase(self, addr, size):
        self.mem[addr:addr + size] = b"\xff" * size

    def boot_bank(self):
        for bank in (0, 0x20000):
            if self.mem[bank + 8] == 0x4B:
                return bank
        return None


class StockReceiver:
    """Stock PickSmart firmware (new-style Telink OTA server)."""
    PDU = 16

    def __init__(self, flash, new_bank=0x20000, max_size=0x1F000):
        self.f, self.new, self.max_size = flash, new_bank, max_size
        self.state = 0
        self.last = -1
        self.max_index = max_size // 16
        self.crc_ok = False
        self.result = None
        self.rebooted = False
        self.notifications = []

    def _fail(self, code):
        self.result = code
        self.notifications.append(bytes([0x06, 0xFF, code, 0]))
        self.f.erase(self.new, self.max_size)
        self.rebooted = True
        return code

    def write(self, value):
        if self.result is not None:
            return None  # finished; ignores everything until reboot
        l2cap = len(value) + 3
        op = struct.unpack_from("<H", value, 0)[0]
        if op <= 0xFEFF:
            return self._data(op, value, l2cap)
        if op in (0xFF01, 0xFF03):
            if self.state != 0:
                return self._fail(6)
            self.state, self.last, self.crc_ok = 2, -1, False
            return None
        if op == 0xFF02:
            if not self.state & 0x0A:
                return self._fail(6)
            mx, x = struct.unpack_from("<HH", value, 2)
            res = 0
            if mx ^ x == 0xFFFF and mx != self.last:
                res = 5
            if not self.crc_ok:
                res = 7
            if res:
                return self._fail(res)
            self.result = 0
            self.notifications.append(bytes([0x06, 0xFF, 0, 0]))
            self.f.write(self.new + 8, b"\x4b")
            self.f.write((0 if self.new else 0x20000) + 8, b"\x00\x00\x00\x00")
            self.rebooted = True
            return 0
        if op in (0xFF00, 0xFF04):
            return None
        return self._fail(2)

    def _data(self, op, value, l2cap):
        if op > self.max_index:
            return self._fail(2)
        r6 = self.last_len if (op == self.max_index and self.state & 0x20) else self.PDU
        if not self.state & 2:
            return self._fail(6)
        if op == 2 and not self.state & 0x20:
            return self._fail(11)
        if l2cap != r6 + 7:
            return self._fail(9)
        if op != self.last + 1:
            return self._fail(1)
        if crc16(value[:r6 + 2]) != struct.unpack_from("<H", value, r6 + 2)[0]:
            return self._fail(3)
        data = bytearray(value[2:2 + r6])
        off = op * self.PDU
        if off <= 24 < off + self.PDU:
            size = struct.unpack_from("<I", data, 24 - off)[0]
            if size <= 0x3FFF or size > self.max_size or size % 16 != 4:
                return self._fail(11)
            self.max_index = (size - 1) // self.PDU
            m = size % self.PDU
            self.last_len = (m & ~15) + 16
            self.crc_pos = m & ~15
            self.state |= 0x20
        if op == 0:
            self.crc = 0xFFFFFFFF
        if self.state & 0x20 and op == self.max_index:
            crc_in = struct.unpack_from("<I", data, self.crc_pos)[0]
            self.crc = half_crc(self.crc, bytes(data[:self.crc_pos]))
            if self.crc != crc_in:
                return self._fail(7)
            self.crc_ok = True
        else:
            self.crc = half_crc(self.crc, bytes(data))
        if off <= 8 < off + len(data):
            if bytes(data[8 - off:12 - off]) != b"KNLT":
                return self._fail(10)
            data[8 - off] = 0xFF
        self.f.write(self.new + off, data)
        self.last = op
        self.state |= 8
        return None


class AtcLibReceiver:
    """otaWrite from the SDK library linked into the ported ATC firmware."""

    def __init__(self, flash, new_bank):
        self.f, self.new = flash, new_bank
        self.started = False
        self.last = -1
        self.check_en = False
        self.crc_match = False
        self.crc_idx = 0x4000
        self.result = None
        self.rebooted = False
        self.notifications = []

    def _fail(self, code):
        self.result = code
        self.f.erase(self.new, 0x20000)
        self.rebooted = True
        return code

    def write(self, value):
        if self.result is not None:
            return None
        l2cap = len(value) + 3
        op = struct.unpack_from("<H", value, 0)[0]
        if op == 0xFF00:
            return None
        if op == 0xFF01:
            self.started, self.last, self.check_en, self.crc_idx = True, -1, False, 0x4000
            return None
        if op == 0xFF02:
            err = 6 if (self.check_en and not self.crc_match) else 0
            if l2cap == 9:
                mx, x = struct.unpack_from("<HH", value, 2)
                if mx ^ x == 0xFFFF and mx != self.last:
                    err = 4
            if err:
                return self._fail(err)
            self.result = 0
            self.f.write(self.new + 8, b"\x4b")
            self.f.write((0 if self.new else 0x20000) + 8, b"\x00\x00\x00\x00")
            self.rebooted = True
            return 0
        if op != self.last + 1:
            if self.last >= op:
                return None  # repeated packet: ignored
            return self._fail(1)
        if crc16(value[:18]) != struct.unpack_from("<H", value, 18)[0]:
            return self._fail(2)
        data = bytearray(value[2:18])
        bad_flag = False
        if op == 0:
            if data[6] == 0x5D and data[7] == 0x02:
                self.check_en = True
            elif data[6] == 0:
                self.check_en = False
            else:
                self.check_en, bad_flag = False, True
            self.crc = 0xFFFFFFFF
        if self.check_en:
            if op > self.crc_idx:
                if op == self.crc_idx + 1:
                    if self.crc != struct.unpack_from("<I", data, 0)[0]:
                        return self._fail(6)
                    self.crc_match = True
            else:
                if op == 1:
                    size = struct.unpack_from("<I", data, 8)[0]
                    if size - 1 <= 0x3FFFE and size % 16 == 4:
                        self.crc_idx = size // 16 - 1
                    else:
                        self.crc_idx, self.check_en, bad_flag = 0x4000, False, True
                self.crc = half_crc(self.crc, bytes(data))
                if self.check_en and op == self.crc_idx + 1:
                    pass
        if bad_flag:
            return self._fail(6)
        if op == 0:
            data[8] = 0xFF
        self.f.write(self.new + op * 16, data)
        self.last = op
        return None


class FakePeripheral:
    """Mimics CoreBluetooth's write-without-response buffer: it holds `room`
    packets, frees one slot per readiness check, and a write sent while it is
    full is silently lost (as on a real Mac)."""

    def __init__(self, room=3):
        self.room = room
        self.queued = 0
        self.not_ready_answers = 0

    def canSendWriteWithoutResponse(self):
        if self.queued >= self.room:
            self.queued -= 1  # the radio sends one; the caller sees "full" this time
            self.not_ready_answers += 1
            return False
        return True

    def take(self):
        """True if the write fits, False if CoreBluetooth would drop it."""
        if self.queued >= self.room:
            return False
        self.queued += 1
        return True

    def flush(self):
        self.queued = 0


class _Backend:
    def __init__(self, peripheral):
        self._peripheral = peripheral


class FakeClient:
    """Stands in for bleak.BleakClient, feeding a simulated receiver. Result
    notifications (06 FF code 00) are copied into `results`, like the updater's
    notification handler does."""

    def __init__(self, receiver, drop_index=None, results=None, peripheral=None):
        self.rx = receiver
        self.drop_index = drop_index
        self.results = results if results is not None else []
        self.writes = 0
        self.lost = 0
        self._seen = 0
        if peripheral is not None:
            self._backend = _Backend(peripheral)
        self.peripheral = peripheral

    @property
    def is_connected(self):
        return not self.rx.rebooted

    def _collect(self):
        for n in self.rx.notifications[self._seen:]:
            if len(n) >= 3 and n[0] == 0x06 and n[1] == 0xFF:
                self.results.append(n[2])
        self._seen = len(self.rx.notifications)

    async def write_gatt_char(self, char, data, response=False):
        if not self.is_connected:
            raise ConnectionError("disconnected")
        self.writes += 1
        if self.peripheral is not None and not self.peripheral.take():
            self.lost += 1
            return  # dropped by CoreBluetooth
        op = struct.unpack_from("<H", data, 0)[0]
        if self.drop_index is not None and op == self.drop_index:
            self.drop_index = None
            self.lost += 1
            return
        self.rx.write(bytes(data))
        self._collect()

    async def read_gatt_char(self, char):
        if not self.is_connected:
            raise ConnectionError("disconnected")
        if self.peripheral is not None:
            self.peripheral.flush()  # a read waits until the queue has gone out
        return b"\x00"
