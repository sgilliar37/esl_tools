# esl_tools

Tools and firmware for PickSmart 2.13" electronic shelf labels (Telink TLSR8250/8258 with an
ST7305 reflective LCD, 250×122, black/white).

**Command-line manual:** [docs/CLI_MANUAL.md](docs/CLI_MANUAL.md) covers backing up, finding,
updating, locking, wired flashing and rebuilding, step by step.

| Folder | What it is |
|---|---|
| `tag_designer/` | Tag Designer: a Mac app (browser UI + Python/bleak server) to design screens and send them to many tags over Bluetooth, with the password lock UI. See its README. |
| `Tag Designer.app`, `Tag Designer.command` | Double-click launchers for Tag Designer (expect this folder at `~/ATC_TLSR_Paper` or next to the app). |
| `atc_port/` | The ported firmware (`ATC_Paper_picksmart.bin`), the Bluetooth updater `ota.sh`, the SWire flasher `flash.sh`, `unlock_usb.sh`, the password-lock client `tag_lock.py`, and `picksmart_st7305.patch` (all firmware source changes). See `PORTING_NOTES.md`. |
| `firmware/` | `build.sh` rebuilds the firmware from atc1441/ATC_TLSR_Paper@3778d29 + the patch. |
| `stock_backup/` | Read-only flash backup scripts for the original firmware over a USB-UART adapter (SWire), vendoring pvvx TlsrComSwireWriter. |

Firmware versions in `atc_port/`:
- `ATC_Paper_picksmart.bin` = v3.1: v2.0 screen driver + password lock (current).
- `_v2.0.bin`: what the working tags run (no lock). `_v2.1.bin`, `_v3.0.bin`: v2.1 screen driver, not confirmed on working glass. `_v1.bin`: first port.

Not in the repository: the original PickSmart firmware dump (needed by `ota.sh restore` and
`flash.sh restore`, kept in `stock_backup/backups/`), logs, your saved designs, uploaded fonts and
pictures. The tag password is kept only in the macOS Keychain.

Tests (no hardware): `python3 atc_port/tests/test_lock.py`, `python3 tag_designer/tests/test_designer.py`,
`python3 stock_backup/tests/test_backup.py`; `atc_port/tests/test_ota.py` needs the stock dump.
