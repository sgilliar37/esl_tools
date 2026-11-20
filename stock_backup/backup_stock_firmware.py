#!/usr/bin/env python3
"""
Read-only backup of the stock firmware on a TLSR8250 board.

It drives pvvx's TLSR825xComFlasher (vendored in tools/TlsrComSwireWriter)
and only ever calls its `rf` (Read Flash) command, whose syntax in the
tool source is:

    TLSR825xComFlasher.py [-p PORT] [-t TACT_MS] [-b BAUD] rf ADDRESS SIZE FILENAME

What it does:
  1. Probes the start of flash and the same offset at 512 KB. A 512 KB chip
     ignores the extra address bit, so the two reads are identical (mirror).
  2. Reads the whole flash twice and compares the SHA256 hashes. If they
     differ, it does a third read and keeps the pair that agrees.
  3. Rejects a dump that is all 0xFF (blank or not really read) or all 0x00.
  4. Checks for the Telink "KNLT" firmware signature and writes SHA256SUMS
     and backup_info.json next to the image, then makes the files read-only.

Manual reset (no RTS/DTR on the adapter): only the FIRST flasher run uses
activation (-t), and before it the script asks you to unplug 3V3 and plug it
back in when "Activate" appears. Activation soft-resets the chip, so every later
read runs with -t 0, which in the flasher skips activate() entirely: no soft
reset, the CPU stays halted from the first catch, and no new catch is needed.
Only if a -t 0 read fails does it ask for another catch and retry.

Logging: every run appends to ONE file, stock_backup/backup.log, starting with
a timestamped "RUN ..." header. The flasher's output is shown live on the
terminal as it arrives (so you can see "Activate" in time to power the board)
and copied into the same log.

Usage:
    python3 backup_stock_firmware.py backup [--port /dev/cu.usbserial-XXXX]
    python3 backup_stock_firmware.py verify backups/stock_YYYYmmdd_HHMMSS
    python3 backup_stock_firmware.py ports
"""

import argparse
import codecs
import datetime
import glob
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TOOL_DIR = os.path.join(HERE, "tools", "TlsrComSwireWriter")
DEFAULT_OUT_DIR = os.path.join(HERE, "backups")
LOG_NAME = "backup.log"       # one log for all runs, next to the backups/ folder

KB = 1024
SIZE_512K = 512 * KB
SIZE_1M = 1024 * KB
CATCH_SIZE = 0x100            # throwaway read that does the activation
PROBE_SIZE = 16 * KB          # bytes compared for the 512 KB / 1 MB mirror test
TELINK_MAGIC = b"KNLT"        # at offset 8 of a Telink firmware image
TELINK_MAGIC_OFFSET = 8
OTA_BANK2 = 0x20000           # usual second OTA bank on 825x firmware

# The only flasher sub-command this script is ever allowed to run.
ALLOWED_OPERATION = "rf"
FORBIDDEN_OPERATIONS = ("wf", "we", "es", "ea")


class BackupError(Exception):
    pass


