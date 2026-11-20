#!/usr/bin/env python3
"""
Write the ported ATC_TLSR_Paper firmware to the PickSmart TLSR8250 tag, then
read it back and check every byte. Also restores the stock backup.

    ./flash.sh                 write ATC_Paper_picksmart.bin, then verify
    ./flash.sh restore         write the stock 512 KB backup back, then verify
    ./flash.sh --port /dev/cu.usbserial-XXXX ...

How the chip is caught (same as the backup): start with the board's 3V3 wire
UNPLUGGED, run the script, and plug 3V3 in when it prints "Activate".
After the catch the CPU stays halted, so the write and the read-back run
with -t 0 and need no more replugging.

Nothing is written until you type WRITE at the prompt.
"""
import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STOCK_DIR = os.path.join(HERE, "..", "stock_backup")
sys.path.insert(0, STOCK_DIR)
import backup_stock_firmware as bsf  # noqa: E402  (port + flasher discovery)

FIRMWARE = os.path.join(HERE, "ATC_Paper_picksmart.bin")
STOCK_IMAGE = os.path.join(STOCK_DIR, "backups", "stock_20261113_175437", "stock_full_512KB.bin")
STOCK_SHA256 = "37eb9d79d0e01f46af9c5ddabef57b9cc8a4a09f36d6724f6ba7b36f16affade"
LOG = os.path.join(HERE, "flash.log")
CHUNK = 0x4000  # read back in 16 KB pieces so a bad block only costs one piece


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def say(msg):
    print(msg, flush=True)
    with open(LOG, "a") as f:
        f.write(msg + "\n")


class Flasher:
    def __init__(self, port, baud, clk):
        self.python = os.path.join(STOCK_DIR, ".venv", "bin", "python")
        if not os.path.exists(self.python):
            self.python = sys.executable
        self.tool = bsf.find_flasher(os.path.join(STOCK_DIR, "tools", "TlsrComSwireWriter"))
        self.port, self.baud, self.clk = port, baud, clk
        self.banner = None

    def run(self, tact, *op, timeout=1800):
        cmd = [self.python, "-u", self.tool, "-p", self.port, "-b", str(self.baud),
               "-t", str(tact), "-c", str(self.clk)] + [str(o) for o in op]
        say("$ " + " ".join(os.path.basename(c) if i < 3 else c for i, c in enumerate(cmd)))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        t0 = time.time()
        out = b""
        with open(LOG, "ab") as lf:
            while True:
                ch = p.stdout.read1(256)
                if not ch:
                    break
                out += ch
                lf.write(ch)
                sys.stdout.write(ch.decode("utf-8", "replace"))
                if self.banner and tact and b"Activate" in out:
                    sys.stdout.write("\a\n" + "*" * 60 + "\n" + self.banner + "\n" + "*" * 60 + "\n")
                    tact = 0  # only once per run
                sys.stdout.flush()
                if time.time() - t0 > timeout:
                    p.kill()
                    raise SystemExit("Flasher timed out.")
        rc = p.wait()
        return rc, out.decode("utf-8", "replace")

    def read(self, addr, size, tact=0):
        fd, path = tempfile.mkstemp(suffix=".bin")
        os.close(fd)
        os.unlink(path)
        rc, _ = self.run(tact, "rf", "0x%x" % addr, "0x%x" % size, path)
        data = open(path, "rb").read() if os.path.exists(path) else b""
        if os.path.exists(path):
            os.unlink(path)
        return rc == 0 and len(data) == size, data


def try_catch(fl):
    """One catch attempt. True when the chip answers and reads back a real header."""
    ok, _ = fl.read(0, 0x100, tact=10000)
    if not ok:
        return False
    # The first read after a catch often comes back as zeros; the next ones must be real.
    for _ in range(3):
        ok, data = fl.read(0, 0x100)
        if ok and data[8:12] == b"KNLT":
            return True
    say("Chip answered, but the read-back looks wrong (no KNLT header).")
    return False


