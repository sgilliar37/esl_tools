# Command-line manual: back up, find and update the tags

This manual covers every command-line tool in this repository for working with the
PickSmart 2.13" tags (Telink TLSR8250, ST7305 reflective LCD). Every command, flag and
default below is taken from the scripts themselves; if a script changes, check its
`--help` too.

All paths assume the repository lives at `~/ATC_TLSR_Paper` on the Mac (the same layout
as this repo). Commands are run from the folder named in each section.

**Contents**

1. [Safety rules (read first)](#1-safety-rules-read-first)
2. [Which tag is which](#2-which-tag-is-which)
3. [One-time setup](#3-one-time-setup)
4. [Hardware: adapter, wiring and catching the chip](#4-hardware-adapter-wiring-and-catching-the-chip)
5. [Full flash backup over SWire](#5-full-flash-backup-over-swire)
6. [Finding tags over Bluetooth](#6-finding-tags-over-bluetooth)
7. [Updating firmware over Bluetooth](#7-updating-firmware-over-bluetooth)
8. [Password lock](#8-password-lock)
9. [Wired flashing over SWire](#9-wired-flashing-over-swire)
10. [Rebuilding the firmware](#10-rebuilding-the-firmware)
11. [Using the raw flasher directly](#11-using-the-raw-flasher-directly)
12. [Tests and Tag Designer from the command line](#12-tests-and-tag-designer-from-the-command-line)
13. [Troubleshooting](#13-troubleshooting)
14. [Quick reference](#14-quick-reference)

---

## 1. Safety rules (read first)

1. **Always make and verify a full flash backup before any write or erase.** The wired
   tools (`flash.sh`, `unlock_usb.sh`) refuse to run unless the stock backup
   `stock_backup/backups/stock_20261113_175437/stock_full_512KB.bin` is present and its
   SHA256 is `37eb9d79d0e01f46af9c5ddabef57b9cc8a4a09f36d6724f6ba7b36f16affade`.
   `ota.sh restore` needs the same file. Keep a copy of that whole folder off the Mac;
   it is not in Git (it holds the tag's MAC and pairing data).
2. **Do not alter the working tags `ESL_A7E16A` and `ESL_8B062B`.** They run firmware
   **v2.0** with working screens. Do not send them any firmware, restore or unlock
   without a deliberate decision to do so.
3. **Know what `./ota.sh` sends by default.** With no file argument it sends
   `atc_port/ATC_Paper_picksmart.bin`, which is the password-lock build, not v2.0.
   The lock builds were developed on top of v2.1 (`_v3.0.bin` uses the v2.1 screen
   driver, which has never been confirmed on working glass). The current default
   (v3.1) goes back to the v2.0 driver, but it has only run on `ESL_8079EB`, whose
   screen is broken. So never point a plain `./ota.sh --name ...` at a working tag. To
   put exactly what the working tags run on a tag, name the file:
   `./ota.sh ATC_Paper_picksmart_v2.0.bin --name ESL_XXXXXX`.
4. Without `--name`, `./ota.sh` only ever targets the tag the stock backup came from
   (`NEMR62616747` / `ESL_8079EB`). It never picks another tag by itself.
5. **After a Bluetooth update a tag may not come back on its own.** If it does not
   reappear within a minute or two, take the battery out for 5 seconds and put it back.
6. Never connect the adapter's 3V3 while the tag's battery is in, and never connect 5V
   to the board. Run tags from their own battery for Bluetooth work (a tag powered from
   the CP2102's 3V3 has dropped off Bluetooth before).
7. Every wired write asks you to type a word (`WRITE`, `UNLOCK`) and every Bluetooth
   update asks for `YES`. Read the line above the prompt before you type it.

## 2. Which tag is which

| Tag (port name) | Stock name | Firmware now | Screen | Notes |
|---|---|---|---|---|
| `ESL_A7E16A` | probably `NEMR62616005` | v2.0 | works | **Do not alter.** |
| `ESL_8B062B` | `NEMR62616020` | v2.0 | works | **Do not alter.** First tag that got a Bluetooth update. |
| `ESL_8079EB` | `NEMR62616747` | test builds (v2.0 / v3.x) | blank (hardware fault, also blank on stock) | The wired, backed-up tag (MAC `A0:40:FA:80:79:EB`). Use it as the test tag. |

How names work:
- Stock PickSmart firmware advertises `NEMR` + the 32-bit ID stored at flash `0x7F000`.
- This project's firmware advertises `ESL_` + the last three MAC bytes (MAC at flash
  `0x76000`). Both use the same Bluetooth address, so the Mac sees the same device
  before and after an update.

Firmware images in `atc_port/`:

| File | What it is |
|---|---|
| `ATC_Paper_picksmart.bin` | v3.1, the default for `ota.sh` and `flash.sh`: v2.0 screen driver + password lock |
| `ATC_Paper_picksmart_v2.0.bin` | v2.0: what the working tags run (no lock) |
| `ATC_Paper_picksmart_v2.1.bin` | v2.1 screen driver, not confirmed on working glass |
| `ATC_Paper_picksmart_v3.0.bin` | v2.1 driver + password lock, not confirmed on working glass |
| `ATC_Paper_picksmart_v1.bin` | first port |

All of these are in Telink OTA format (`KNLT` at byte 8, Telink CRC32 in the last 4 bytes).

## 3. One-time setup

All tools share one Python virtual environment in `stock_backup/.venv`.

```bash
cd ~/ATC_TLSR_Paper/stock_backup
./setup.sh
```

`setup.sh`:
- uses `python3` (override with `PYTHON=/path/to/python3 ./setup.sh`);
- creates `.venv` and installs `pyserial>=3.5`;
- checks that the vendored flasher `tools/TlsrComSwireWriter/TLSR825xComFlasher-macos.py`
  is there and prints the pinned pvvx commit;
- runs the simulated-flash self-test (no adapter needed) and ends with `Setup OK`.

The Bluetooth library (`bleak==3.1.0`) is installed into the same venv the first time
you run `./ota.sh` (about a minute). macOS will ask to allow Bluetooth for Terminal the
first time.

## 4. Hardware: adapter, wiring and catching the chip

Only the wired tools (`backup.sh`, `flash.sh`, `unlock_usb.sh`) need this. Bluetooth
tools need nothing but a powered tag near the Mac.

### Adapter

The adapter is an HJHYUL CP2102 USB-UART. It exposes only **3V3, TXD, RXD, GND, 5V**.
It has **no DTR or RTS pin**, so the tools cannot reset the chip; you reset it by hand
by plugging 3V3 in at the right moment (see "Catching the chip"). Set it to 3.3 V
logic. A 4-pin 2.54 mm clip and a **1 kΩ resistor** are needed.

Find its port:

```bash
cd ~/ATC_TLSR_Paper/stock_backup
./.venv/bin/python backup_stock_firmware.py ports
```

Expect `/dev/cu.usbserial-XXXX` (or `/dev/cu.SLAB_USBtoUART` with the vendor driver).
macOS lists every adapter twice (`cu.*` and `tty.*`); the tools use `cu.*`. If only one
adapter is plugged in, every tool picks it automatically and `--port` is optional.

### Board header

The board has two rows of through-hole header holes (2.54 mm):

- Lower group: **3V3, RST, SWM, SWS, GND**. Use this one for flashing. SWM stays unconnected.
- Upper group: GND, RX, TX, 3V3. This is the tag's own UART (logs), not used here.

### Wiring for SWire

```
CP2102            Tag (lower header)
------            ------------------
3V3  ------------ 3V3     (battery OUT while this is connected)
GND  ------------ GND
TXD  --[ 1 kΩ ]--+
                 +------- SWS        (TX through 1 kΩ and RX joined at SWS)
RXD  ------------+
5V                 not connected
                   RST: not wired (reset is done by replugging 3V3, or tapping RST to GND)
```

Check the wiring without touching the chip (nothing is written, no catch needed):

```bash
cd ~/ATC_TLSR_Paper/atc_port
./flash.sh check                      # live loop: wiggle the clip and watch, Ctrl-C to stop
```

It sends a pattern on TX and expects it straight back on RX through SWS. `OK: adapter
hears its own signal on SWS` means TX/RX/SWS are joined properly. `nothing comes back`
means RX is not joined to SWS; `line stuck low` means SWS is touching GND.

### Catching the chip

The flasher has to halt the CPU right after power-up ("activation"). Without RTS/DTR,
you do it by hand:

1. **Start with the 3V3 wire unplugged** (GND, TX and RX stay connected).
2. Run the tool and press Enter when it asks.
3. When it prints `Activate` (the tools also beep and show `>>> PLUG 3V3 IN NOW <<<`),
   **plug 3V3 in**.
4. A `Chip answered` / `Chip caught` line confirms it. This takes about **1 try in 3**;
   the tools retry and tell you when to unplug again.

Alternative for `flash.sh` / `unlock_usb.sh`: `--rst` keeps 3V3 connected and asks you
to tap RST to GND for about half a second instead.

Facts that the scripts already handle, worth knowing when reading the logs:

- Speeds that work: **921600 baud** (`-b 921600`) and **SWire clock 48 MHz** (`-c 48`).
  `-c` is only the first clock tried; the flasher falls back through 16/24/32/48.
- After one catch the CPU stays halted, so **every later read runs with `-t 0`**
  (no activation, no soft reset) and needs no replugging.
- **The first block read after a catch comes back as zeros.** The tools do a throwaway
  256-byte "catch" read first and ignore it.
- Reads run at about **0.5 KB/s** (one USB round trip per byte), so **512 KB takes about
  12 to 17 minutes** per full read. Baud and `-c` do not change this much.
- Occasional bad blocks happen, so the backup uses a **2-of-3 majority vote**.
- The `--fastcatch` flasher variant caught worse in testing; leave it off.
- After any wired session, unplug 3V3 and plug it back in (or put the battery back) so
  the tag starts normally. A tag left halted by a missed catch does not advertise.

## 5. Full flash backup over SWire

Read-only. The backup script only ever calls the flasher's `rf` (read flash) command;
it refuses to build any write or erase command, never overwrites a file, and makes the
finished backup read-only.

```bash
cd ~/ATC_TLSR_Paper/stock_backup
./backup.sh                                   # = backup_stock_firmware.py backup
./backup.sh --port /dev/cu.usbserial-0001
```

`backup.sh` passes every option straight to `backup_stock_firmware.py backup`:

| Option | Default | Meaning |
|---|---|---|
| `-p`, `--port PORT` | auto if one adapter | Serial port |
| `-b`, `--baud N` | `921600` | UART speed (flasher minimum 340000). Try `460800` if reads keep disagreeing |
| `-t`, `--tact MS` | `10000` | Activation window for the first (catch) read, in ms: your time to plug 3V3 in. Later reads always use 0. Raise it (e.g. `-t 15000`) if you keep missing |
| `-c`, `--clk MHz` | `48` | SWire clock to try first (`0` = flasher auto-detect) |
| `--retries N` | `2` | If a read fails, ask for another 3V3 catch and retry this many times |
| `--no-wait` | off | Don't pause for Enter before the replug prompt |
| `--size auto\|512k\|1m` | `auto` | Flash size. `512k` skips the size probes (this board is 512 KB) |
| `--out-dir DIR` | `./backups` | Where backup folders go |
| `--log FILE` | `stock_backup/backup.log` | Log file; every run appends with a `# RUN <time>` header |
| `--timeout-per-kb S` | `10` | Abort a read that takes longer than this many seconds per KB |
| `--fastcatch` | off | Experimental activation loop; did worse, leave off |

What a run does:

1. **Catch**: one 256-byte throwaway read with activation (`-t`), as in section 4.
2. **Probe** (skipped with `--size 512k`): reads 16 KB at `0x000000` and at `0x080000`.
   A 512 KB chip mirrors, so they match. All-FF or all-00 stops here.
3. **Two full reads** compared by SHA256. If they differ, a third read; if two of three
   match, that one is kept. If all three differ, the image is built by a byte-wise
   **2-of-3 majority vote**, and the outvoted blocks are listed. If any byte has no
   majority, it stops and prints the address ranges.
4. **Checks**: rejects all-FF / all-00, warns if there is no Telink `KNLT` signature.
5. Writes the result and makes it read-only. A good run ends with `BACKUP OK`, the
   image path and its SHA256.

Result layout:

```
stock_backup/backups/stock_YYYYmmdd_HHMMSS/
  stock_full_512KB.bin    the backup (read-only)
  SHA256SUMS              hashes of the image and every raw read
  backup_info.json        port, baud, flash size, flasher commit, checks
  raw/                    catch read, probes, every full read
stock_backup/backup.log   one log for all runs
```

Expect about 35 to 50 minutes for a whole backup (two or three full reads).

### Verify a backup (do this before any write or erase)

```bash
./.venv/bin/python backup_stock_firmware.py verify backups/stock_20261113_175437
```

Re-hashes the image and every raw read against `SHA256SUMS`. Also check the image's
SHA256 against the one you recorded (`shasum -a 256 backups/.../stock_full_512KB.bin`).
Two backups taken on different days with the same SHA256 is the strongest proof.

### Rebuild an image from reads already on disk (offline)

```bash
./.venv/bin/python backup_stock_firmware.py merge backups/stock_YYYYmmdd_HHMMSS
./.venv/bin/python backup_stock_firmware.py merge backups/stock_... raw/read1.bin raw/read2.bin raw/read3.bin
```

Byte-wise majority vote over 3 or more full reads (default: every full-size
`raw/read*.bin`). It refuses if the folder already has a `stock_full_*.bin`.

### Other subcommand

```bash
./.venv/bin/python backup_stock_firmware.py ports    # list USB-UART ports
```

The existing stock backup: `backups/stock_20261113_175437/stock_full_512KB.bin`,
512 KB, SHA256 `37eb9d79d0e01f46af9c5ddabef57b9cc8a4a09f36d6724f6ba7b36f16affade`
(2-of-3 majority of three reads, taken from `NEMR62616747`).

## 6. Finding tags over Bluetooth

```bash
cd ~/ATC_TLSR_Paper/atc_port
./ota.sh --scan
```

Scans for up to 10 seconds (and tries up to 3 times, because the first scan after
starting often hears nothing on macOS), then for every tag in range prints:

- its name and signal strength (strongest first);
- what its advert says, e.g. `ATC-style advert: MAC A0:40:FA:80:79:EB, battery 87%`
  or `PickSmart advert`;
- which firmware it runs, found by connecting briefly and reading its services:
  - `stock PickSmart firmware` (service FEF0),
  - `the new firmware from this project` (1F10 + Telink OTA),
  - `atc1441's original ATC firmware (not this project's)` (1F10 + 221F).

The scan cannot tell v2.0 from v3.x; Tag Designer's tag list shows a 🔒 badge for
locked tags. If the backed-up tag is not in range, the scan says so and suggests a
restart.

Tags it recognises: names starting with `NEMR` or `ESL_`, or anything advertising the
Telink OTA service or PickSmart manufacturer data. A tag running this project's firmware
puts its name only in the scan response; the tool rebuilds the `ESL_` name from the MAC
in the advert when that packet is missed.

If a tag does not show up: check its battery, bring it next to the Mac, and if it was
just wired for SWire, restart it once (RST to GND, or battery out for 5 s).

## 7. Updating firmware over Bluetooth

No wires and no catching: the tag only needs power. This is the normal way to update.

```bash
cd ~/ATC_TLSR_Paper/atc_port
./ota.sh                                                   # ATC_Paper_picksmart.bin to the backed-up tag
./ota.sh --name ESL_8079EB                                 # same file, to the named tag
./ota.sh ATC_Paper_picksmart_v2.0.bin --name ESL_8079EB    # a specific file
./ota.sh restore                                           # original PickSmart firmware to the backed-up tag
./ota.sh restore --name ESL_XXXXXX                         # ... to another tag
./ota.sh test                                              # offline tests, no Bluetooth
```

Arguments (`ota_update.py`):

| Argument | Default | Meaning |
|---|---|---|
| `what` | `firmware` | `firmware` = `ATC_Paper_picksmart.bin`; `restore` = stock firmware cut from the stock backup (needs the backup, checks its SHA256); or a path to any Telink-format `.bin` (relative to `atc_port/`) |
| `--name NAME` | the backed-up tag | Update this tag instead (exact advertised name, e.g. `ESL_8079EB` or `NEMR62616747`) |
| `--scan` | | Only list tags (section 6) |
| `--sync N` | `16` | Packets between delivery checks. Lower it if packets get lost, raise it if the tag times out (the tool adjusts this itself between tries) |
| `--yes` | off | Don't ask before sending (avoid this) |

What happens:

1. The image is checked before anything is sent: `KNLT` mark, size between 16 KB and
   124 KB, length a multiple of 16 plus 4, and a correct Telink CRC32. A bad image is
   refused with the reason.
2. The tool looks for the target tag by name for about half a minute. Without
   `--name` the target is the tag the stock backup came from (`NEMR62616747` or
   `ESL_8079EB`); no other tag is ever picked.
3. It asks `Send <file> to <tag>? Type YES to go ahead`.
4. If the tag runs v3 or later and is locked, it unlocks it first with Tag Designer's
   password from the macOS Keychain (section 8). If it cannot, nothing is sent.
5. It sends the image, showing progress. The tag writes into its spare flash bank and
   switches only after the whole-image CRC matches. If anything fails (Bluetooth drop,
   lost packet, the tag's 180-second limit), the tag throws the partial copy away and
   keeps its current firmware. The tool tries up to 3 times.
6. It waits up to a minute for the same tag (same Bluetooth address) to come back
   with the expected name prefix (`ESL_` after a firmware update, `NEMR` after
   `restore`) and prints `DONE`.

If it says `The transfer finished, but ... has not come back`: wait a minute and run
`./ota.sh --scan`. If the tag still does not appear, **power-cycle it (battery out for
5 seconds)**; this was needed after the first v3 update.

Every run is logged in `atc_port/ota.log`.

**Before you update a tag, re-read the safety rules in section 1.** In particular,
`ESL_A7E16A` and `ESL_8B062B` stay on v2.0, and the default image is a lock build.

## 8. Password lock

Firmware v3 and later can be locked with a password. A locked tag refuses pictures,
settings, clock changes and Bluetooth firmware updates until the connection proves it
knows the tag's key. The unlock lasts for one connection.

### Setting, changing or removing the password

This is done in **Tag Designer** (its "Password lock" panel), not from the command line.
`atc_port/tag_lock.py` is the library Tag Designer and `ota.sh` use; it has no command
line of its own.

How the keys work, in short:
- Your app password (at least 10 characters) becomes a master key with PBKDF2-SHA256
  (600,000 rounds). Only the master is kept, in the macOS Keychain item
  `Tag Designer tag lock`; the password itself is never stored.
- Each tag gets its own key, derived from the master and a random salt the tag stores.
  The app keeps the two previous masters too and moves tags to the current one
  automatically.
- A tag allows 3 wrong answers per connection; after 6 wrong answers in a row it allows
  only one try per 30 seconds.

### Updating a locked tag

Nothing extra: `./ota.sh` unlocks with the Keychain password before sending and prints
`Unlocked <tag> with Tag Designer's password.` If the password does not match, it stops
with `<tag> is locked with a password: ... Nothing was sent.`

### Lost password: unlock over USB-UART

```bash
cd ~/ATC_TLSR_Paper/atc_port
./unlock_usb.sh
./unlock_usb.sh --port /dev/cu.usbserial-XXXX
./unlock_usb.sh --rst                 # tap RST instead of replugging 3V3
```

Same wiring and catch as section 4. It is `flash_atc.py unlock` and takes the same
options as `flash.sh` (section 9). It:

1. refuses to run unless the stock backup is present and intact;
2. catches the chip, then reads the two password sectors `0x7A000` and `0x7B000`;
3. if both are blank, says `This tag has no password stored` and stops;
4. otherwise asks you to type `UNLOCK`, erases **only** those two 4 KB sectors, and
   checks they read back blank.

Firmware, pictures and settings are untouched. Afterwards unplug 3V3 and plug it back
in (or put the battery back); the tag has no password until you lock it again in Tag
Designer.

## 9. Wired flashing over SWire

Use this only when Bluetooth is not possible (for example a tag that no longer boots).
Bluetooth OTA (section 7) is the normal route.

```bash
cd ~/ATC_TLSR_Paper/atc_port
./flash.sh                            # write ATC_Paper_picksmart.bin at 0x0, then verify
./flash.sh restore                    # write the full 512 KB stock backup back, then verify
./flash.sh check                      # live wiring check (writes nothing)
./flash.sh --port /dev/cu.usbserial-XXXX
```

Options (`flash_atc.py`):

| Option | Default | Meaning |
|---|---|---|
| `what` | `firmware` | `firmware`, `restore`, `check` (wiring loop) or `unlock` (section 8) |
| `-p`, `--port PORT` | auto if one adapter | Serial port |
| `-b`, `--baud N` | `921600` | UART speed |
| `-c`, `--clk MHz` | `48` | SWire clock to try first |
| `--rst` | off | Keep 3V3 connected and tap RST to GND instead of replugging 3V3 |
| `--tries N` | `20` | Catch attempts before giving up |

`flash.sh` always writes `ATC_Paper_picksmart.bin` (or the stock backup with
`restore`); it has no option for another file. To put v2.0 on a tag, use Bluetooth:
`./ota.sh ATC_Paper_picksmart_v2.0.bin --name ...`.

Steps:

1. **Safety check**: refuses to run unless the stock backup is present with the expected
   SHA256, and the image has the `KNLT` mark.
2. **Step 1, catch**: first an echo test of the wiring, then up to `--tries` catch
   attempts (activation window 10 s each). A catch only counts when a later read shows
   the `KNLT` header (the first read after a catch is zeros).
3. **Step 2, write**: prints the range it will erase and rewrite and asks you to type
   `WRITE`. Then runs the flasher's `wf 0 <image>` with `-t 0`.
4. **Step 3, read back and compare** in 16 KB pieces (each piece retried up to 3 times).
   `DONE: every byte matches` means success.

If the write or verify reports a problem: **do not unplug**. The chip is still halted,
so run the same command again. If it keeps failing, `./flash.sh restore`.

When done, unplug 3V3 and plug it back in to start the new firmware.

Every run is logged in `atc_port/flash.log`, including each flasher command line.

## 10. Rebuilding the firmware

```bash
cd ~/ATC_TLSR_Paper          # or a clone of this repository
firmware/build.sh
```

Needs Linux on x86-64 (it uses atc1441's bundled `tc32` toolchain), `git`, `make` and
`python3`. It does not run on the Mac directly; use a Linux machine, VM or container.

What it does:

1. Clones `https://github.com/atc1441/ATC_TLSR_Paper` into `firmware/build/` (first run
   only) and checks out commit `3778d29`.
2. Cleans `Firmware/src` and applies `atc_port/picksmart_st7305.patch` (ST7305 LCD
   driver, picture keep/read-back, password lock).
3. Runs `make` in `Firmware/`.
4. Converts `ATC_Paper.bin` to Telink OTA format with `atc_port/telink_ota.py` and writes
   `firmware/out/ATC_Paper_picksmart.bin`, printing its size and SHA256.

Compare the printed SHA256 with the one in `atc_port/PORTING_NOTES.md` for that version.
To send the result: `cd atc_port && ./ota.sh ../firmware/out/ATC_Paper_picksmart.bin --name ESL_8079EB`
(test on `ESL_8079EB` first). `firmware/build/` and `firmware/out/` are not in Git.

## 11. Using the raw flasher directly

You should not need this; the scripts above wrap it with the safety checks. For
reference, the vendored tool is pvvx's TlsrComSwireWriter (pinned commit in
`stock_backup/tools/TlsrComSwireWriter/PINNED_COMMIT`). On macOS use the `-macos`
variant. Global options go **before** the command:

```bash
cd ~/ATC_TLSR_Paper/stock_backup
PY=./.venv/bin/python
F=tools/TlsrComSwireWriter/TLSR825xComFlasher-macos.py

# first read: activation, plug 3V3 in at "Activate"
$PY -u $F -p /dev/cu.usbserial-XXXX -b 921600 -t 10000 -c 48 rf 0x0 0x100 catch.bin
# later reads: -t 0, the CPU stays halted
$PY -u $F -p /dev/cu.usbserial-XXXX -b 921600 -t 0 -c 48 rf 0x0 0x80000 read1.bin
```

Commands: `rf ADDR SIZE FILE` (read), `wf ADDR FILE` (erase sectors, then write),
`we ADDR FILE` (write without erasing), `es ADDR SIZE` (erase sectors) and `ea` (erase
all). **`wf`, `we`, `es` and `ea` change the flash: only
use them with a verified backup, and prefer `flash.sh` / `unlock_usb.sh`.** The flasher
exits 0 even when stopped with Ctrl-C and leaves a partial file, so always check the
output size.

## 12. Tests and Tag Designer from the command line

Offline tests (no hardware):

```bash
cd ~/ATC_TLSR_Paper
stock_backup/.venv/bin/python -m unittest discover -s stock_backup/tests   # or ./stock_backup/setup.sh
python3 atc_port/tests/test_lock.py
python3 tag_designer/tests/test_designer.py
atc_port/ota.sh test              # needs the stock backup
```

Tag Designer is normally started by double-clicking `Tag Designer.command` or
`Tag Designer.app`. From a terminal, `tag_designer/server.py` accepts:

- `--demo`: simulated tags, no Bluetooth needed;
- `--window`: open in its own window instead of the browser;
- `--no-browser`: start the server without opening a browser.

See `tag_designer/README.md` for using the app.

## 13. Troubleshooting

| Symptom | What to do |
|---|---|
| `Run ../stock_backup/setup.sh first.` | Run `stock_backup/setup.sh` once (section 3) |
| `No USB-UART adapter found` | Try another USB cable (some are charge-only); check `backup_stock_firmware.py ports` |
| `Wiring check failed before catching` | Run `./flash.sh check` and fix the clip until it says `OK` |
| `Chip sleep?` / `Wrong RX-TX connection` / missed catch | Plug 3V3 in sooner after `Activate`, or give yourself longer (`./backup.sh -t 15000`). Expect about 1 catch in 3 tries |
| Reads disagree / short reads | Shorter wires, `-b 460800`, power from the battery instead of the adapter (with 3V3 left unconnected) |
| `The stock backup is missing or changed` | Put the verified backup folder back at `stock_backup/backups/stock_20261113_175437/` |
| `No tag to update: name one with --name` | The stock backup is missing, so the default target is unknown. Use `--name` |
| Tag not found over Bluetooth | Check the battery, move it closer, run `./ota.sh --scan` again; restart it if it was wired (RST to GND or battery out 5 s) |
| `... is locked with a password` | Set the right password in Tag Designer, or clear it with `./unlock_usb.sh` |
| Tag did not come back after an update | Wait a minute, `./ota.sh --scan`, then battery out for 5 s |
| `Gave up after 3 tries` | Nothing is damaged; the tag keeps its firmware. Move closer and try again |
| `Image not accepted` | The file is not Telink OTA format or is the wrong size; use a file from `atc_port/` or `firmware/out/` |

## 14. Quick reference

```bash
# setup (once)
cd ~/ATC_TLSR_Paper/stock_backup && ./setup.sh

# backup (wired, read-only) and verify
cd ~/ATC_TLSR_Paper/stock_backup
./backup.sh --size 512k
./.venv/bin/python backup_stock_firmware.py verify backups/stock_YYYYmmdd_HHMMSS

# find tags
cd ~/ATC_TLSR_Paper/atc_port && ./ota.sh --scan

# update over Bluetooth (test tag first; never the working v2.0 tags)
./ota.sh ATC_Paper_picksmart_v2.0.bin --name ESL_8079EB
./ota.sh --name ESL_8079EB                  # default image = v3.1 lock build
./ota.sh restore --name ESL_8079EB          # back to stock

# wired
./flash.sh check
./flash.sh            # / ./flash.sh restore
./unlock_usb.sh       # lost password

# rebuild (Linux x86-64)
firmware/build.sh
```