class FlasherFailed(BackupError):
    """A read that failed in a way a fresh reset might fix (retryable)."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
_LOG_PATH = None              # set by open_run_log(); log() also appends there


def open_run_log(path, title):
    """Start a new run section in the single shared log file."""
    global _LOG_PATH
    _LOG_PATH = path
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a") as f:
        f.write("\n\n" + "#" * 72 + "\n")
        f.write("# RUN %s  %s\n" % (stamp, title))
        f.write("#" * 72 + "\n")


def close_run_log():
    global _LOG_PATH
    _LOG_PATH = None


def append_log(text):
    if _LOG_PATH:
        with open(_LOG_PATH, "a") as f:
            f.write(text)


def log(msg=""):
    print(msg, flush=True)
    append_log(msg + "\n")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def fill_ratio(data, value):
    if not data:
        return 0.0
    return data.count(bytes([value])) / len(data)


def count_diff(a, b):
    """Number of differing bytes and the first differing offset (or None)."""
    n = min(len(a), len(b))
    first = None
    diff = abs(len(a) - len(b))
    for i in range(n):
        if a[i] != b[i]:
            diff += 1
            if first is None:
                first = i
    return diff, first


def human_size(n):
    if n % SIZE_1M == 0:
        return "%dMB" % (n // SIZE_1M)
    if n % KB == 0:
        return "%dKB" % (n // KB)
    return "%dB" % n


def list_candidate_ports():
    """USB-UART ports on macOS (and Linux, for completeness)."""
    patterns = [
        "/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*",
        "/dev/cu.usbmodem*",
        "/dev/tty.usbserial*", "/dev/tty.SLAB_USBtoUART*", "/dev/tty.wchusbserial*",
        "/dev/ttyUSB*",
    ]
    found = []
    for p in patterns:
        for dev in sorted(glob.glob(p)):
            if dev not in found:
                found.append(dev)
    return found


def pick_port(requested):
    if requested:
        if not os.path.exists(requested):
            raise BackupError("Port %s does not exist. Run `ports` to list adapters." % requested)
        return requested
    ports = list_candidate_ports()
    # On macOS each adapter shows up as both cu.* and tty.*; prefer cu.*.
    cu = [p for p in ports if p.startswith("/dev/cu.")]
    choices = cu or ports
    if not choices:
        raise BackupError("No USB-UART adapter found (looked for /dev/cu.usbserial-*, "
                          "/dev/tty.usbserial-*, SLAB_USBtoUART, wchusbserial). "
                          "Plug it in, or pass --port.")
    if len(choices) > 1:
        raise BackupError("Several serial ports found, pick one with --port:\n  " + "\n  ".join(choices))
    return choices[0]


def find_flasher(tool_dir, explicit=None, fastcatch=False):
    if explicit:
        path = explicit
    else:
        # pvvx ships a macOS build with extra input-draining for Apple's USB stack.
        # --fastcatch (experimental, opt-in) uses a copy with one change in the
        # activation loop (no idle gaps). It went 0 for 3 on Steve's board, while
        # the original caught about 1 in 3, so the original is the default.
        names = ["TLSR825xComFlasher.py"]
        if sys.platform == "darwin":
            names.insert(0, "TLSR825xComFlasher-macos.py")
            if fastcatch:
                names.insert(0, "TLSR825xComFlasher-macos-fastcatch.py")
        path = None
        for n in names:
            cand = os.path.join(tool_dir, n)
            if os.path.isfile(cand):
                path = cand
                break
    if not path or not os.path.isfile(path):
        raise BackupError("TLSR825xComFlasher not found in %s (run setup.sh)." % tool_dir)
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    # Check the tool still has the `rf ADDRESS SIZE FILENAME` interface we rely on.
    for needle in ("'rf'", "add_argument('address'", "add_argument('size'", "add_argument('filename'"):
        if needle not in src:
            raise BackupError("%s does not look like the expected TLSR825xComFlasher "
                              "(missing %s). Not running it." % (path, needle))
    return path


# --------------------------------------------------------------------------
# flasher invocation (read only)
# --------------------------------------------------------------------------
class PowerCues:
    """Watches the flasher's live output and tells the user when to plug 3V3 in.

    "Activate (" is printed only when activation runs (-t > 0), right before the
    timed window starts, so that is the moment to power the board."""

    PLUG = "\a\n" + "*" * 60 + "\n>>>  PLUG 3V3 IN NOW  <<<\n" + "*" * 60 + "\n"

    def __init__(self):
        self.buf = ""
        self.activated = False
        self.answered = False

    def say(self, msg):
        sys.stdout.write(msg)
        sys.stdout.flush()
        append_log(msg.replace("\a", ""))

    def feed(self, text):
        self.buf = (self.buf + text)[-400:]
        if not self.activated and "Activate (" in self.buf:
            self.activated = True
            self.buf = ""
            self.say(self.PLUG)
        elif self.activated and not self.answered:
            if "ok." in self.buf:
                self.answered = True
                self.say("\n>>> Chip answered. Leave 3V3 connected for the rest of the backup.\n")
            elif "Chip sleep?" in self.buf:
                self.answered = True
                self.say("\n>>> Chip did not answer in the window (3V3 plugged in too late or too early?).\n")


class Reader:
    def __init__(self, python, flasher, port, baud, tact, clk, timeout_per_kb):
        self.python = python
        self.flasher = flasher
        self.port = port
        self.baud = baud
        self.tact = tact
        self.clk = clk
        self.timeout_per_kb = timeout_per_kb
        self.calls = 0

    def build_cmd(self, address, size, filename, tact=None):
        # -u: unbuffered, so lines like "Activate..." reach us the moment they are printed
        cmd = [self.python, "-u", self.flasher, "-p", self.port, "-b", str(self.baud),
               "-t", str(self.tact if tact is None else tact)]
        if self.clk:
            cmd += ["-c", str(self.clk)]
        cmd += [ALLOWED_OPERATION, "0x%06x" % address, "0x%x" % size, filename]
        self.assert_read_only(cmd)
        return cmd

    @staticmethod
    def assert_read_only(cmd):
        ops = [a for a in cmd[1:] if a in FORBIDDEN_OPERATIONS + (ALLOWED_OPERATION,)]
        if ops != [ALLOWED_OPERATION]:
            raise BackupError("Refusing to run a non-read flasher command: %r" % cmd)
        if "-r" in cmd or "--run" in cmd:
            raise BackupError("Refusing to pass --run: %r" % cmd)

    def read(self, address, size, filename, tact=None):
        """Run `rf`, then check the exit code and the exact file size.

        tact=None uses self.tact; tact=0 skips activation (no soft reset)."""
        if os.path.exists(filename):
            raise BackupError("Refusing to overwrite %s" % filename)
        cmd = self.build_cmd(address, size, filename, tact)
        self.calls += 1
        timeout = max(120, int(size / KB * self.timeout_per_kb))
        append_log("\n$ %s\n" % " ".join(cmd))
        print("  $ " + " ".join(os.path.basename(c) if i in (0, 2) else c
                                for i, c in enumerate(cmd)), flush=True)
        t0 = time.time()
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    bufsize=0, env=env)
        except OSError as e:
            raise BackupError("Could not start the flasher: %s" % e)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        cues = PowerCues()
        try:
            while True:
                # read() on an unbuffered pipe returns whatever is there, without
                # waiting for 256 bytes, so output appears live.
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                sys.stdout.write(text)
                sys.stdout.flush()
                append_log(text)
                cues.feed(text)
                if time.time() - t0 > timeout:
                    proc.kill()
                    raise FlasherFailed("Flasher timed out after %d s" % timeout)
            rc = proc.wait()
            proc.stdout.close()
        except KeyboardInterrupt:
            proc.kill()
            raise BackupError("Interrupted by user")
        elapsed = time.time() - t0
        if rc != 0:
            raise FlasherFailed("Flasher exited with code %d while reading 0x%06x+0x%x. "
                              "See the log: %s" % (rc, address, size, _LOG_PATH))
        # The flasher exits 0 on Ctrl-C and leaves a partial file, so the size check matters.
        if not os.path.isfile(filename):
            raise FlasherFailed("Flasher reported success but %s was not created" % filename)
        got = os.path.getsize(filename)
        if got != size:
            raise FlasherFailed("Short read: %s is %d bytes, expected %d" % (filename, got, size))
        return elapsed


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------
def check_not_blank(data, what):
    if fill_ratio(data, 0xFF) == 1.0:
        raise BackupError("%s is all 0xFF. Either the flash is blank or nothing was really "
                          "read (check wiring, power, and the --tact reset)." % what)
    if fill_ratio(data, 0x00) == 1.0:
        raise BackupError("%s is all 0x00. That is a failed read (SWS line stuck low?), "
                          "not firmware." % what)


def majority_vote(datas):
    """Byte-wise majority of 3+ equal-length reads.

    Returns (image, no_majority_offsets, changed_blocks) where changed_blocks maps
    each 256-byte block offset that had any disagreement to the reads that were
    outvoted there."""
    n = len(datas)
    if n < 3 or len(set(len(d) for d in datas)) != 1:
        raise BackupError("Majority vote needs 3 or more reads of the same size")
    need = n // 2 + 1
    out = bytearray(datas[0])
    no_major = []
    changed = {}
    for i in range(len(datas[0])):
        col = [d[i] for d in datas]
        if col.count(col[0]) == n:
            continue
        best = max(set(col), key=col.count)
        if col.count(best) < need:
            no_major.append(i)
            continue
        out[i] = best
        blk = i - i % 256
        changed.setdefault(blk, set()).update(k for k, v in enumerate(col) if v != best)
    return bytes(out), no_major, {b: sorted(v) for b, v in changed.items()}


def offsets_to_ranges(offs):
    ranges = []
    for o in offs:
        if ranges and o == ranges[-1][1] + 1:
            ranges[-1][1] = o
        else:
            ranges.append([o, o])
    return ranges


def has_telink_magic(data, base=0):
    o = base + TELINK_MAGIC_OFFSET
    return data[o:o + 4] == TELINK_MAGIC


def detect_size_from_probes(lo, hi):
    """512 KB flash wraps addresses, so 0x80000 reads back the same bytes as 0x0."""
    return SIZE_512K if lo == hi else SIZE_1M


def describe_image(data):
    info = {
        "ff_ratio": round(fill_ratio(data, 0xFF), 4),
        "telink_magic_at_0x0": has_telink_magic(data, 0),
        "telink_magic_at_0x20000": len(data) > OTA_BANK2 + 12 and has_telink_magic(data, OTA_BANK2),
    }
    if len(data) >= 0x1C:
        # bytes 0x18..0x1B hold the firmware size in Telink images
        info["fw_size_field_0x18"] = int.from_bytes(data[0x18:0x1C], "little")
    return info


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def make_run_dir(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, "stock_" + stamp)
    n = 1
    while True:
        try:
            os.mkdir(path)   # never reuses an existing folder, so an old backup is never touched
            return path
        except FileExistsError:
            n += 1
            path = os.path.join(out_dir, "stock_%s_%d" % (stamp, n))


def make_read_only(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def prompt_catch(args):
    """Tell the user to power-cycle the board so activation can catch it."""
    log("")
    log(">>> ACTION: unplug the 3V3 wire from the board now (keep GND and SWS connected).")
    log(">>> Then wait: plug 3V3 back in when it beeps and shows 'PLUG 3V3 IN NOW' "
        "(you have about %d s)." % (args.tact // 1000))
    if args.wait and sys.stdin.isatty():
        try:
            input(">>> Press Enter once 3V3 is unplugged... ")
        except EOFError:
            pass
    log("")


class ChipSession:
    """Runs reads so the chip needs as few manual catches as possible.

    The first read activates (soft reset + halt CPU); while that halt holds,
    later reads use -t 0 so the flasher does not reset the chip again."""

    def __init__(self, reader, args):
        self.reader = reader
        self.args = args
        self.halted = False
        self.catches = 0

    def catch(self, raw_dir):
        """Activate with a tiny throwaway read. On Steve's board the first 256-byte
        block read after activation comes back as zeros, so it must not be data we keep."""
        for attempt in range(self.args.retries + 1):
            prompt_catch(self.args)
            self.catches += 1
            target = os.path.join(raw_dir, "catch%d.bin" % self.catches)
            try:
                self.reader.read(0, CATCH_SIZE, target, tact=self.args.tact)
            except FlasherFailed as e:
                if attempt == self.args.retries:
                    raise
                log("\nCatch failed: %s" % e)
                log("Trying again (attempt %d of %d)." % (attempt + 2, self.args.retries + 1))
                continue
            self.halted = True
            log("  (Caught. The real reads follow with no reset: keep 3V3 connected.)")
            return

    def read(self, address, size, path):
        base, ext = os.path.splitext(path)
        for attempt in range(self.args.retries + 1):
            target = path if attempt == 0 else "%s_retry%d%s" % (base, attempt, ext)
            if self.halted:
                log("  (CPU still halted: keep 3V3 connected, no reset needed)")
            else:
                self.catch(os.path.dirname(path))
            try:
                t = self.reader.read(address, size, target, tact=0)
            except FlasherFailed as e:
                self.halted = False
                if attempt == self.args.retries:
                    raise
                log("\nRead failed: %s" % e)
                log("Retrying with a fresh reset (attempt %d of %d)." % (attempt + 2, self.args.retries + 1))
                continue
            self.halted = True
            return target, t


def cmd_backup(args):
    open_run_log(args.log or os.path.join(os.path.dirname(os.path.abspath(args.out_dir)), LOG_NAME),
                 " ".join(sys.argv[1:] if args.argv is None else args.argv))
    port = pick_port(args.port)
    flasher = find_flasher(args.tool_dir, args.flasher, args.fastcatch)
    try:
        import serial  # noqa: F401  (the flasher needs pyserial in this interpreter)
    except ImportError:
        if not args.flasher:
            raise BackupError("pyserial is not installed for %s. Run setup.sh first." % sys.executable)

    run_dir = make_run_dir(args.out_dir)
    raw_dir = os.path.join(run_dir, "raw")
    os.mkdir(raw_dir)
    reader = Reader(sys.executable, flasher, port, args.baud, args.tact, args.clk,
                    args.timeout_per_kb)
    chip = ChipSession(reader, args)

    log("=" * 60)
    log("TLSR8250 stock firmware backup (read only)")
    log("Port:    %s @ %d baud, activate %d ms (first read only), SWire CLK %s" %
        (port, args.baud, args.tact, ("%d MHz" % args.clk) if args.clk else "auto"))
    log("Flasher: %s" % flasher)
    log("Output:  %s" % run_dir)
    log("Log:     %s" % _LOG_PATH)
    log("=" * 60)

    started = datetime.datetime.now().isoformat(timespec="seconds")

    # ---- 1. probe and size detection -------------------------------------
    if args.size == "auto":
        log("\n[1/4] Probing flash size (two %d KB reads)..." % (PROBE_SIZE // KB))
        p_lo = os.path.join(raw_dir, "probe_0x000000.bin")
        p_hi = os.path.join(raw_dir, "probe_0x080000.bin")
        p_lo, t = chip.read(0x0, PROBE_SIZE, p_lo)
        lo = read_bytes(p_lo)
        check_not_blank(lo, "The first %d KB of flash" % (PROBE_SIZE // KB))
        p_hi, _ = chip.read(SIZE_512K, PROBE_SIZE, p_hi)
        hi = read_bytes(p_hi)
        if lo != hi or not has_telink_magic(lo):
            # The first read after a catch has come back with a bad (all-zero) first
            # block before, which made a 512 KB chip look like 1 MB. Read 0x0 again.
            log("\nThe probes differ (or no signature at 0x0). Re-reading 0x000000 once to rule out "
                "a bad first read...")
            p_lo2, _ = chip.read(0x0, PROBE_SIZE, os.path.join(raw_dir, "probe_0x000000_again.bin"))
            lo2 = read_bytes(p_lo2)
            if lo2 != lo:
                log("The re-read differs from the first probe, so the first one was bad. Using the re-read.")
                lo = lo2
                check_not_blank(lo, "The first %d KB of flash" % (PROBE_SIZE // KB))
        size = detect_size_from_probes(lo, hi)
        rate = PROBE_SIZE / t if t > 0 else 0
        log("\nProbe: 0x80000 %s 0x00000 -> %s flash" %
            ("mirrors" if size == SIZE_512K else "differs from", human_size(size)))
        if rate:
            eta = size * 2 / rate
            log("Read speed about %.1f KB/s, so two full reads take about %d min." %
                (rate / KB, int(eta / 60) + 1))
        if not has_telink_magic(lo):
            log("WARNING: no Telink 'KNLT' signature at offset 8. The read may be bad, or "
                "the stock firmware uses an unusual layout. Continuing; check the result.")
    else:
        size = SIZE_512K if args.size == "512k" else SIZE_1M
        log("\n[1/4] Flash size forced to %s" % human_size(size))

    # ---- 2. two full reads -------------------------------------------------
    reads = []
    for n in (1, 2):
        log("\n[%d/4] Full read %d of 2 (0x000000..0x%06x)..." % (n + 1, n, size))
        path, _ = chip.read(0, size, os.path.join(raw_dir, "read%d.bin" % n))
        reads.append(path)

    hashes = [sha256_file(p) for p in reads]
    log("\nread1 SHA256 %s\nread2 SHA256 %s" % (hashes[0], hashes[1]))

    good_pair = None
    if hashes[0] == hashes[1]:
        good_pair = (reads[0], reads[1])
    else:
        d, first = count_diff(read_bytes(reads[0]), read_bytes(reads[1]))
        log("MISMATCH: %d bytes differ, first at 0x%06x. Doing a third read to break the tie..." %
            (d, first if first is not None else 0))
        path, _ = chip.read(0, size, os.path.join(raw_dir, "read3.bin"))
        h3 = sha256_file(path)
        log("read3 SHA256 %s" % h3)
        reads.append(path)
        if h3 == hashes[0]:
            good_pair = (reads[0], path)
        elif h3 == hashes[1]:
            good_pair = (reads[1], path)
        else:
            log("All three reads differ. Trying a byte-wise 2-of-3 majority vote...")
            image = vote_or_fail(reads, raw_dir)
            good_pair = None

    if good_pair:
        image = read_bytes(good_pair[0])

    extra = {
        "created": started,
        "port": port,
        "baud": args.baud,
        "tact_ms": args.tact,
        "flasher": os.path.relpath(flasher, HERE) if flasher.startswith(HERE) else flasher,
        "flasher_commit": read_pinned_commit(args.tool_dir),
        "size_mode": args.size,
        "bytes_read_per_pass": size,
        "method": "two identical reads" if good_pair else "2-of-3 byte majority",
        "reads_agreeing": [os.path.basename(p) for p in good_pair] if good_pair else None,
        "reads_voted": None if good_pair else [os.path.basename(p) for p in reads],
        "flasher_calls": reader.calls,
        "manual_resets": chip.catches,
    }
    return finalize(run_dir, raw_dir, image, size, extra)


def vote_or_fail(reads, raw_dir):
    datas = [read_bytes(p) for p in reads]
    image, no_major, changed = majority_vote(datas)
    names = [os.path.basename(p) for p in reads]
    for blk in sorted(changed):
        log("  block 0x%06x: outvoted %s" % (blk, ", ".join(names[k] for k in changed[blk])))
    if no_major:
        rng = offsets_to_ranges(no_major)
        raise BackupError("No 2-of-3 majority for %d bytes, in %s. Re-read those ranges. "
                          "Raw reads kept in %s" %
                          (len(no_major), ", ".join("0x%06x-0x%06x" % (a, b) for a, b in rng), raw_dir))
    log("Majority vote OK: every byte agreed in at least 2 reads.")
    return image


def finalize(run_dir, raw_dir, image, size, extra):
    # ---- 3. sanity checks --------------------------------------------------
    log("\n[4/4] Checking the image...")
    check_not_blank(image, "The full dump")

    detected = size
    if size == SIZE_1M and image[:SIZE_512K] == image[SIZE_512K:]:
        # The probe said 1 MB but the full image is mirrored: it is a 512 KB chip.
        log("Both 512 KB halves are identical, so this is a 512 KB flash (address wrap).")
        detected = SIZE_512K

    info = describe_image(image[:detected])
    if fill_ratio(image[:detected], 0xFF) > 0.98:
        log("WARNING: %.1f%% of the image is 0xFF. Keep it, but double-check it looks like "
            "firmware before relying on it." % (100 * info["ff_ratio"]))
    if not info["telink_magic_at_0x0"] and not info["telink_magic_at_0x20000"]:
        log("WARNING: no Telink 'KNLT' signature at 0x0 or 0x20000.")

    # ---- 4. write the result ----------------------------------------------
    final_name = "stock_full_%s.bin" % human_size(detected)
    final_path = os.path.join(run_dir, final_name)
    with open(final_path, "wb") as f:
        f.write(image[:detected])
    final_hash = sha256_file(final_path)

    sums_path = os.path.join(run_dir, "SHA256SUMS")
    with open(sums_path, "w") as f:
        f.write("%s  %s\n" % (final_hash, final_name))
        for p in sorted(glob.glob(os.path.join(raw_dir, "*.bin"))):
            f.write("%s  raw/%s\n" % (sha256_file(p), os.path.basename(p)))

    meta = dict(extra)
    meta.update({
        "finished": datetime.datetime.now().isoformat(timespec="seconds"),
        "host": platform.platform(),
        "python": sys.version.split()[0],
        "flash_size": detected,
        "image": final_name,
        "sha256": final_hash,
        "checks": info,
    })
    with open(os.path.join(run_dir, "backup_info.json"), "w") as f:
        json.dump(meta, f, indent=2)

    for p in [final_path, sums_path] + glob.glob(os.path.join(raw_dir, "*.bin")):
        make_read_only(p)

    log("\n" + "=" * 60)
    log("BACKUP OK")
    log("  Image:   %s" % final_path)
    log("  Size:    %s" % human_size(detected))
    log("  SHA256:  %s" % final_hash)
    log("  Telink signature at 0x0: %s, at 0x20000: %s" %
        (info["telink_magic_at_0x0"], info["telink_magic_at_0x20000"]))
    log("  Method:  %s" % meta.get("method"))
    if meta.get("manual_resets") is not None:
        log("  Manual 3V3 resets needed: %d" % meta["manual_resets"])
    log("Copy the whole %s folder somewhere off this Mac before writing anything." %
        os.path.basename(run_dir))
    log("The chip was halted for the read: power-cycle the board to run it again.")
    log("=" * 60)
    return 0


def read_pinned_commit(tool_dir):
    p = os.path.join(tool_dir, "PINNED_COMMIT")
    if os.path.isfile(p):
        with open(p) as f:
            return f.read().strip()
    return None


def cmd_verify(args):
    run_dir = args.dir
    sums = os.path.join(run_dir, "SHA256SUMS")
    if not os.path.isfile(sums):
        raise BackupError("No SHA256SUMS in %s" % run_dir)
    ok = True
    with open(sums) as f:
        lines = f.read().splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        want, name = line.split(None, 1)
        path = os.path.join(run_dir, name.strip())
        if not os.path.isfile(path):
            log("MISSING  %s" % name)
            ok = False
            continue
        got = sha256_file(path)
        log("%s  %s" % ("OK      " if got == want else "CHANGED ", name))
        ok = ok and got == want
    images = glob.glob(os.path.join(run_dir, "stock_full_*.bin"))
    for img in images:
        data = read_bytes(img)
        try:
            check_not_blank(data, os.path.basename(img))
        except BackupError as e:
            log("BAD      %s" % e)
            ok = False
    log("All files match." if ok else "VERIFY FAILED")
    return 0 if ok else 1


def cmd_merge(args):
    """Build the image offline from 3+ full reads already in a run folder (no hardware)."""
    run_dir = os.path.abspath(args.dir)
    raw_dir = os.path.join(run_dir, "raw")
    open_run_log(args.log or os.path.join(os.path.dirname(os.path.dirname(run_dir)), LOG_NAME),
                 "merge " + run_dir)
    if glob.glob(os.path.join(run_dir, "stock_full_*.bin")):
        raise BackupError("%s already has a stock_full_*.bin. Not touching it." % run_dir)
    if args.reads:
        reads = [p if os.path.isabs(p) else os.path.join(raw_dir, p) for p in args.reads]
    else:
        cands = sorted(glob.glob(os.path.join(raw_dir, "read*.bin")))
        sizes = [os.path.getsize(p) for p in cands]
        full = max(sizes) if sizes else 0
        reads = [p for p, n in zip(cands, sizes) if n == full and n in (SIZE_512K, SIZE_1M)]
    if len(reads) < 3:
        raise BackupError("Need 3 or more complete reads to vote, found: %s" %
                          ", ".join(os.path.basename(p) for p in reads))
    size = os.path.getsize(reads[0])
    log("Merging %d reads of %s: %s" % (len(reads), human_size(size),
                                       ", ".join(os.path.basename(p) for p in reads)))
    for p in reads:
        log("  %s  %s" % (sha256_file(p), os.path.basename(p)))
    image = vote_or_fail(reads, raw_dir)
    extra = {
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "bytes_read_per_pass": size,
        "method": "2-of-3 byte majority (merge command, offline)",
        "reads_voted": [os.path.basename(p) for p in reads],
        "flasher_commit": read_pinned_commit(DEFAULT_TOOL_DIR),
    }
    return finalize(run_dir, raw_dir, image, size, extra)


def cmd_ports(args):
    ports = list_candidate_ports()
    if not ports:
        log("No USB-UART adapters found.")
        return 1
    for p in ports:
        log(p)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only stock firmware backup for TLSR8250 (via pvvx TLSR825xComFlasher rf).")
    sub = ap.add_subparsers(dest="cmd")

    b = sub.add_parser("backup", help="Read the flash twice, verify and save it")
    b.add_argument("-p", "--port", help="Serial port, e.g. /dev/cu.usbserial-0001 (auto-detected if only one)")
    b.add_argument("-b", "--baud", type=int, default=921600, help="UART baud (default 921600, flasher min 340000)")
    b.add_argument("-t", "--tact", type=int, default=10000,
                   help="Activation window in ms for the first read (default 10000: time to plug "
                        "3V3 back in by hand; later reads use 0 = no reset)")
    b.add_argument("-c", "--clk", type=int, default=48,
                   help="SWire CLK in MHz to try first (default 48; the flasher then falls back "
                        "to 16/24/32/48; 0 = its auto-detect)")
    b.add_argument("--fastcatch", action="store_true",
                   help="Experimental: activation loop without idle gaps (did worse in testing)")
    b.add_argument("--retries", type=int, default=2,
                   help="If a read fails, ask for another 3V3 reset and retry this many times (default 2)")
    b.add_argument("--no-wait", dest="wait", action="store_false",
                   help="Don't pause for Enter before asking you to plug 3V3 back in")
    b.add_argument("--size", choices=["auto", "512k", "1m"], default="auto", help="Flash size (default auto-detect)")
    b.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="Where backups go (default ./backups)")
    b.add_argument("--tool-dir", default=DEFAULT_TOOL_DIR, help=argparse.SUPPRESS)
    b.add_argument("--flasher", help=argparse.SUPPRESS)  # used by the tests
    b.add_argument("--log", help="Log file (default: backup.log next to the backups folder; "
                                 "every run appends to it)")
    b.add_argument("--timeout-per-kb", type=float, default=10.0,
                   help="Abort a read that takes longer than this many seconds per KB (default 10)")

    v = sub.add_parser("verify", help="Re-check an existing backup folder against its SHA256SUMS")
    v.add_argument("dir")

    m = sub.add_parser("merge", help="Build the image from 3+ full reads in a run folder by "
                                     "byte-wise majority vote (offline, no hardware)")
    m.add_argument("dir")
    m.add_argument("reads", nargs="*", help="Reads to vote (default: every full-size raw/read*.bin)")
    m.add_argument("--log", help=argparse.SUPPRESS)

    sub.add_parser("ports", help="List USB-UART ports")

    args = ap.parse_args(argv)
    args.argv = argv
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        return {"backup": cmd_backup, "verify": cmd_verify, "merge": cmd_merge,
                "ports": cmd_ports}[args.cmd](args)
    except BackupError as e:
        log("\nERROR: %s" % e)
        return 1
    finally:
        close_run_log()


if __name__ == "__main__":
    sys.exit(main())
