# Stock firmware backup (TLSR8250, 2.13" B/W e-ink)

Everything here **only reads** the flash. The script calls pvvx's
`TLSR825xComFlasher` with its `rf` command and nothing else; it refuses to build
any `wf`/`we`/`es`/`ea` command, never overwrites a file, and makes the finished
backup read-only. Do not write any new firmware until this backup has passed.

## What's in this folder

| File | Purpose |
|---|---|
| `setup.sh` | One-time: makes `.venv` with pyserial and runs the self-test (no hardware) |
| `backup.sh` | Runs the backup. Options are passed through (see below) |
| `backup_stock_firmware.py` | The logic: `backup`, `verify <folder>`, `ports` |
| `tools/TlsrComSwireWriter/` | pvvx's flasher, pinned to the commit in `PINNED_COMMIT` (public domain). On macOS the `-macos` variant is used. `-macos-fastcatch.py` (opt-in with `--fastcatch`) removes the idle gaps in the activation loop; it caught worse in testing, so it is off by default |
| `tests/` | Simulated-flash tests: 512 KB / 1 MB detection, all-0xFF rejection, mismatched reads, partial files |
| `backups/` | Created on the first run; one `stock_YYYYmmdd_HHMMSS/` folder per backup |

## Tonight (no adapter needed)

```bash
cd ~/ATC_TLSR_Paper/stock_backup
./setup.sh
```
It should end with `OK` from the tests and `Setup OK`.

## Wiring

Use a 3.3 V USB-UART with an RTS pin (CP2102 or CH340). **Not FTDI**, and not one
with an LED on its RX line. Set its voltage jumper to **3.3 V**.

```
USB-UART            TLSR8250 board
--------            --------------
GND  -------------- GND
3V3  -------------- VCC (3.3 V)    <- only if the board's battery is OUT
RX   -------------- SWS
TX   --[ 1 kΩ ]---- SWS            (TX and RX both end on SWS)
RTS  -------------- RESET (RST)
```

- Never connect the adapter's 3.3 V while a battery is in. Either take the battery
  out and power from the adapter, or keep the battery and leave 3V3 unconnected
  (GND must still be connected).
- Never connect 5 V to the board.
- Keep the wires short (under ~15 cm).
- If the board has no RESET pad, wire RTS to the board's VCC instead of 3V3, so RTS
  power-cycles it.

## Tomorrow: run the backup

1. Plug in the adapter and find it:
   ```bash
   ./.venv/bin/python backup_stock_firmware.py ports
   ```
   Expect `/dev/cu.usbserial-XXXX` (or `/dev/cu.SLAB_USBtoUART` / `/dev/cu.wchusbserial*`
   with vendor drivers). macOS lists each adapter twice, as `cu.*` and `tty.*`; the script uses `cu.*`.
2. Run it:
   ```bash
   ./backup.sh                                  # auto-picks the port if there's only one
   ./backup.sh --port /dev/cu.usbserial-0001    # or name it
   ```
   Defaults suit an adapter with **no RTS/DTR** (reset by hand): `-t 10000 -c 48`.
   - Before the first read the script prints `>>> ACTION: unplug the 3V3 wire...` and
     waits for Enter. Unplug 3V3 and press Enter. When the window opens it beeps and
     shows `>>>  PLUG 3V3 IN NOW  <<<`: plug 3V3 back in then. A `Chip answered` line
     confirms the catch.
   - That one catch halts the CPU. Every later read runs with `-t 0`, which skips the
     flasher's activation (and its soft reset), so you **leave 3V3 connected** for the
     rest of the backup.
   - If a later read fails anyway (`Chip sleep?`), it asks for another catch and retries
     (up to `--retries`, default 2). The summary shows how many manual resets it took.
