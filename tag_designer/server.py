#!/usr/bin/env python3
"""
Tag Designer: lay out text, pictures and boxes for the 2.13" tags on the Mac,
then send the result to one tag or several over Bluetooth.

Started by "Tag Designer.command" (double-click it). It runs a small web server
on this Mac only (127.0.0.1) and opens the designer in your browser; the
Bluetooth work happens here, in Python, with bleak.

How a picture reaches the tag (ATC_TLSR_Paper picture service):
  service 13187b10-eba9-a3ba-044e-83d3217d9a38, characteristic 4b646063-...
  00 FF        clear the tag's picture buffer to white
  02 HI LO     set the write position
  03 data...   copy data into the buffer at the position
  01           show the buffer on the screen
  05 00/01     (firmware v2 from this project) back to the clock / keep the picture
The buffer is 4000 bytes: pixel (x, y), x 0..249, y 0..121, is bit 0x80 >> (y % 8)
of byte (249 - x) * 16 + y // 8, set = white.
"""
import asyncio
import base64
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
ATC_PORT = os.path.join(HERE, "..", "atc_port")
FONTS = os.path.join(HERE, "fonts")
DESIGNS = os.path.join(HERE, "designs")
LOG = os.path.join(HERE, "tag_designer.log")
SCREENS = os.path.join(HERE, "screens.json")  # last known screen of each tag
def _code_version():
    import hashlib
    h = hashlib.sha1()
    for f in ("server.py", "index.html"):
        try:
            with open(os.path.join(HERE, f), "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:12]


CODE_VERSION = _code_version()  # a running copy with another version is out of date and gets replaced
TAG_SCREENS = os.path.join(HERE, "tag_screens.json")  # {tag name: screen type id} chosen in the app
MAX_PICTURE = 5000  # the ATC firmware's picture buffer (200x200 screens use all of it)
TAG_FIELDS = os.path.join(HERE, "tag_fields.json")  # your own fields ({room} ...) and each tag's values

EPD_SERVICE = "13187b10-eba9-a3ba-044e-83d3217d9a38"
EPD_CHAR = "4b646063-6264-f3a7-8941-e65356ea82fe"
RXTX_CHAR = "00001f1f-0000-1000-8000-00805f9b34fb"
SCREEN_CHAR = "4b646064-6264-f3a7-8941-e65356ea82fe"  # firmware v2: screen read-back window
SCREEN_WINDOW = 180
BUFFER_SIZE = 4000          # 250 columns x 16 bytes
FIRMWARE_KEEPS_PICTURE = 2  # firmware v2 and later answer >= 2 when the picture characteristic is read
LOCK_NAMES = {0: "none", 1: "locked", 2: "open"}  # firmware v3: password lock state
GALLERY = os.path.join(HERE, "gallery")
TAGFONTS = os.path.join(HERE, "tagfonts")  # the firmware's own bitmap fonts (tools/extract_tag_fonts.py)
GALLERY_CATS = ("backgrounds", "frames", "pictures", "twotone", "mine")  # mine = pictures you add
IMAGE_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
FONT_TYPES = {".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2"}


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Bluetooth: one asyncio loop in a background thread does all of it
# --------------------------------------------------------------------------
class Bluetooth:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.lock = None
        self.devices = {}   # id -> bleak device from the last scan
        self.tags = {}      # id -> what the page shows about it
        self.jobs = {}
        self.backup_names = set()
        self.heard_others = 0
        try:
            self.screens = json.load(open(SCREENS))
        except (OSError, ValueError):
            self.screens = {}
        self.run(self._init())

    async def _init(self):
        self.lock = asyncio.Lock()

    def run(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- scanning -------------------------------------------------------
    async def scan(self):
        import ota_update  # from ../atc_port: the scan that copes with macOS's quirks
        if not self.backup_names:
            self.backup_names = ota_update.backup_tag_names()
        async with self.lock:
            found = []
            ota_update._adv.clear()  # every device heard, tag or not
            for _ in range(3):  # the first scan after starting sometimes hears nothing
                found = await ota_update.scan(8.0)
                if found:
                    break
        self.heard_others = len(ota_update._adv)
        tags = []
        for name, dev, rssi in found:
            if name.startswith("ESL_"):
                kind = "port"
            elif name.startswith("NEMR"):
                kind = "stock"
            else:
                kind = "unknown"
            tid = str(dev.address)
            self.devices[tid] = dev
            info = self.tags.get(tid, {})
            sd = (ota_update._adv.get(dev.address) or {}).get("sdata", {}).get(ota_update.ATC_ADV_UUID)
            if sd and len(sd) >= 12:  # the port's advert: MAC, temperature, battery %, battery mV
                info.update(mac=":".join("%02X" % b for b in sd[:6]), battery=sd[9],
                            battery_mv=(sd[10] << 8) | sd[11])
            info.update(id=tid, name=name, rssi=rssi, kind=kind,
                        backed_up=name in self.backup_names, screen=self.screens.get(tid))
            self.tags[tid] = info
            tags.append(info)
        log("scan: %s  (Bluetooth devices heard in total: %d)" % (
            ", ".join("%s (%d)" % (t["name"], t["rssi"]) for t in tags) or "no tags", self.heard_others))
        return tags

    # ---- sending --------------------------------------------------------
    def new_job(self, action, ids, data=None):
        job = {"id": uuid.uuid4().hex[:10], "action": action, "done": False, "tags": []}
        for tid in ids:
            info = self.tags.get(tid, {"name": tid})
            job["tags"].append({"id": tid, "name": info.get("name", tid), "state": "waiting",
                                "progress": 0, "message": "Waiting"})
        self.jobs[job["id"]] = job
        self.start(self._run_job(job, data))
        return job

    async def _run_job(self, job, data):
        async with self.lock:
            for entry in job["tags"]:
                tag_data = data.get(entry["id"]) if isinstance(data, dict) else data
                for attempt in (1, 2):
                    try:
                        await self._one_tag(job["action"], entry, tag_data)
                        break
                    except Exception as e:
                        reason = str(e) or type(e).__name__
                        log("%s %s attempt %d failed: %s" % (job["action"], entry["name"], attempt, reason))
                        if os.environ.get("TAG_DESIGNER_DEBUG"):
                            traceback.print_exc()
                        if attempt == 2 or isinstance(e, TagRefused):
                            entry.update(state="failed", message=reason)
                            break
                        entry.update(message="Retrying (%s)" % reason, progress=0)
                        await asyncio.sleep(2)
            job["done"] = True

    def remember_screen(self, tid, buf, source):
        self.screens[tid] = {"buffer": base64.b64encode(buf).decode(), "source": source,
                             "time": time.strftime("%Y-%m-%d %H:%M")}
        self.tags.setdefault(tid, {"id": tid})["screen"] = self.screens[tid]
        try:
            with open(SCREENS, "w") as f:
                json.dump(self.screens, f)
        except OSError:
            pass

    async def _read_screen(self, client, char, entry, start=0, span=100):
        """The tag's own screen buffer (firmware v2), or None if it can't report it."""
        screen = client.services.get_characteristic(SCREEN_CHAR)
        if screen is None:
            return None
        out = bytearray()
        for pos in range(0, BUFFER_SIZE, SCREEN_WINDOW):
            await client.write_gatt_char(char, bytes([0x06, pos >> 8, pos & 0xFF]), response=True)
            out += bytes(await client.read_gatt_char(screen))[:SCREEN_WINDOW]
            entry["progress"] = start + int(span * min(len(out), BUFFER_SIZE) / BUFFER_SIZE)
        if len(out) < BUFFER_SIZE:
            raise RuntimeError("the tag sent back only %d bytes of its screen" % len(out))
        return bytes(out[:BUFFER_SIZE])

    async def _one_tag(self, action, entry, data):
        from bleak import BleakClient
        dev = self.devices.get(entry["id"])
        if dev is None:
            raise TagRefused("Not found in the last scan. Press Scan and try again.")
        entry.update(state="working", message="Connecting", progress=0)
        client = BleakClient(dev, timeout=20.0)
        await client.connect()
        try:
            char = client.services.get_characteristic(EPD_CHAR)
            if char is None:
                raise TagRefused("This tag's firmware has no picture service. "
                                 "It needs this project's firmware (./ota.sh).")
            import tag_lock
            version, lock_state = await tag_lock.read_status(client)
            keeps = version >= FIRMWARE_KEEPS_PICTURE
            info = self.tags.setdefault(entry["id"], {})
            info["keeps_picture"] = keeps
            info["lock"] = LOCK_NAMES.get(lock_state, "locked") if version >= 3 else "unsupported"
            if action in ("lock", "unlock"):
                await self._lock_action(client, action, entry, info, version)
                return
            if lock_state == tag_lock.LOCKED:
                entry["message"] = "Unlocking with your password"
                try:
                    await tag_lock.unlock(client, entry["name"])
                except tag_lock.LockError as e:
                    raise TagRefused(str(e))
            if action == "read":
                entry["message"] = "Reading its screen"
                buf = await self._read_screen(client, char, entry)
                if buf is None:
                    entry.update(state="done", progress=100,
                                 message="This tag's firmware can't report its screen (update it with ./ota.sh).")
                else:
                    self.remember_screen(entry["id"], buf, "tag")
                    entry.update(state="done", progress=100, message="")
            elif action == "send":
                await self._send_picture(client, char, entry, data, 70 if keeps else 100)
                back = None
                if keeps and len(data) == BUFFER_SIZE:  # read-back covers the 250x122 layout
                    entry["message"] = "Checking"
                    back = await self._read_screen(client, char, entry, 70, 30)
                if back is not None:
                    if back != data:
                        self.remember_screen(entry["id"], back, "tag")
                        raise TagRefused("Sent, but the tag's screen does not match. Try sending again.")
                    self.remember_screen(entry["id"], back, "tag")
                    entry.update(state="done", progress=100, message="Done (checked with the tag)")
                elif keeps:
                    if len(data) == BUFFER_SIZE:
                        self.remember_screen(entry["id"], data, "sent")
                    else:  # other screen sizes: don't keep showing an old picture as "on its screen"
                        self.screens.pop(entry["id"], None)
                        self.tags.setdefault(entry["id"], {"id": entry["id"]})["screen"] = None
                    entry.update(state="done", progress=100, message="Done")
                else:
                    if len(data) == BUFFER_SIZE:
                        self.remember_screen(entry["id"], data, "sent")
                    entry.update(state="done", progress=100,
                                 message="Sent, but its older firmware's clock will cover it within "
                                         "a minute (update it with ./ota.sh)")
            elif action == "clock":
                rxtx = client.services.get_characteristic(RXTX_CHAR)
                if rxtx is not None:  # set the tag's clock to the Mac's local time
                    now = int(time.time()) + (time.localtime().tm_gmtoff or 0)
                    await client.write_gatt_char(rxtx, bytes([0xDD]) + now.to_bytes(4, "big"), response=True)
                if not keeps:
                    raise TagRefused("This tag runs the older firmware, which always shows the clock "
                                     "within a minute. Update it with ./ota.sh to use this.")
                await client.write_gatt_char(char, b"\x05\x00", response=True)
                await asyncio.sleep(2)  # the tag redraws its clock on its next loop
                try:
                    buf = await self._read_screen(client, char, entry)
                    if buf is not None:
                        self.remember_screen(entry["id"], buf, "tag")
                except Exception as e:
                    log("reading the clock screen back failed: %s" % e)
                entry.update(state="done", progress=100, message="Showing the clock")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        log("%s -> %s: %s" % (action, entry["name"], entry["message"]))

    async def _lock_action(self, client, action, entry, info, version):
        import tag_lock
        if version < 3:
            raise TagRefused("This tag's firmware has no password lock. Update it with ./ota.sh first.")
        try:
            if action == "lock":
                entry["message"] = "Locking"
                await tag_lock.lock(client, entry["name"])
                info["lock"] = "locked"
                entry.update(state="done", progress=100, message="Locked with your password")
            else:
                entry["message"] = "Removing the password"
                removed = await tag_lock.remove(client, entry["name"])
                info["lock"] = "none"
                entry.update(state="done", progress=100,
                             message="Password removed: anyone can change this tag now" if removed
                             else "This tag had no password")
        except tag_lock.LockError as e:
            raise TagRefused(str(e))

    async def _send_picture(self, client, char, entry, data, span=100):
        mtu = getattr(client, "mtu_size", 23) or 23
        chunk = max(16, min(240, mtu - 4))  # 3 bytes ATT header + 1 command byte
        entry["message"] = "Sending"
        await client.write_gatt_char(char, b"\x00\xff", response=True)  # clear to white
        await client.write_gatt_char(char, b"\x02\x00\x00", response=True)  # position 0
        total = len(data)
        for pos in range(0, total, chunk):
            await client.write_gatt_char(char, b"\x03" + data[pos:pos + chunk], response=True)
            entry["progress"] = int(span * 0.95 * min(total, pos + chunk) / total)
        await client.write_gatt_char(char, b"\x01", response=True)  # show it
        entry["progress"] = span


class TagRefused(Exception):
    """A problem that retrying will not fix."""


# --------------------------------------------------------------------------
# Web server (only reachable from this Mac)
# --------------------------------------------------------------------------
def gallery_listing():
    """{category: [{"file", "name"}]}: the order and names come from each folder's
    manifest*.json files; pictures without an entry are listed after them."""
    out = {}
    for cat in GALLERY_CATS:
        folder = os.path.join(GALLERY, cat)
        try:
            files = sorted(f for f in os.listdir(folder) if safe_name(f, IMAGE_TYPES))
        except OSError:
            files = []
        items, seen = [], set()
        for m in sorted(f for f in os.listdir(folder) if f.startswith("manifest") and f.endswith(".json")) if files else []:
            try:
                with open(os.path.join(folder, m)) as mf:
                    entries = json.load(mf)
                for e in entries:
                    if e.get("file") in files and e["file"] not in seen:
                        seen.add(e["file"])
                        items.append({"file": e["file"], "name": str(e.get("name") or e["file"])})
            except (OSError, ValueError, AttributeError, TypeError):
                pass
        for f in files:
            if f not in seen:
                items.append({"file": f, "name": os.path.splitext(f)[0].replace("_", " ").capitalize()})
        out[cat] = items
    return out


def safe_name(name, exts=None):
    name = os.path.basename(unquote(name)).strip()
    if not name or name.startswith(".") or not re.fullmatch(r"[\w .()'&+,-]{1,80}", name):
        return None
    if exts and os.path.splitext(name)[1].lower() not in exts:
        return None
    return name


class Handler(BaseHTTPRequestHandler):
    ble = None
    server_version = "TagDesigner/1"

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=8 << 20):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n)

    def local_only(self):
        # refuse pages from other web sites talking to this server
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost"):
            return False
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return False
        return True

    def do_GET(self):
        if not self.local_only():
            return self.reply(403, {"error": "forbidden"})
        url = urlparse(self.path)
        p = url.path
        try:
            if p in ("/", "/index.html"):
                return self.reply(200, open(os.path.join(HERE, "index.html"), "rb").read(),
                                  "text/html; charset=utf-8")
            if p == "/api/tags":
                if parse_qs(url.query).get("scan"):
                    tags = self.ble.run(self.ble.scan(), timeout=90)
                else:
                    tags = list(self.ble.tags.values())
                return self.reply(200, {"tags": tags, "heard": self.ble.heard_others})
            if p.startswith("/api/jobs/"):
                job = self.ble.jobs.get(p.rsplit("/", 1)[1])
                return self.reply(200, job) if job else self.reply(404, {"error": "no such job"})
            if p == "/api/gallery":
                return self.reply(200, {"gallery": gallery_listing()})
            if p.startswith("/gallery/"):
                parts = p.split("/")
                name = safe_name(parts[-1], IMAGE_TYPES) if len(parts) == 4 else None
                path = name and parts[2] in GALLERY_CATS and os.path.join(GALLERY, parts[2], name)
                if not path or not os.path.isfile(path):
                    return self.reply(404, {"error": "no such picture"})
                with open(path, "rb") as f:
                    data = f.read()
                return self.reply(200, data, IMAGE_TYPES[os.path.splitext(name)[1].lower()])
            if p == "/api/tagfields":
                try:
                    return self.reply(200, open(TAG_FIELDS, "rb").read())
                except OSError:
                    return self.reply(200, {"fields": [], "values": {}})
            if p == "/api/fonts":
                files = sorted(f for f in os.listdir(FONTS) if safe_name(f, FONT_TYPES))
                return self.reply(200, {"fonts": files})
            if p.startswith("/fonts/"):
                name = safe_name(p[len("/fonts/"):], FONT_TYPES)
                path = name and os.path.join(FONTS, name)
                if not path or not os.path.isfile(path):
                    return self.reply(404, {"error": "no such font"})
                return self.reply(200, open(path, "rb").read(), FONT_TYPES[os.path.splitext(name)[1].lower()])
            if p == "/api/version":
                # stale: the files were updated after this copy started (it keeps running old code)
                return self.reply(200, {"version": CODE_VERSION, "stale": _code_version() != CODE_VERSION})
            if p == "/api/password":
                import tag_lock
                return self.reply(200, {"set": tag_lock.load_masters()["current"] is not None})
            if p == "/api/tagscreens":
                try:
                    with open(TAG_SCREENS) as f:
                        got = json.load(f)
                    return self.reply(200, got if isinstance(got, dict) else {})
                except (OSError, ValueError):
                    return self.reply(200, {})
            if p == "/api/tagfonts":
                try:
                    with open(os.path.join(TAGFONTS, "index.json")) as f:
                        return self.reply(200, {"fonts": json.load(f)})
                except (OSError, ValueError):
                    return self.reply(200, {"fonts": []})
            if p.startswith("/tagfonts/"):
                name = safe_name(p[len("/tagfonts/"):], {".json": 1})
                path = name and name != "index.json" and os.path.join(TAGFONTS, name)
                if not path or not os.path.isfile(path):
                    return self.reply(404, {"error": "no such tag font"})
                with open(path, "rb") as f:
                    return self.reply(200, f.read())
            if p == "/api/designs":
                files = [f[:-5] for f in os.listdir(DESIGNS) if f.endswith(".json") and safe_name(f)]
                files.sort(key=lambda n: -os.path.getmtime(os.path.join(DESIGNS, n + ".json")))
                return self.reply(200, {"designs": files})
            if p.startswith("/api/designs/"):
                name = safe_name(p[len("/api/designs/"):])
                path = name and os.path.join(DESIGNS, name + ".json")
                if not path or not os.path.isfile(path):
                    return self.reply(404, {"error": "no such design"})
                return self.reply(200, open(path, "rb").read())
            return self.reply(404, {"error": "not found"})
        except Exception as e:
            log("GET %s failed: %s" % (p, e))
            return self.reply(500, {"error": str(e) or type(e).__name__})

    def do_POST(self):
        if not self.local_only():
            return self.reply(403, {"error": "forbidden"})
        p = urlparse(self.path).path
        if p == "/api/quit":  # a newer copy is starting: hand Bluetooth and the port over to it
            self.reply(200, {"quitting": True})
            log("Tag Designer is closing so a newer version can start")
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        try:
            if p == "/api/password":
                import tag_lock
                req = json.loads(self.body(1 << 16) or b"{}")
                new, cur = req.get("password"), req.get("current")
                if not isinstance(new, str):
                    return self.reply(400, {"error": "Type a password."})
                try:
                    if tag_lock.load_masters()["current"] is not None:  # changing it needs the current one
                        if not isinstance(cur, str) or not tag_lock.check_app_password(cur):
                            return self.reply(400, {"error": "The current password is not right."})
                    tag_lock.set_app_password(new)
                except tag_lock.LockError as e:
                    return self.reply(400, {"error": str(e)})
                log("app password set")
                return self.reply(200, {"set": True})
            if p in ("/api/send", "/api/clock", "/api/read", "/api/lock", "/api/unlock"):
                req = json.loads(self.body() or b"{}")
                ids = [i for i in req.get("tags", []) if isinstance(i, str)]
                if not ids:
                    return self.reply(400, {"error": "Pick at least one tag."})
                data = None
                if p == "/api/send":
                    # one picture for all tags, or one per tag ("buffers": {tag id: picture})
                    per_tag = req.get("buffers")
                    if isinstance(per_tag, dict):
                        data = {i: base64.b64decode(per_tag.get(i, "")) for i in ids}
                        pictures = list(data.values())
                    else:
                        data = base64.b64decode(req.get("buffer", ""))
                        pictures = [data]
                    if any(not 0 < len(d) <= MAX_PICTURE for d in pictures):
                        return self.reply(400, {"error": "picture must be 1 to %d bytes" % MAX_PICTURE})
                job = self.ble.new_job(p.rsplit("/", 1)[1], ids, data)
                return self.reply(200, job)
            if p == "/api/tagscreens":
                req = json.loads(self.body(1 << 20) or b"{}")
                if not isinstance(req, dict):
                    return self.reply(400, {"error": "expected {tag name: screen type}"})
                clean = {k[:40]: v[:40] for k, v in req.items()
                         if isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[\w-]{1,40}", v)}
                with open(TAG_SCREENS, "w") as f:
                    json.dump(clean, f, indent=1)
                return self.reply(200, clean)
            if p == "/api/tagfields":
                req = json.loads(self.body(1 << 20) or b"{}")
                fields = [f for f in req.get("fields", []) if isinstance(f, str) and re.fullmatch(r"[A-Za-z]\w{0,23}", f)]
                values = {}
                for tag, vals in (req.get("values") or {}).items():
                    if isinstance(tag, str) and isinstance(vals, dict):
                        values[tag[:40]] = {k: str(v)[:200] for k, v in vals.items() if k in fields}
                with open(TAG_FIELDS, "w") as f:
                    json.dump({"fields": fields, "values": values}, f, indent=1)
                return self.reply(200, {"fields": fields, "values": values})
            if p.startswith("/api/gallery/mine/"):
                name = safe_name(p[len("/api/gallery/mine/"):], IMAGE_TYPES)
                if not name:
                    return self.reply(400, {"error": "Use a .png, .jpg, .gif, .webp or .svg file with a simple name."})
                os.makedirs(os.path.join(GALLERY, "mine"), exist_ok=True)
                with open(os.path.join(GALLERY, "mine", name), "wb") as f:
                    f.write(self.body(20 << 20))
                return self.reply(200, {"file": name})
            if p.startswith("/api/fonts/"):
                name = safe_name(p[len("/api/fonts/"):], FONT_TYPES)
                if not name:
                    return self.reply(400, {"error": "Use a .ttf, .otf, .woff or .woff2 file with a simple name."})
                with open(os.path.join(FONTS, name), "wb") as f:
                    f.write(self.body(20 << 20))
                return self.reply(200, {"font": name})
            if p.startswith("/api/designs/"):
                name = safe_name(p[len("/api/designs/"):])
                if not name:
                    return self.reply(400, {"error": "Use letters, numbers, spaces and - _ . in the name."})
                raw = self.body(16 << 20)
                json.loads(raw)  # must be valid JSON
                with open(os.path.join(DESIGNS, name + ".json"), "wb") as f:
                    f.write(raw)
                return self.reply(200, {"design": name})
            return self.reply(404, {"error": "not found"})
        except Exception as e:
            log("POST %s failed: %s" % (p, e))
            return self.reply(500, {"error": str(e) or type(e).__name__})

    def do_DELETE(self):
        if not self.local_only():
            return self.reply(403, {"error": "forbidden"})
        p = urlparse(self.path).path
        if p.startswith("/api/gallery/mine/"):  # only your own pictures can be removed
            name = safe_name(p[len("/api/gallery/mine/"):], IMAGE_TYPES)
            path = name and os.path.join(GALLERY, "mine", name)
            if path and os.path.isfile(path):
                os.remove(path)
                return self.reply(200, {"deleted": name})
        if p.startswith("/api/designs/"):
            name = safe_name(p[len("/api/designs/"):])
            path = name and os.path.join(DESIGNS, name + ".json")
            if path and os.path.isfile(path):
                os.remove(path)
                return self.reply(200, {"deleted": name})
        return self.reply(404, {"error": "not found"})


def make_server(preferred=8250):
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            continue
    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def running_instance(port=8250):
    """URL of a Tag Designer that is already running, or None."""
    import urllib.request
    url = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(url + "api/designs", timeout=1) as r:
            if "designs" in json.loads(r.read()):
                return url
    except Exception:
        pass
    return None


def running_version(url):
    """The code version of a running copy ('' for copies from before versions existed)."""
    import urllib.request
    try:
        with urllib.request.urlopen(url + "api/version", timeout=1) as r:
            return json.loads(r.read()).get("version", "")
    except Exception:
        return ""


def replace_old_instance(url):
    """Ask an out-of-date running copy to close. True once its port is free."""
    import urllib.request
    try:
        req = urllib.request.Request(url + "api/quit", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass
    for _ in range(30):
        time.sleep(0.2)
        if not running_instance():
            return True
    return False


def main(argv):
    os.makedirs(FONTS, exist_ok=True)
    os.makedirs(DESIGNS, exist_ok=True)
    os.makedirs(os.path.join(GALLERY, "mine"), exist_ok=True)
    sys.path.insert(0, ATC_PORT)
    if "--demo" in argv:  # simulated tags, no Bluetooth needed
        sys.path.insert(0, os.path.join(HERE, "tests"))
        import fake_tags
        fake_tags.install()
        log("demo mode: simulated tags, nothing is sent over Bluetooth")
    if "--demo" not in argv:
        url = running_instance()
        if url and running_version(url) != CODE_VERSION:
            log("An older Tag Designer is running; replacing it with this version")
            if replace_old_instance(url):
                url = None
            else:
                log("The older Tag Designer did not close. Quit Terminal (or close the window "
                    "that runs it), then open Tag Designer again.")
                if "--window" in argv:
                    show_message("An older Tag Designer is still running",
                                 "Quit Terminal (or close the Terminal window that runs Tag Designer), "
                                 "then open Tag Designer again to get the new version.")
        if url:  # only one copy may own Bluetooth: show the one that is running
            log("Tag Designer is already running at %s" % url)
            if not ("--window" in argv and show_window(url)):
                webbrowser.open(url)
            return 0
    Handler.ble = Bluetooth()
    httpd = make_server()
    port = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    if "--window" in argv and run_in_window(httpd, url):
        return 0
    log("Tag Designer is running at %s  (close this window or press Ctrl-C to stop)" % url)
    if "--no-browser" not in argv:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def run_in_window(httpd, url):
    """The Mac app: the designer in its own window (pywebview / WebKit) instead of a
    browser. Falls back to the browser if the window cannot open. Returns when done."""
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if not show_window(url):
        webbrowser.open(url)  # no window: keep serving for the browser until stopped
        log("Tag Designer is running at %s  (close this window or press Ctrl-C to stop)" % url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    httpd.shutdown()
    log("Tag Designer app closed")
    return True


def show_message(title, text):
    """A plain Mac alert (best effort)."""
    import subprocess
    try:
        subprocess.run(["osascript", "-e", 'display alert "%s" message "%s"' % (title.replace('"', "'"), text.replace('"', "'"))],
                       timeout=120, check=False)
    except Exception:
        pass


def show_window(url):
    """Open the designer in its own window and wait until it is closed. False if that failed."""
    try:
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True  # Export PNG asks where to save
        _mac_app_identity()
        log("Tag Designer app window open (%s)" % url)
        webview.create_window("Tag Designer", url, width=1440, height=920, min_size=(900, 600))
        storage = os.path.join(HERE, ".app_storage")  # keeps the page's settings between runs
        webview.start(private_mode=False, storage_path=storage)
        return True
    except Exception as e:
        log("could not open the app window (%s); using the browser instead" % e)
        return False


def _mac_app_identity():
    """Show 'Tag Designer' and its icon in the Dock and menu bar instead of 'Python'."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "Tag Designer"
        from AppKit import NSApplication, NSImage
        app = os.environ.get("TAG_DESIGNER_APP") or os.path.join(HERE, "..", "Tag Designer.app")
        icon = os.path.join(app, "Contents", "Resources", "AppIcon.icns")
        if os.path.exists(icon):
            NSApplication.sharedApplication().setApplicationIconImage_(
                NSImage.alloc().initWithContentsOfFile_(icon))
    except Exception as e:
        log("app name/icon not set: %s" % e)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
