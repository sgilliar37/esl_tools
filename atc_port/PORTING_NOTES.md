# ATC_TLSR_Paper port for the PickSmart "2.13inch BW Tag" (TLSR8250)

## The big finding: the screen is an LCD, not e-ink
The stock firmware's screen init sends `D6 17 02, D1 01, C0 0E 05, … 11, 36 00, 3A 11, 2A 19 23, 2B 00 7C, 39, 29`.
That is the init for a **Sitronix ST7305-family reflective (memory-in-pixel style) LCD**, not an e-ink controller.
The stored picture at 0x44000 decodes in the ST7305 RAM format and gives back the original screen exactly
(33 bytes per row address × 125 rows = 250 × 132 px, of which 122 are on the glass).

What that means in practice:
- The picture is kept only while the LCD has power. Unplugging the battery blanks it (unlike e-ink).
- Updates are instant (no e-ink flashing), and the LCD idles at very low power.
- The ATC e-ink drivers (UC8151/SSD16xx) would not have worked; this port adds a new ST7305 driver.

## Pin map (from disassembling the stock firmware)
| Signal | Pin | Evidence |
|---|---|---|
| SPI MOSI | PB7 | `spi_master_gpio_set(B6B7D2D7)` |
| SPI CLK | PD7 | same |
| CS | PD2 | CS argument of every `spi_write` call |
| D/C | PB6 | set 0 before command byte, 1 before data |
| RESET | PB5 | toggled 1 → 0 (50 ms) → 1 before init |
| LCD power | PA0 | driven high at init, held high by a 10K pull-up while asleep |
| TE (tearing) | PC0 | input after `35 00` (TE on); unused here |

