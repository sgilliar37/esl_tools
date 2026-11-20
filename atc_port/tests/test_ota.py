#!/usr/bin/env python3
"""
Tests for the Bluetooth updater against simulated tags (no Bluetooth needed).

    python3 tests/test_ota.py

The simulated receivers in sim_tag.py follow the disassembled update code of
the stock PickSmart firmware and of the SDK library in the ported firmware.
"""
import asyncio
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import telink_ota as T  # noqa: E402
import ota_update as U  # noqa: E402
from sim_tag import Flash, StockReceiver, AtcLibReceiver, FakeClient, FakePeripheral  # noqa: E402

STOCK_FULL = os.environ.get("OTA_TEST_STOCK", U.STOCK_IMAGE)
U.STOCK_IMAGE = STOCK_FULL
U.LOG = os.devnull  # keep test runs out of ota.log


def run(coro):
    return asyncio.run(coro)


def send(client, img, sync=16):
    """Run the updater's transfer. Returns None on success, or the OtaError."""
    try:
        run(U.send_image(client, None, img, sync, results=client.results))
        return None
    except U.OtaError as e:
        return e


def stock_flash():
    return Flash(open(STOCK_FULL, "rb").read())


def assert_same_except(before, after, ranges, what):
    """Every byte outside `ranges` must be unchanged."""
    keep = bytearray(after)
    for a, b in ranges:
        keep[a:b] = before[a:b]
    assert bytes(keep) == bytes(before), what + ": bytes changed outside the expected areas"


def synthetic_image(size=20000, seed=1):
    body = bytearray(zlib.compress(bytes(range(256)) * 200, 0)[:size].ljust(size, b"\x5a"))
    body[0:8] = b"\x26\x80\x00\x00\x00\x00\x00\x00"
    body[8:12] = b"KNLT"
    struct.pack_into("<I", body, 0x18, size)
    body[100] = seed
    return T.to_telink_image(bytes(body))


# ---------------------------------------------------------------- the images
def test_firmware_image_is_valid():
    img, _ = U.load_image("firmware")
    assert T.check_image(img) == [], T.check_image(img)
    assert len(img) % 16 == 4 and img[6:8] == b"\x5d\x02"
    assert T.MIN_SIZE < len(img) <= T.MAX_SIZE
    assert img == open(U.FIRMWARE, "rb").read(), "the shipped file is already in update format"


def test_restore_image_is_the_stock_firmware():
    img, _ = U.load_image("restore")
    full = open(STOCK_FULL, "rb").read()
    assert img == full[:len(img)] and len(img) == 72916
    assert T.check_image(img) == []


# -------------------------------------------- stock firmware receives the port
def test_stock_accepts_port_and_switches_bank():
    img, _ = U.load_image("firmware")
    fl = stock_flash()
    before = bytes(fl.mem)
    rx = StockReceiver(fl, new_bank=0x20000)
    client = FakeClient(rx)
    err = send(client, img)
    assert err is None, err
    assert rx.result == 0 and client.results == [0]
    assert bytes(fl.mem[0x20000:0x20000 + len(img)]) == img, "new bank holds the exact image"
    assert fl.boot_bank() == 0x20000, "tag boots the new firmware"
    assert fl.mem[8:12] == b"\x00\x00\x00\x00", "old bank's boot flag cleared"
    assert_same_except(before, fl.mem, [(8, 12), (0x20000, 0x20000 + len(img))], "stock update")
    # the MAC, the stored picture and the other data areas are untouched
    for a, b in ((0x44000, 0x46000), (0x76000, 0x77000), (0x7F000, 0x80000)):
        assert bytes(fl.mem[a:b]) == before[a:b]


def test_stock_rejects_lost_packet_and_keeps_its_firmware():
    img, _ = U.load_image("firmware")
    fl = stock_flash()
    before = bytes(fl.mem)
    rx = StockReceiver(fl)
    err = send(FakeClient(rx, drop_index=1234), img)
    assert err is not None and err.code == 1, err
    assert bytes(fl.mem) == before, "flash exactly as before"
    assert fl.boot_bank() == 0


