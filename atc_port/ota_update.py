#!/usr/bin/env python3
"""
Update the tag's firmware over Bluetooth (Telink OTA). No wires needed: the tag
only has to be powered.

    ./ota.sh              send the ported firmware (ATC_Paper_picksmart.bin)
    ./ota.sh restore      send the original PickSmart firmware back
    ./ota.sh FILE.bin     send another Telink-format image
    ./ota.sh --scan       just list the tags it can see

Safe by design: the tag writes the new firmware into its spare flash bank and
only switches to it after the whole-image checksum matches. If anything goes
wrong (Bluetooth drops, a packet is lost, timeout), the tag throws the partial
copy away and keeps running what it had.
"""
import argparse
import asyncio
import hashlib
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import telink_ota as T  # noqa: E402

FIRMWARE = os.path.join(HERE, "ATC_Paper_picksmart.bin")
STOCK_IMAGE = os.path.join(HERE, "..", "stock_backup", "backups", "stock_20261113_175437",
                           "stock_full_512KB.bin")
STOCK_SHA256 = "37eb9d79d0e01f46af9c5ddabef57b9cc8a4a09f36d6724f6ba7b36f16affade"
NAME_PREFIXES = ("NEMR", "ESL_")
LOG = os.path.join(HERE, "ota.log")
TAG_TIME_LIMIT = 180  # the stock firmware gives up on an update 180 s after it starts
LOST_PACKETS = (1, 5)
TOO_SLOW = (12, 13)