3. What happens:
   - **Probe**: reads 16 KB at `0x000000` and at `0x080000`. A 512 KB chip wraps the
     address, so the two match and the chip is 512 KB; otherwise it's treated as 1 MB.
     If the first 16 KB is all 0xFF or all 0x00, it stops here. It also prints the
     measured speed and how long the full reads will take.
   - **Two full reads** of the whole chip, compared by SHA256. If they differ it does a
     third read and keeps the two that agree; if all three differ it stops.
   - **Checks**: rejects an all-0xFF or all-0x00 image, warns if there's no Telink
     `KNLT` signature at 0x0 or 0x20000. If a 1 MB read turns out to be two identical
     halves, it saves it as 512 KB.
4. A good run ends with `BACKUP OK`, the image path, and its SHA256.
5. The read halts the CPU. Power-cycle the board afterwards and check it still works.

Under the hood each read is exactly this (syntax verified from the tool source):
```
python -u TLSR825xComFlasher-macos.py -p PORT -b 921600 -t 10000 -c 48 rf 0x000000 0x4000 raw/probe_0x000000.bin   # first read: activation
python -u TLSR825xComFlasher-macos.py -p PORT -b 921600 -t 0 -c 48 rf 0x000000 0x80000 raw/read1.bin             # later reads: no reset
```

## Result

```
backups/stock_20261112_101500/
  stock_full_512KB.bin   <- the backup (read-only)
  SHA256SUMS             <- hashes of the image and every raw read
  backup_info.json       <- port, baud, flash size, flasher commit, checks
  raw/                   <- probes and every full read
backup.log               <- ONE log for every run (each starts with a "# RUN <time>" header)
```

The flasher's output is printed live while it runs, so you see `Activate (... ms)...`
the moment it starts and can plug in 3V3 (or tap RST) during that window. Older runs
may still have a `flasher.log` in their folder; new runs don't create one.

Then:
- Copy the whole `stock_*` folder off the Mac (USB stick, cloud drive). The dump can
  hold the device's MAC/pairing data, so don't publish it (`backups/` is git-ignored).
- Re-check it any time: `./.venv/bin/python backup_stock_firmware.py verify backups/stock_...`
- Two backups on different days with the same SHA256 is the strongest proof.

## When reads disagree

- The catch is a separate 256-byte throwaway read (`raw/catchN.bin`), because on this board
  the first block read after activation comes back as zeros. The kept reads start after it.
- If read1 and read2 differ, a third read is made. If no two of the three are identical,
  the image is built by a byte-wise 2-of-3 majority vote, and the outvoted blocks are listed.
  If any byte has no majority, it stops and prints the address ranges to re-read.
- To vote reads that are already on disk (offline, no hardware):
  `./.venv/bin/python backup_stock_firmware.py merge backups/stock_...`

## Speed

Expect about 0.5 KB/s, so roughly 17 min per 512 KB read and 35 min for a whole
backup. The flasher reads one byte per USB round trip (it must wait for each reply
on the shared SWS wire), so this is set by USB latency, not by `-c` or the baud.
`-c` only picks the first SWire speed to try; using the right one (48) just skips
a few seconds of failed tries per run. If you know the chip size, `--size 512k`
skips the probes.

## Troubleshooting

| Message | Try |
|---|---|
| `No USB-UART adapter found` | Different cable (some are charge-only), then check System Information → USB |
| `Wrong RX-TX connection` / `Chip sleep?` | Check GND, both TX (via 1 kΩ) and RX on SWS. Plug 3V3 in sooner after `Activate`, or give yourself longer: `./backup.sh -t 15000` |
| `BAD USB-UART Chip` | The adapter can't keep up (FTDI, LED on RX). Use a CP2102 or CH340 |
| `All three reads differ` / short reads | Shorter wires, `./backup.sh -b 460800`, power from the battery instead of the adapter |
| `all 0xFF` | Nothing was really read, or the chip is blank. Check wiring and run again |
| No `KNLT` signature warning | Keep the dump, run a second backup, and compare the SHA256 before trusting it |