def test_stock_rejects_bad_checksum_and_keeps_its_firmware():
    img, _ = U.load_image("firmware")
    bad = bytearray(img)
    bad[0x3000] ^= 0x01  # one flipped bit in the body, CRC left as it was
    fl = stock_flash()
    before = bytes(fl.mem)
    rx = StockReceiver(fl)
    err = send(FakeClient(rx), bytes(bad))
    assert err is not None and err.code == 7, err
    assert bytes(fl.mem) == before
    assert fl.boot_bank() == 0


def test_too_big_image_is_refused_before_sending():
    import tempfile
    body = bytearray(b"\x5a" * (T.MAX_SIZE + 16))
    body[8:12] = b"KNLT"
    struct.pack_into("<I", body, 0x18, len(body))
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(body)
    try:
        U.load_image(f.name)
        raise AssertionError("an oversize image was accepted")
    except U.OtaError as e:
        assert "outside" in str(e), e
    finally:
        os.unlink(f.name)


# ----------------------------------------- Mac Bluetooth buffer (flow control)
def test_flow_control_prevents_coreBluetooth_drops():
    img = synthetic_image()
    fl = Flash(b"")
    rx = StockReceiver(fl)
    per = FakePeripheral(room=3)
    client = FakeClient(rx, peripheral=per)
    err = send(client, img, sync=16)
    assert err is None, err
    assert client.lost == 0 and per.not_ready_answers > 0
    assert fl.boot_bank() == 0x20000


def test_without_flow_control_drops_are_caught_safely():
    img = synthetic_image()
    fl = Flash(b"")
    before = bytes(fl.mem)
    rx = StockReceiver(fl)
    client = FakeClient(rx, peripheral=FakePeripheral(room=3))
    real = U._cb_peripheral
    U._cb_peripheral = lambda c: None  # pretend the readiness check is unavailable
    try:
        err = send(client, img, sync=16)
    finally:
        U._cb_peripheral = real
    assert err is not None and err.code == 1, err
    assert bytes(fl.mem) == before and fl.boot_bank() is None


class StuckPeripheral(FakePeripheral):
    """A readiness flag that never comes back: the updater must not hang on it."""

    def canSendWriteWithoutResponse(self):
        self.not_ready_answers += 1
        return False

    def take(self):
        return True


def test_stuck_readiness_flag_falls_back_to_read_checks():
    img = synthetic_image()
    fl = Flash(b"")
    rx = StockReceiver(fl)
    per = StuckPeripheral()
    client = FakeClient(rx, peripheral=per)
    err = send(client, img, sync=16)
    assert err is None, err
    assert fl.boot_bank() == 0x20000
    assert per.not_ready_answers < 400, "gave up on the flag after one timeout"


# ------------------------------- the port receives later updates (SDK library)
def boot_port(fl):
    """What the port does on its first start from bank 0x20000: the SDK picks
    bank 0 as the next update area and erases it (bls_ota_clearNewFwDataArea)."""
    assert fl.boot_bank() == 0x20000
    fl.erase(0, 0x20000)


def test_full_round_trip_stock_to_port_to_stock_to_port():
    port, _ = U.load_image("firmware")
    stock, _ = U.load_image("restore")
    fl = stock_flash()
    original = bytes(fl.mem)
    # 1. stock firmware takes the port
    assert send(FakeClient(StockReceiver(fl, new_bank=0x20000)), port) is None
    boot_port(fl)
    # 2. the port takes the stock firmware back (./ota.sh restore)
    rx = AtcLibReceiver(fl, new_bank=0)
    err = send(FakeClient(rx), stock)
    assert err is None and rx.result == 0, err
    assert fl.boot_bank() == 0
    assert bytes(fl.mem[:len(stock)]) == stock
    assert bytes(fl.mem[0x44000:]) == original[0x44000:], "picture, MAC and data areas untouched"
    # 3. stock again takes the port (bank 0x20000 still holds the old port copy,
    #    which the stock firmware erases at start-up like the SDK does)
    fl.erase(0x20000, 0x20000)
    assert send(FakeClient(StockReceiver(fl, new_bank=0x20000)), port) is None
    assert fl.boot_bank() == 0x20000
    assert bytes(fl.mem[0x20000:0x20000 + len(port)]) == port