def catch_chip(fl, max_tries, use_rst):
    say("\n=== Step 1: catch the chip ===")
    if use_rst:
        say("RST mode: keep 3V3 CONNECTED the whole time. Have a jumper wire on the")
        say("board's RST hole ready. Press Enter to start.")
        action = "TAP RST TO GND for about half a second, then let go"
    else:
        say("Unplug the board's 3V3 wire now, then press Enter.")
        action = "PLUG 3V3 IN NOW"
    input()
    for attempt in range(1, max_tries + 1):
        say("\n--- Catch attempt %d of %d ---" % (attempt, max_tries))
        ok, msg = echo_test(fl.port, fl.baud)
        if not ok:
            raise SystemExit("\nWiring check failed before catching: %s\n"
                             "Fix it with the live check: ./flash.sh check" % msg)
        fl.banner = ">>>  %s  <<<" % action
        if try_catch(fl):
            say("\nChip caught and readable (attempt %d)." % attempt)
            return
        if attempt == max_tries:
            break
        if use_rst:
            say("\nMissed it. Next try starts in 3 s (Ctrl-C to stop).")
            wait = 3
        else:
            say("\nMissed it. UNPLUG 3V3 now. Next try starts in 5 s (Ctrl-C to stop).")
            wait = 5
        for s in range(wait, 0, -1):
            sys.stdout.write("  %d...\r" % s)
            sys.stdout.flush()
            time.sleep(1)
        say("")
    raise SystemExit("\nNo catch after %d tries. Nothing was written. Check the wires "
                     "(TX via 1k + RX joined to SWS, GND to GND), then run ./flash.sh again." % max_tries)


def verify(fl, image, base=0):
    say("\n=== Step 3: read back and compare ===")
    bad = []
    for off in range(0, len(image), CHUNK):
        want = image[off:off + CHUNK]
        for attempt in range(3):  # retry a piece if the link glitches
            ok, got = fl.read(base + off, len(want))
            if ok and got == want:
                break
        else:
            bad.append(off)
            say("  MISMATCH in 0x%05x..0x%05x" % (base + off, base + off + len(want)))
            continue
        say("  ok 0x%05x..0x%05x" % (base + off, base + off + len(want)))
    return bad


def echo_test(port, baud):
    """TX and RX are joined at SWS, so every byte sent must come straight back.
    Returns (ok, message). Needs no chip catch and writes nothing to the tag."""
    import serial
    pattern = bytes([0x55, 0xAA, 0x00, 0xFF, 0x0F, 0xF0, 0x33, 0xCC]) * 8
    try:
        sp = serial.Serial(port, baud, timeout=0.3)
    except Exception as e:
        return False, "cannot open %s: %s" % (port, e)
    try:
        sp.reset_input_buffer()
        sp.write(pattern)
        sp.flush()
        got = sp.read(len(pattern))
    finally:
        sp.close()
    if got == pattern:
        return True, "OK: adapter hears its own signal on SWS"
    if not got:
        return False, "BROKEN: nothing comes back (RX not joined to TX/SWS, or SWS shorted to GND)"
    if all(b == 0 for b in got):
        return False, "BROKEN: line stuck low (SWS touching GND?)"
    return False, "BROKEN: echo is garbled (%d of %d bytes, loose contact?)" % (len(got), len(pattern))


def wiring_check_loop(port, baud):
    say("Live wiring check on %s. Wiggle the clip and watch. Ctrl-C to stop." % port)
    say("Nothing is written to the tag, and the chip does not need to be caught.\n")
    last = None
    try:
        while True:
            ok, msg = echo_test(port, baud)
            if msg != last:
                say(time.strftime("%H:%M:%S ") + msg + ("" if ok else "  <<<"))
                last = msg
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("\nStopped.")


LOCK_SECTORS = (0x7A000, 0x7B000)  # tag_lock.c keeps the tag's password key in these two sectors