## What the port changes (`picksmart_st7305.patch`, against atc1441/ATC_TLSR_Paper 3778d29)
- `main.h`: `BOARD_PICKSMART_ST7305` switch and the pin map above.
- New `lcd_st7305.c/.h`: stock init sequence, conversion from the ATC 2.13" BW buffer, bit-banged SPI. Inits once per power-up; the LCD is never powered off.
- `epd.c`: model fixed to "ST7305"; no busy wait, no power-down; temperature line hidden (the LCD has no sensor).
- Disabled on this board: LEDs (ATC's LED pins are PA7 = SWS flashing pin and PD2 = LCD CS), UART (PA0 is LCD power), I2C/NFC (PC0 is TE). The battery sense uses PB5 (reset is held high anyway) instead of PB7 (MOSI).
- `app_att.c`, `ble.c`, `app.c`: the update service is the standard Telink one (same UUIDs as the stock firmware) and runs the SDK's `otaWrite` instead of ATC's own scheme, with a 180 s limit. ATC's `custom_otaWrite` is no longer reachable (it always wrote to 0x20000, which would hit the running copy after a bank switch).
- Flash use: two 128 KB firmware banks (0x00000 and 0x20000; the tag runs from either), settings 0x78100. The MAC at 0x76000, the stock picture at 0x44000 and the data at 0x7F000 are untouched.

## Updating over Bluetooth (no wires): `./ota.sh`
The stock PickSmart firmware has the standard Telink update service built in and switched on from power-up, with no password. `./ota.sh` uses it:
1. Finds the tag your backup came from (NEMR62616747 on stock, ESL_8079EB once ported) and asks you to type YES. Any other tag is only updated when you name it with `--name`. `./ota.sh --scan` lists the tags in range and connects to each to say which firmware it runs.
2. Sends `ATC_Paper_picksmart.bin` in 16-byte packets. The tag writes them into its empty second bank (0x20000), checks every packet's CRC16 and the whole-image CRC32, and only then flips the boot flag and restarts.
3. Waits for the tag to come back as `ESL_…`.

Tag names: the stock firmware calls itself `NEMR` + the 32-bit ID stored at 0x7F000 (hex), the port `ESL_` + the last three bytes of the MAC at 0x76000. Both use that MAC as their public Bluetooth address, so the Mac sees the same device before and after an update, and the updater waits for that address to come back with the new name. Your backup came from NEMR62616747 (ESL_8079EB once ported). The first real update, on 2026-11-14, went to another tag: NEMR62616020. Right after it, NEMR62616020 stopped showing up and ESL_8B062B appeared at the same signal strength; `--scan` confirms which firmware ESL_8B062B runs. `./ota.sh --scan` marks the backed-up tag, and says so when it is missing (a tag left paused by a missed SWire catch needs one restart: RST to GND, or power off for 5 s). If the Mac misses the port's name packet, the name is rebuilt from the MAC at the start of its advert data (13 bytes of 0x181A service data).

Anything going wrong (lost packet, dropped link, bad checksum, more than 180 s) makes the tag erase the half-written copy and restart on the firmware it had. The updater then retries up to 3 times.

After the switch the port runs from 0x20000 and clears the old bank at start-up, so the stock copy is gone from flash, but the stock backup can be sent back the same way: `./ota.sh restore` (the port's SDK update code accepts the stock image; it writes bank 0 and switches back).

What the stock update code accepts (from its disassembly, otaWrite at 0x107a0): start `01 FF`; packets `index + 16 bytes + CRC16/MODBUS`, strictly in order; image length 16 KB to 124 KB (0x1F000), `length % 16 == 4`, `KNLT` at offset 8, size at 0x18 = full length, Telink CRC32 in the last 4 bytes; end `02 FF + last + ~last`. Result is notified as `06 FF <code> 00`. `ATC_Paper_picksmart.bin` is already in this format (85,476 bytes; bytes 6–7 = `5D 02` as the SDK library wants).

On the Mac: bleak (installed into `../stock_backup/.venv` on first run). CoreBluetooth silently drops write-without-response packets when its buffer is full and bleak does not wait for room, so the updater checks `canSendWriteWithoutResponse` before each packet and does a read every 16 packets as a delivery check.

## Checked offline
- Builds cleanly with the repo's tc32 toolchain; packaged for the update service (85,476 bytes, KNLT header, Telink CRC32 appended). The same file is used by `./flash.sh`.
- `tests/test_ota.py` (24 tests, `./ota.sh test`) runs the updater against simulated tags whose accept/reject rules follow the disassembled update code of the stock firmware and of the SDK library: stock → port → stock → port round trip, lost packets, flipped bits, CoreBluetooth buffer drops, a stuck readiness flag, retries and giving up, plus a replay of the first real run (connect timeout, other tags nearby, name only in the scan response, stale cached name), a scan response that never arrives, and an empty first scan. Every failure leaves flash byte-identical to before.
- The buffer-to-LCD conversion, run on the stock picture, reproduces the stock RAM bytes exactly (0 of 4,125 differ).
- `flash_atc.py` tested end-to-end against a simulated chip: writes, reads back, verifies, leaves 0x76000 alone.

## Not yet checked (needs the real board)
- Colours could come out inverted. Fix: build with `-DLCD_INVERT=1`.
- No LEDs or buttons are mapped; PC0 might be a button rather than TE.

## After flashing
- BLE name `ESL_XXXXXX` (last 3 MAC bytes). Send pictures with https://atc1441.github.io/ATC_TLSR_Paper_Image_Upload.html (pick the 2.13" BW / 250×122 mode).
- Later updates over the air: `./ota.sh FILE.bin` with a newly built `ATC_Paper.bin` (it is converted to the update format automatically). atc1441's web OTA page no longer works with this build, because it used ATC's own update scheme.
- Going back: `./ota.sh restore` over Bluetooth, or `./flash.sh restore` over the wires.

## Version 2 (2026-11-17): pictures sent over Bluetooth stay on screen
For the Tag Designer app (`../tag_designer`). `ATC_Paper_picksmart.bin` is now this version (85,684 bytes, sha256 f0e29f9f…154d0a); the first version is kept as `ATC_Paper_picksmart_v1.bin`. `picksmart_st7305.patch` includes the changes.
- `epd_ble_service.c`: a picture sent with the picture service (`00` clear, `01` show, `04` TIFF) sets `epd_custom_image`, and `app.c` then stops redrawing the clock every minute over it. New command `05 00` goes back to the clock (redrawn straight away), `05 01` keeps the picture.
- `epd_buffer` and `byte_pos` moved into retained RAM, so an upload cannot be lost if the chip deep-sleeps between packets (the SDK may do that during a slow connection). Retained RAM use is 22.5 KB of 32 KB.
- Reading the picture characteristic answers `02`, so the app can tell version 2 from version 1.
- Screen read-back: a new read-only characteristic `4b646064-6264-f3a7-8941-e65356ea82fe` in the picture service holds a 180-byte window; `06 HI LO` written to the picture characteristic fills it from `epd_buffer` at that offset. The clock is drawn through `epd_buffer` too, so it always holds what is on the screen. The app reads 23 windows to show a tag's screen and to check every picture it sends. (This also fixes the attribute count: the original table declared one more EPD attribute than it had; a compile-time check now catches that.)
- Same pins, same update service, same flash layout. Update a tag that already runs version 1 with `./ota.sh --name ESL_XXXXXX`.
Not changed: after a power loss the tag starts on the clock screen (the LCD forgets its picture without power).

## Version 2.1 (2026-11-17): screen driver aligned with the stock firmware
On the real tags, version 2 ran and its screen buffer held the clock (read back over Bluetooth), but the glass stayed blank. A line-by-line comparison with the stock firmware's disassembly (bus setup 0x8BA0, init 0x9264, frame 0x8FA4 to 0x9000) found the pins, levels, SPI mode, reset timing and init bytes all matched. Version 2.1 copies the remaining differences:
- Reset and full init before every frame, as stock does (it clears its init flag after each frame). Before, the port initialised once per boot.
- Each frame is bracketed `28` (display off), `2A 19 23`, `2B 00 7C`, `2C`, data, then `39`, `29`, as stock does.
- Stock CS framing: the command byte in its own CS frame with D/C low, then the parameters in a second CS frame with D/C high.
- The battery is measured on PB0, as stock does, instead of on PB5 (the LCD reset pin).
A host build of `lcd_st7305.c` with logged pins decodes to exactly the stock byte sequence. Not yet confirmed on the glass. Quick hardware test: send a design with a black background from Tag Designer. If the glass stays blank even then, the controller is not taking the bus, and a logic analyser on PD7, PB7, PD2, PB6, PB5 and PA0 is the next step.

## Version 3 (2026-11-18): password lock

New `src/tag_lock.c/.h`. A tag with a key refuses every change until the
connection answers a challenge: writes to the picture characteristic (except
10/11), the 1F10 command characteristic (settings, clock) and the Telink OTA
characteristic are dropped while locked.

- `10` -> screen window = `10 state challenge[16] salt[16]`; `11 AES128(key, challenge)` unlocks
  for this connection; `12 key[16] salt[16]` sets a key (masked with
  AES(key, challenge ^ 5A) when changing one); `13` removes it. Reading the picture
  characteristic returns `03 state` (0 none, 1 locked, 2 unlocked).
- One try per challenge, 3 wrong answers per connection, after 6 in a row one try per 30 s.
- Keys live in flash 0x7A000 / 0x7B000 (both blank in the stock dump), as records
  with a sequence number: a new key is written to the other sector and checked
  before the old one is erased, so a power cut keeps a valid key. A damaged
  record keeps the tag locked; `./unlock_usb.sh` erases both sectors over SWire.
- Software AES-128 (checked against FIPS-197 and Python). Host tests run the same
  C file: `LOCK_HOST=<host build> python3 -m unittest tests/test_lock.py`.
- Mac side: `tag_lock.py` (key from PBKDF2-SHA256 of the app password, 600k rounds,
  then HMAC with a fresh random salt per lock; master key in the macOS Keychain).
  `ota_update.py` unlocks before sending firmware.
- Not protected: the Bluetooth link is not encrypted, so an attacker with special
  gear could take over a connection right after the app unlocks it; the first
  lock sends the key in the clear (do it with no strangers around); a tag with no
  password can be locked by anyone first.

## Version 3.1 (2026-11-19): password lock on the v2.0 screen driver
Found on the real tags: ESL_A7E16A and ESL_8B062B (both working, screens fine) run v2.0
(4df38ac6…, 85,668 bytes); only ESL_8079EB ever got v2.1 (and v3.0, built on v2.1), and its
glass is blank even on the stock firmware. v2.1's driver changes were never confirmed on
working glass, so v3.1 goes back to the v2.0 driver: `ATC_Paper_picksmart.bin` = v2.0 source
(lcd_st7305.c, battery on PB5) + the password lock (87,684 bytes, sha256 79dd1ef2…ba1e8c).
Also: data that is not one of our lock records (e.g. left by other firmware) in 0x7A000/0x7B000
now counts as "no password" instead of locking the tag.
Kept: `ATC_Paper_picksmart_v2.0.bin` (rebuilt byte-identical), `_v2.1.bin`, `_v3.0.bin` (v2.1 driver + lock).