def test_port_rejects_lost_packet_and_keeps_running():
    port, _ = U.load_image("firmware")
    stock, _ = U.load_image("restore")
    fl = stock_flash()
    assert send(FakeClient(StockReceiver(fl)), port) is None
    boot_port(fl)
    before = bytes(fl.mem)
    rx = AtcLibReceiver(fl, new_bank=0)
    err = send(FakeClient(rx, drop_index=777), stock)
    assert err is not None and err.code == 14, err
    assert rx.result == 1
    assert bytes(fl.mem) == before and fl.boot_bank() == 0x20000


def test_port_rejects_bad_checksum_and_keeps_running():
    port, _ = U.load_image("firmware")
    stock, _ = U.load_image("restore")
    fl = stock_flash()
    assert send(FakeClient(StockReceiver(fl)), port) is None
    boot_port(fl)
    before = bytes(fl.mem)
    bad = bytearray(stock)
    bad[0x2000] ^= 0x80
    rx = AtcLibReceiver(fl, new_bank=0)
    err = send(FakeClient(rx), bytes(bad))
    assert err is not None and rx.result == 6, (err, rx.result)
    assert bytes(fl.mem) == before and fl.boot_bank() == 0x20000


# ------------------------------ the whole ./ota.sh flow with fake Bluetooth
class Args:
    def __init__(self, what="firmware", sync=16, name=None):
        self.what, self.sync, self.name, self.yes, self.scan = what, sync, name, True, False


def run_updater(tag, args, bystanders=(), answers=(), coro=None, quiet_scans=0):
    """Run ota_update.main_async (or `coro`) against a simulated tag.
    Returns (rc, output); rc is the OtaError if one ends the run.
    `answers` are typed at the updater's prompts, in order."""
    import builtins
    typed = list(answers)

    def fake_input(prompt=""):
        print(prompt)
        if not typed:
            raise AssertionError("unexpected prompt: %r" % prompt)
        return typed.pop(0)

    import contextlib
    import io
    import fake_bleak
    saved = sys.modules.get("bleak")
    real_sleep = asyncio.sleep

    async def no_wait(_t, *a, **k):
        await real_sleep(0)

    fake_bleak.install(tag, bystanders, quiet_scans)
    asyncio.sleep = no_wait
    real_input, builtins.input = builtins.input, fake_input
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            try:
                rc = run(coro() if coro else U.main_async(args))
            except U.OtaError as e:
                print(e)
                rc = e
    finally:
        builtins.input = real_input
        asyncio.sleep = real_sleep
        if saved is None:
            sys.modules.pop("bleak", None)
        else:
            sys.modules["bleak"] = saved
    return rc, out.getvalue()


def test_updater_end_to_end_then_restore():
    import fake_bleak
    port, _ = U.load_image("firmware")
    stock, _ = U.load_image("restore")
    fl = stock_flash()
    original = bytes(fl.mem)
    tag = fake_bleak.SimTag(fl)
    rc, out = run_updater(tag, Args())
    assert rc == 0, out
    assert "Found NEMR62616747  (signal -60)   <- the tag your backup came from" in out, out
    assert "DONE: the tag restarted as ESL_8079EB" in out, out
    assert fl.boot_bank() == 0x20000 and bytes(fl.mem[0x20000:0x20000 + len(port)]) == port
    assert bytes(fl.mem[:0x20000]) == b"\xff" * 0x20000, "the port cleared the old bank at start-up"
    rc, out = run_updater(tag, Args("restore"))
    assert rc == 0, out
    assert "DONE: the tag restarted as NEMR62616747" in out, out
    assert fl.boot_bank() == 0 and bytes(fl.mem[:len(stock)]) == stock
    assert bytes(fl.mem[0x40000:]) == original[0x40000:], "picture, MAC and data areas untouched"