def unlock_over_wires(args):
    """Take the password off a tag whose password is lost: erase only the two
    password sectors over SWire. Firmware, pictures and settings are untouched."""
    port = bsf.pick_port(args.port)
    fl = Flasher(port, args.baud, args.clk)
    say("\n# RUN %s  unlock  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), port))
    catch_chip(fl, args.tries, args.rst)
    blank = True
    for sec in LOCK_SECTORS:
        ok, data = fl.read(sec, 0x100)
        if not ok:
            raise SystemExit("Could not read the password sectors. Nothing was changed. Run ./unlock_usb.sh again.")
        blank = blank and data == b"\xff" * len(data)
    if blank:
        say("\nThis tag has no password stored. Nothing to do.")
        return
    say("\nThis erases only flash 0x%05x..0x%05x, where the tag keeps its password." % (
        LOCK_SECTORS[0], LOCK_SECTORS[-1] + 0xFFF))
    if input("Type UNLOCK and press Enter to go ahead: ").strip() != "UNLOCK":
        raise SystemExit("Stopped. Nothing was changed.")
    for sec in LOCK_SECTORS:
        rc, out = fl.run(0, "es", "0x%x" % sec, "0x1000", timeout=120)
        ok, data = fl.read(sec, 0x100)
        if rc != 0 or not ok or data != b"\xff" * len(data):
            raise SystemExit("\nThe erase did not take. Run ./unlock_usb.sh again (the chip is still halted).")
    say("\nDONE: the password is gone. Unplug 3V3 and plug it back in (or put the battery back);")
    say("the tag then has no password until you lock it again in Tag Designer.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", nargs="?", default="firmware", choices=["firmware", "restore", "check", "unlock"])
    ap.add_argument("-p", "--port")
    ap.add_argument("-b", "--baud", type=int, default=921600)
    ap.add_argument("-c", "--clk", type=int, default=48)
    ap.add_argument("--rst", action="store_true",
                    help="reset by tapping RST to GND (3V3 stays connected) instead of replugging 3V3")
    ap.add_argument("--tries", type=int, default=20, help="catch attempts before giving up")
    args = ap.parse_args()

    if args.what == "check":
        wiring_check_loop(bsf.pick_port(args.port), args.baud)
        return

    # Never write anything unless the stock backup is present and intact.
    if not os.path.exists(STOCK_IMAGE) or sha256(STOCK_IMAGE) != STOCK_SHA256:
        raise SystemExit("The stock backup is missing or changed:\n  %s\nRefusing to write." % STOCK_IMAGE)

    if args.what == "unlock":
        return unlock_over_wires(args)

    image_path = FIRMWARE if args.what == "firmware" else STOCK_IMAGE
    image = open(image_path, "rb").read()
    if image[8:12] != b"KNLT":
        raise SystemExit("%s does not look like Telink firmware." % image_path)

    port = bsf.pick_port(args.port)
    fl = Flasher(port, args.baud, args.clk)
    say("\n# RUN %s  %s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), args.what, port))
    say("Image: %s (%d bytes, sha256 %s)" % (os.path.basename(image_path), len(image), sha256(image_path)))

    catch_chip(fl, args.tries, args.rst)

    say("\n=== Step 2: write ===")
    say("This ERASES and rewrites flash 0x00000..0x%05x on the tag." % len(image))
    if args.what == "firmware":
        say("The stock firmware will be replaced. Your backup can put it back: ./flash.sh restore")
    if input("Type WRITE and press Enter to go ahead: ").strip() != "WRITE":
        raise SystemExit("Stopped. Nothing was written.")
    rc, out = fl.run(0, "wf", "0", image_path, timeout=3600)
    if rc != 0 or "rror" in out:
        say("\nThe write reported a problem. Do NOT unplug yet; run the same command again")
        say("(the chip is still halted). If it keeps failing: ./flash.sh restore")
        raise SystemExit(1)

    bad = verify(fl, image)
    if bad:
        say("\n%d piece(s) did not match. Run the same command again before unplugging." % len(bad))
        raise SystemExit(1)
    say("\nDONE: every byte matches. Unplug 3V3 and plug it back in to start the new firmware.")


if __name__ == "__main__":
    try:
        main()
    except bsf.BackupError as e:
        raise SystemExit(str(e))