def say(msg=""):
    print(msg, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


class OtaError(Exception):
    def __init__(self, msg, code=None, sent=True):
        super().__init__(msg)
        self.code = code
        self.sent = sent  # False: failed before any firmware data went out


def tag_error(code):
    return OtaError("tag reported: %s (code %d)" % (T.RESULT_TEXT.get(code, "unknown"), code), code)


def load_image(what):
    if what == "restore":
        raw = open(STOCK_IMAGE, "rb").read()
        if hashlib.sha256(raw).hexdigest() != STOCK_SHA256:
            raise OtaError("The stock backup changed or is missing: %s" % STOCK_IMAGE)
        size = struct.unpack_from("<I", raw, 0x18)[0]
        img = raw[:size]  # the stock firmware is the first `size` bytes of the backup
        label = "original PickSmart firmware"
    else:
        path = FIRMWARE if what == "firmware" else what
        try:
            img = T.to_telink_image(open(path, "rb").read())
        except ValueError as e:
            raise OtaError("Image not accepted: %s" % e)
        label = os.path.basename(path)
    problems = T.check_image(img)
    if problems:
        raise OtaError("Image not accepted: " + "; ".join(problems))
    return img, label


# --------------------------------------------------------------------------
# the OTA transfer itself (works on any client with the bleak API)
# --------------------------------------------------------------------------
def _cb_peripheral(client):
    """On macOS: the CoreBluetooth peripheral behind a bleak client. None elsewhere."""
    p = getattr(getattr(client, "_backend", None), "_peripheral", None)
    return p if hasattr(p, "canSendWriteWithoutResponse") else None


async def _room_to_send(peripheral, limit=0.5):
    """CoreBluetooth silently drops a write-without-response sent while its buffer
    is full, and bleak does not wait for room, so wait here. False = still full."""
    t_end = time.monotonic() + limit
    while not peripheral.canSendWriteWithoutResponse():
        if time.monotonic() > t_end:
            return False
        await asyncio.sleep(0.002)
    return True


async def send_image(client, char, img, sync_every, progress=None, results=None):
    """Send img. Returns normally when the END command has gone out.
    `results` is a list that the notification handler appends result codes to."""
    results = results if results is not None else []
    peripheral = _cb_peripheral(client)

    def check_result():
        for code in results:
            if code != 0:
                raise tag_error(code)

    async def call(op):
        # A Bluetooth error here means the link is gone (the tag restarts after
        # rejecting an update). Give the tag's own reason when it sent one.
        try:
            return await op
        except OtaError:
            raise
        except Exception as e:
            check_result()
            raise OtaError("Bluetooth connection dropped (%s: %s)" % (type(e).__name__, e), 14)

    async def barrier():
        # A read is answered only after every earlier write has reached the tag,
        # so the queue stays short and a rejected update is noticed quickly.
        await call(client.read_gatt_char(char))
        check_result()
        if not client.is_connected:
            raise OtaError("Bluetooth connection dropped", 14)

    await call(client.write_gatt_char(char, T.start_packet(), response=False))
    await barrier()  # the start command is processed before any data
    packets = list(T.data_packets(img))
    total = len(packets)
    t0 = time.monotonic()
    for n, (_, pkt) in enumerate(packets, 1):
        if peripheral is not None and not await _room_to_send(peripheral):
            await barrier()
            peripheral = None  # the readiness flag looks stuck: rely on the read checks
        await call(client.write_gatt_char(char, pkt, response=False))
        if n % sync_every == 0 or n == total:
            await barrier()
            elapsed = time.monotonic() - t0
            if elapsed > 5 and sync_every < 64 and elapsed / n * total > 0.8 * TAG_TIME_LIMIT:
                sync_every *= 2  # running slow for the tag's time limit: check less often
            if progress:
                progress(n, total, elapsed)
    await call(client.write_gatt_char(char, T.end_packet(packets[-1][0]), response=False))
    try:
        await client.read_gatt_char(char)
    except Exception:
        pass  # the tag may already be restarting into the new firmware
    check_result()


def show_progress(done, total, elapsed):
    pct = 100.0 * done / total
    eta = elapsed / done * (total - done) if done else 0
    sys.stdout.write("\r  sending: %5.1f%%   %3d s left   " % (pct, eta))
    sys.stdout.flush()
    if done == total:
        sys.stdout.write("\n")


# --------------------------------------------------------------------------
# Bluetooth (bleak) parts
# --------------------------------------------------------------------------
def backup_tag_names():
    """Names of the tag the stock backup came from: the stock firmware calls
    itself NEMR + the ID at 0x7F000, the port ESL_ + the end of the MAC at 0x76000."""
    try:
        raw = open(STOCK_IMAGE, "rb").read()
    except OSError:
        return set()
    names = set()
    if len(raw) >= 0x80000:
        ident = struct.unpack_from("<I", raw, 0x7F000)[0]
        if ident != 0xFFFFFFFF:
            names.add("NEMR%08X" % ident)
        mac = raw[0x76000:0x76006]
        if mac != b"\xff" * 6:
            names.add("ESL_%02X%02X%02X" % (mac[2], mac[1], mac[0]))
    return names


def describe(name, rssi):
    mark = "   <- the tag your backup came from" if name in backup_tag_names() else ""
    return "%s  (signal %d)%s" % (name, rssi, mark)


# What each firmware shows over Bluetooth
ATC_ADV_UUID = "0000181a-0000-1000-8000-00805f9b34fb"   # the port's advert data
PICKSMART_COMPANY = 0x5053                              # stock manufacturer data
SVC_OTA = T.OTA_SERVICE_UUID
SVC_PICKSMART = "0000fef0-0000-1000-8000-00805f9b34fb"
SVC_ATC_RXTX = "00001f10-0000-1000-8000-00805f9b34fb"
SVC_ATC_OLD_OTA = "0000221f-0000-1000-8000-00805f9b34fb"
_adv = {}  # address -> what the last scan heard from it


def advert_summary(address):
    """What a tag's adverts say about it (no connection needed)."""
    e = _adv.get(address)
    if not e:
        return ""
    sd = e["sdata"].get(ATC_ADV_UUID)
    if sd and len(sd) >= 12:
        return "ATC-style advert: MAC %s, battery %d%%" % (":".join("%02X" % b for b in sd[:6]), sd[9])
    if PICKSMART_COMPANY in e["mdata"]:
        return "PickSmart advert"
    return ""


async def identify(dev):
    """Connect briefly and tell which firmware a tag runs from its services."""
    from bleak import BleakClient
    client = BleakClient(dev, timeout=15.0)
    try:
        await client.connect()
        uuids = {str(svc.uuid).lower() for svc in client.services}
    except Exception as e:
        return "could not connect to check (%s)" % type(e).__name__
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    if SVC_PICKSMART in uuids:
        return "stock PickSmart firmware"
    if SVC_ATC_RXTX in uuids and SVC_OTA in uuids:
        return "the new firmware from this project"
    if SVC_ATC_RXTX in uuids and SVC_ATC_OLD_OTA in uuids:
        return "atc1441's original ATC firmware (not this project's)"
    return "unknown firmware, services: " + ", ".join(sorted(uuids))


async def scan(seconds=8.0):
    """Tags in range as (name, device, rssi), strongest first.
    macOS reports a tag's advert and its scan response as separate packets, and
    the port only names itself in the scan response, so names are collected over
    the whole scan. The Mac's own cached name (dev.name) can be out of date
    after an update, so it is only a last resort."""
    from bleak import BleakScanner
    seen = {}

    def on_adv(dev, adv):
        e = seen.setdefault(dev.address, {"name": None, "uuids": set(), "sdata": {}, "mdata": {}})
        e["dev"], e["rssi"] = dev, adv.rssi
        if adv.local_name:
            e["name"] = adv.local_name
        e["uuids"].update(u.lower() for u in (adv.service_uuids or []))
        e["sdata"].update({str(k).lower(): bytes(v) for k, v in (getattr(adv, "service_data", None) or {}).items()})
        e["mdata"].update({k: bytes(v) for k, v in (getattr(adv, "manufacturer_data", None) or {}).items()})

    scanner = BleakScanner(detection_callback=on_adv)
    await scanner.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await scanner.stop()
    _adv.update(seen)
    tags = []
    for e in seen.values():
        name = e["name"]
        sd = e["sdata"].get(ATC_ADV_UUID)
        if not name and sd and len(sd) == 13:
            # the port, heard without its name packet: its name is the end of the
            # MAC at the start of its advert data
            name = "ESL_%02X%02X%02X" % (sd[3], sd[4], sd[5])
        name = name or e["dev"].name or ""
        if (name.startswith(NAME_PREFIXES) or T.OTA_SERVICE_UUID in e["uuids"]
                or PICKSMART_COMPANY in e["mdata"]):
            tags.append((name or "(no name)", e["dev"], e["rssi"]))
    tags.sort(key=lambda t: -t[2])
    return tags


def say_not_showing_up(label):
    say("%s is not showing up. Check it has power and is near the Mac." % label)
    say("If it was wired up for SWire flashing, restart it once (touch RST to GND, or")
    say("unplug its power for 5 s): a missed catch can leave the chip paused.")


async def pick_tag(want_names=None, want_address=None, tries=3, rescans=4):
    """Find exactly the wanted tag (by any of want_names, or by the Mac's address
    for it), looking for about half a minute (it may still be restarting) before
    asking. Other tags are never picked."""
    label = " or ".join(sorted(want_names)) if want_names else "the tag"
    for attempt in range(tries):
        for _ in range(rescans):
            say("Looking for %s over Bluetooth (about 8 s)..." % label)
            everything = await scan()
            tags = [t for t in everything if (want_names and t[0] in want_names) or
                    (want_address is not None and t[1].address == want_address)]
            if tags:
                return tags[0]
        say_not_showing_up(label)
        if everything:
            say("Tags in range:")
            for name, dev, rssi in everything:
                say("  " + describe(name, rssi))
            say("To update one of those instead, name it: ./ota.sh --name NAME")
        if attempt < tries - 1:
            input("Press Enter to look again (Ctrl-C to stop)...")
    raise OtaError("%s not found over Bluetooth. Nothing was sent." % label, sent=False)


async def unlock_if_locked(client, name):
    """Tags with firmware v3 can be locked with Tag Designer's password: a locked
    tag ignores updates until the connection proves it knows the password."""
    if not name.startswith("ESL_"):
        return
    import tag_lock
    try:
        how, _, _ = await tag_lock.unlock(client, name)
    except tag_lock.LockError as e:
        raise OtaError("%s is locked with a password: %s Nothing was sent." % (name, e), sent=False)
    if how == "open":
        say("Unlocked %s with Tag Designer's password." % name)


async def connect_and_send(dev, img, sync_every, name=""):
    from bleak import BleakClient
    results = []

    def on_notify(_, data):
        # Stock firmware reports the result as 06 FF <code> 00
        if len(data) >= 3 and data[0] == 0x06 and data[1] == 0xFF:
            results.append(data[2])

    client = BleakClient(dev, timeout=20.0)
    try:
        await client.connect()
    except Exception as e:
        raise OtaError("could not connect (%s %s). Nothing was sent." % (type(e).__name__, e), sent=False)
    try:
        char = client.services.get_characteristic(T.OTA_CHAR_UUID)
        if char is None:
            raise OtaError("This tag has no Telink update service.", sent=False)
        await unlock_if_locked(client, name)
        if "notify" in char.properties:
            try:
                await client.start_notify(char, on_notify)
            except Exception:
                pass  # result messages are a bonus; the tag checks the image either way
        say("Connected. Sending %d bytes..." % len(img))
        await send_image(client, char, img, sync_every, show_progress, results)
        # give the tag a moment to check the image and report back
        for _ in range(30):
            if results or not client.is_connected:
                break
            await asyncio.sleep(0.2)
        if results and results[-1] != 0:
            raise tag_error(results[-1])
    finally:
        try:
            await client.disconnect()  # usually already gone: the tag restarts
        except Exception:
            pass


async def wait_for_new_name(address, want_prefix, scans=12):
    """Wait until the same tag (same Bluetooth address: both firmwares use the MAC
    stored in the tag's flash) advertises a name starting with want_prefix.
    Other tags nearby never count."""
    say("Waiting for the tag to restart (up to a minute)...")
    for _ in range(scans):
        for name, dev, rssi in await scan(5.0):
            if dev.address == address and name.startswith(want_prefix):
                return name
    return None


async def main_async(args):
    img, label = load_image(args.what)
    say("\n# OTA %s  %s (%d bytes, sha256 %s)" % (time.strftime("%Y-%m-%d %H:%M:%S"), label, len(img),
                                                 hashlib.sha256(img).hexdigest()[:16]))
    # Only ever the tag the backup came from, unless another one is named on purpose.
    targets = {args.name} if args.name else backup_tag_names()
    if not targets:
        raise OtaError("No tag to update: name one with --name (./ota.sh --scan lists them).", sent=False)
    name, dev, rssi = await pick_tag(targets)
    say("Found " + describe(name, rssi))
    want_prefix = "NEMR" if args.what == "restore" else "ESL_"
    if not args.yes:
        if input("Send %s to %s? Type YES to go ahead: " % (label, name)).strip().upper() != "YES":
            say("Stopped. Nothing was sent.")
            return 1
    sync = args.sync
    for attempt in range(1, 4):
        code = None
        sent = True
        try:
            await connect_and_send(dev, img, sync, name)
            break
        except OtaError as e:
            say("\nAttempt %d failed: %s" % (attempt, e))
            code, sent = e.code, e.sent
        except Exception as e:  # Bluetooth errors from bleak/CoreBluetooth
            say("\nAttempt %d failed: %s: %s" % (attempt, type(e).__name__, e))
        if attempt == 3:
            say("\nGave up after 3 tries. The tag keeps its current firmware, nothing is damaged.")
            return 1
        if code in LOST_PACKETS:
            sync = max(2, sync // 2)   # packets went missing: check more often
        elif code in TOO_SLOW:
            sync = min(64, sync * 2)   # ran out of time: check less often
        if sent:
            say("The tag throws the partial copy away and restarts. Trying again in 5 s...")
        else:
            say("Trying again in 5 s...")
        await asyncio.sleep(5)
        name, dev, _ = await pick_tag({name}, dev.address)
    new_name = await wait_for_new_name(dev.address, want_prefix)
    if new_name:
        say("\nDONE: the tag restarted as %s, so the %s is running." % (new_name, "original PickSmart firmware" if args.what == "restore" else "firmware you sent"))
        return 0
    say("\nThe transfer finished, but %s has not come back as %s... yet." % (name, want_prefix))
    tags = await scan()
    if tags:
        say("Tags in range now:")
        for n, d, r in tags:
            say("  " + describe(n, r))
    say("Give it a minute and run ./ota.sh --scan to see what it is advertising.")
    return 1


async def scan_only():
    say("Looking for tags over Bluetooth...")
    for _ in range(3):  # the first scan after starting sometimes hears nothing
        tags = await scan(10.0)
        if tags:
            break
    else:
        say("No tags found.")
    for name, dev, rssi in tags:
        say("  " + describe(name, rssi))
        summary = advert_summary(dev.address)
        if summary:
            say("      " + summary)
        say("      checking its firmware: " + await identify(dev))
    mine = backup_tag_names()
    if mine and not any(name in mine for name, _, _ in tags):
        say("")
        say_not_showing_up("The tag your backup came from (%s)" % " or ".join(sorted(mine)))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", nargs="?", default="firmware",
                    help="'firmware' (default), 'restore', or a path to a .bin")
    ap.add_argument("--scan", action="store_true",
                    help="list tags in range and check which firmware each one runs")
    ap.add_argument("--name", help="update this tag instead of the one your backup came from")
    ap.add_argument("--sync", type=int, default=16, help="packets between delivery checks")
    ap.add_argument("--yes", action="store_true", help="do not ask before sending")
    args = ap.parse_args()
    try:
        import bleak  # noqa: F401
    except ImportError:
        raise SystemExit("The 'bleak' Bluetooth library is missing. Run ./ota.sh, which installs it.")
    try:
        rc = asyncio.run(scan_only() if args.scan else main_async(args))
    except OtaError as e:
        say("\n" + str(e))
        rc = 1
    except KeyboardInterrupt:
        say("\nStopped. If a transfer was running, the tag discards it and keeps its firmware.")
        rc = 1
    except Exception as e:
        if type(e).__name__ == "BleakBluetoothNotAvailableError":
            say("\nmacOS is not letting Terminal use Bluetooth (%s)." % e)
            say("Turn Bluetooth on, and allow Terminal in System Settings > Privacy & Security >")
            say("Bluetooth. Then run ./ota.sh again.")
            rc = 1
        else:
            raise
    sys.exit(rc)


if __name__ == "__main__":
    main()