def test_updater_retries_after_a_lost_packet():
    import fake_bleak
    fl = stock_flash()
    tag = fake_bleak.SimTag(fl, drop_on_attempt={1: 2000})
    rc, out = run_updater(tag, Args())
    assert rc == 0, out
    assert tag.connections == 2
    assert "Attempt 1 failed: tag reported: a packet was lost or repeated (code 1)" in out, out
    assert fl.boot_bank() == 0x20000


def test_updater_gives_up_after_three_failures_and_tag_is_unchanged():
    import fake_bleak
    fl = stock_flash()
    original = bytes(fl.mem)
    tag = fake_bleak.SimTag(fl, drop_on_attempt={1: 10, 2: 3000, 3: 5342})
    rc, out = run_updater(tag, Args())
    assert rc == 1, out
    assert "Gave up after 3 tries" in out, out
    assert bytes(fl.mem) == original and fl.boot_bank() == 0


def other_tag_flash(mac_tail=b"\x2b\x06\x8b", ident=0x62616020):
    """A second tag: same stock firmware, its own MAC and ID (not backed up)."""
    fl = stock_flash()
    fl.erase(0x76000, 0x1000)
    fl.write(0x76000, mac_tail + b"\xfa\x40\xa0")
    fl.erase(0x7F000, 0x1000)
    fl.write(0x7F000, struct.pack("<I", ident))
    return fl


def test_first_real_run_replayed():
    """Steve's first run: connect timed out, the tag then stayed silent for a
    while, other tags were around, and the port only names itself in the scan
    response while the Mac still remembered the stock name."""
    import fake_bleak
    fl = other_tag_flash()
    tag = fake_bleak.SimTag(fl, connect_timeouts=1, restart_scans=2)
    others = [fake_bleak.Bystander("NEMR62616005", "MAC-UUID-2")]
    rc, out = run_updater(tag, Args(name="NEMR62616020"), others)
    assert rc == 0, out
    assert "Found NEMR62616020  (signal -60)\n" in out, out
    assert "could not connect (TimeoutError" in out and "Nothing was sent" in out, out
    assert "throws the partial copy away" not in out, out
    assert "DONE: the tag restarted as ESL_8B062B" in out, out
    assert tag.connections == 2 and fl.boot_bank() == 0x20000


def test_slow_restart_is_waited_for():
    import fake_bleak
    fl = stock_flash()
    tag = fake_bleak.SimTag(fl, restart_scans=8)
    rc, out = run_updater(tag, Args())
    assert rc == 0 and "DONE: the tag restarted as ESL_8079EB" in out, out


def test_another_updated_tag_nearby_is_not_taken_for_this_one():
    import fake_bleak
    fl = other_tag_flash()
    tag = fake_bleak.SimTag(fl, restart_scans=1000)  # never comes back
    others = [fake_bleak.Bystander("ESL_8B062B", "MAC-UUID-2"), fake_bleak.Bystander("NEMR62616005", "MAC-UUID-3")]
    rc, out = run_updater(tag, Args(name="NEMR62616020"), others)
    assert rc == 1, out
    assert "DONE" not in out, out
    assert "NEMR62616020 has not come back as ESL_... yet" in out and "ESL_8B062B" in out, out


def test_default_only_ever_updates_the_backed_up_tag():
    """With no --name, other tags are listed but never touched, even when the
    backed-up tag is missing (what went wrong on the first real run)."""
    import fake_bleak
    fl = other_tag_flash()
    before = bytes(fl.mem)
    tag = fake_bleak.SimTag(fl)
    others = [fake_bleak.Bystander("NEMR62616005", "MAC-UUID-2")]
    rc, out = run_updater(tag, Args(), others, answers=["", ""])
    assert isinstance(rc, U.OtaError) and "Nothing was sent" in str(rc), out
    assert "ESL_8079EB or NEMR62616747 is not showing up" in out, out
    assert "NEMR62616020  (signal -60)" in out and "./ota.sh --name NAME" in out, out
    assert tag.connections == 0 and bytes(fl.mem) == before


