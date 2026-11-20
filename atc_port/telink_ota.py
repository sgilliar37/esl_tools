"""
Telink BLE OTA helpers shared by the updater and the tests.

Image format the Telink OTA code accepts (checked against the stock PickSmart
firmware and the SDK library used by ATC_TLSR_Paper):
  * bytes 8..11 are "KNLT"
  * bytes 6..7 are 5D 02 (asks the receiver to check the whole-image CRC)
  * the 32-bit size at 0x18 is the full image length, CRC included
  * length % 16 == 4 (16-byte aligned body + 4-byte CRC)
  * last 4 bytes: Telink CRC32 of everything before it, little endian
    (reflected CRC-32, init 0xFFFFFFFF, no final XOR = zlib.crc32 ^ 0xFFFFFFFF)

OTA packets on characteristic 00010203-0405-0607-0809-0a0b0c0d2b12:
  start : 01 FF
  data  : index(le16) + 16 bytes + crc16(le16) over the first 18 bytes
          (CRC-16/MODBUS: init 0xFFFF, poly 0xA001 reflected)
  end   : 02 FF + last_index(le16) + (~last_index & 0xFFFF)(le16)
"""
import struct
import zlib

OTA_SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
OTA_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b12"
PDU = 16
MIN_SIZE = 0x4000      # stock rejects images of 16 KB or less
MAX_SIZE = 0x1F000     # stock accepts at most 124 KB (ATC lib: 128 KB)

RESULT_TEXT = {
    0: "success",
    1: "a packet was lost or repeated",
    2: "invalid packet",
    3: "packet checksum error",
    4: "flash write error",
    5: "the last packets did not arrive",
    6: "commands sent in the wrong order",
    7: "whole-image checksum did not match",
    8: "version check refused the update",
    9: "wrong packet length",
    10: "file is not Telink firmware (no KNLT mark)",
    11: "firmware size not accepted",
    12: "too long between packets",
    13: "update took longer than the tag allows",
    14: "connection dropped",
}


def telink_crc32(data):
    return zlib.crc32(data) ^ 0xFFFFFFFF


def crc16(data):
    crc = 0xFFFF
    for b in data:
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if (crc ^ b) & 1 else 0)
            b >>= 1
    return crc


def check_image(img):
    """Return a list of problems (empty if the image is acceptable)."""
    problems = []
    if len(img) < 0x20:
        return ["file too small"]
    if img[8:12] != b"KNLT":
        problems.append("no KNLT mark at offset 8")
    size = struct.unpack_from("<I", img, 0x18)[0]
    if size != len(img):
        problems.append("size field %d does not match file length %d" % (size, len(img)))
    if len(img) % 16 != 4:
        problems.append("length %% 16 is %d, must be 4" % (len(img) % 16))
    if not (MIN_SIZE < len(img) <= MAX_SIZE):
        problems.append("length %d outside %d..%d" % (len(img), MIN_SIZE, MAX_SIZE))
    if struct.unpack_from("<I", img, len(img) - 4)[0] != telink_crc32(img[:-4]):
        problems.append("whole-image CRC is wrong")
    return problems


def to_telink_image(raw):
    """Convert an ATC_Paper.bin build (or a raw linker image) into Telink OTA format."""
    if not check_image(raw):
        return bytes(raw)
    size = struct.unpack_from("<I", raw, 0x18)[0]
    if raw[8:12] != b"KNLT" or not (0x20 <= size <= len(raw)):
        raise ValueError("not a Telink firmware image")
    body = bytearray(raw[:size])  # drops ATC's own appended CRC
    if len(body) % PDU:
        body += b"\xff" * (PDU - len(body) % PDU)
    body[6], body[7] = 0x5D, 0x02
    struct.pack_into("<I", body, 0x18, len(body) + 4)
    img = bytes(body) + struct.pack("<I", telink_crc32(bytes(body)))
    problems = check_image(img)
    if problems:
        raise ValueError("; ".join(problems))
    return img


def start_packet():
    return struct.pack("<H", 0xFF01)


def data_packets(img):
    """Yield (index, 20-byte packet). The last packet carries the CRC, padded with FF."""
    n = (len(img) + PDU - 1) // PDU
    for i in range(n):
        chunk = img[i * PDU:(i + 1) * PDU]
        chunk += b"\xff" * (PDU - len(chunk))
        head = struct.pack("<H", i) + chunk
        yield i, head + struct.pack("<H", crc16(head))


def end_packet(last_index):
    return struct.pack("<HHH", 0xFF02, last_index, (~last_index) & 0xFFFF)