def test_scan_tells_which_firmware_each_tag_runs():
    import fake_bleak
    fl = stock_flash()
    tag = fake_bleak.SimTag(fl)
    assert run_updater(tag, Args())[0] == 0  # the backed-up tag now runs the port
    others = [fake_bleak.Bystander("NEMR62616005", "MAC-UUID-2", "stock"),
              fake_bleak.Bystander("ESL_8B062B", "MAC-UUID-3", "other")]
    rc, out = run_updater(tag, None, others, coro=U.scan_only)
    assert rc == 0, out
    lines = out.splitlines()
    i = lines.index("  ESL_8079EB  (signal -60)   <- the tag your backup came from")
    assert lines[i + 1] == "      ATC-style advert: MAC A0:40:FA:80:79:EB, battery 87%", out
    assert lines[i + 2] == "      checking its firmware: the new firmware from this project", out
    i = lines.index("  NEMR62616005  (signal -60)")
    assert lines[i + 1] == "      PickSmart advert" and lines[i + 2].endswith("stock PickSmart firmware"), out
    i = lines.index("  ESL_8B062B  (signal -60)")
    assert "unknown firmware" in lines[i + 1], out


def test_port_is_recognised_when_its_name_packet_is_missed():
    """If the Mac never passes on the port's scan response, the port is named
    from the MAC in its advert, not from the Mac's out-of-date cached name."""
    import fake_bleak
    fl = stock_flash()
    tag = fake_bleak.SimTag(fl, scan_response=False)
    rc, out = run_updater(tag, Args())
    assert rc == 0 and "DONE: the tag restarted as ESL_8079EB" in out, out
    assert tag.cached_name == "NEMR62616747", "the Mac still remembers the stock name"


def test_scan_tries_again_when_the_first_scan_hears_nothing():
    import fake_bleak
    tag = fake_bleak.SimTag(stock_flash())
    rc, out = run_updater(tag, None, coro=U.scan_only, quiet_scans=2)
    assert rc == 0 and "No tags found" not in out, out
    assert "  NEMR62616747  (signal -60)   <- the tag your backup came from" in out, out
    assert "is not showing up" not in out, out


def test_scan_says_how_to_wake_the_backed_up_tag():
    import fake_bleak
    tag = fake_bleak.SimTag(other_tag_flash())
    rc, out = run_updater(tag, None, coro=U.scan_only)
    assert "  NEMR62616020  (signal -60)\n" in out, out
    assert "The tag your backup came from (ESL_8079EB or NEMR62616747) is not showing up" in out, out
    assert "touch RST to GND" in out, out
    rc, out = run_updater(tag, None, coro=U.scan_only, quiet_scans=100)
    assert "No tags found." in out and "touch RST to GND" in out, out
    assert tag.connections == 1, "the scan only connected to look, once"


def test_packets_match_the_documented_format():
    img = synthetic_image()
    pk = list(T.data_packets(img))
    assert len(pk) == (len(img) + 15) // 16
    i, p = pk[5]
    assert len(p) == 20 and p[:2] == b"\x05\x00" and p[2:18] == img[80:96]
    assert struct.unpack_from("<H", p, 18)[0] == T.crc16(p[:18])
    assert T.end_packet(0x1234) == b"\x02\xff\x34\x12\xcb\xed"
    assert T.start_packet() == b"\x01\xff"


def main():
    if not os.path.exists(STOCK_FULL):
        raise SystemExit("Stock backup not found: %s" % STOCK_FULL)
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok    " + name)
        except Exception as e:  # report every failure, then exit non-zero
            failed += 1
            print("FAIL  %s: %s: %s" % (name, type(e).__name__, e))
    print("\n%d passed, %d failed" % (len(tests) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
