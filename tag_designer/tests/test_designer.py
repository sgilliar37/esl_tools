#!/usr/bin/env python3
"""Offline tests: the designer's Bluetooth side against simulated tags.
Run: ../stock_backup/.venv/bin/python tests/test_designer.py  (or python3)"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "atc_port")]
import fake_tags  # noqa: E402


def make_buffer():
    """A known picture: left half black, a white 1-pixel frame, bottom row black."""
    import random
    random.seed(1)
    return bytes(random.getrandbits(8) for _ in range(4000))


class DesignerTests(unittest.TestCase):
    def setUp(self):
        self.tags = fake_tags.install()
        import tempfile
        import server as srv
        srv.SCREENS = os.path.join(tempfile.mkdtemp(), "screens.json")
        srv.LOG = os.devnull
        import server
        self.server = server
        self.ble = server.Bluetooth()
        found = self.ble.run(self.ble.scan(), timeout=30)
        self.by_name = {t["name"]: t for t in found}

    def wait(self, job):
        t0 = time.time()
        while not job["done"]:
            self.assertLess(time.time() - t0, 30)
            time.sleep(0.05)
        return job

    def sim(self, name):
        return next(t for t in self.tags if t.name == name)

    def lock_env(self):
        import tempfile
        import tag_lock
        os.environ["TAG_LOCK_FILE"] = os.path.join(tempfile.mkdtemp(), "keys.json")
        tag_lock.PBKDF2_ROUNDS = 1000
        return tag_lock

    def test_spoofed_tag_does_not_get_the_real_key(self):
        L = self.lock_env()
        L.set_app_password("tag password 1")
        tid = self.by_name["ESL_8079EB"]["id"]
        self.wait(self.ble.new_job("lock", [tid]))
        real = self.sim("ESL_8079EB")
        real_key = real.lock.key
        real.lock.key, real.lock.salt = None, bytes(16)  # pretend: a fake device answering as this tag, unlocked
        self.wait(self.ble.new_job("lock", [tid]))
        self.assertNotEqual(real.lock.key, real_key)

    def test_lock_send_and_refuse_strangers(self):
        L = self.lock_env()
        L.set_app_password("tag password 1")
        tid = self.by_name["ESL_8079EB"]["id"]
        job = self.wait(self.ble.new_job("lock", [tid]))
        self.assertEqual(job["tags"][0]["state"], "done", job)
        self.assertEqual(self.ble.tags[tid]["lock"], "locked")
        t = self.sim("ESL_8079EB")
        self.assertIsNotNone(t.lock.key)
        # the app still sends: it unlocks by itself
        data = make_buffer()
        job = self.wait(self.ble.new_job("send", [tid], data))
        self.assertEqual(job["tags"][0]["state"], "done", job)
        self.assertEqual(t.screen, data)
        # a stranger (no password) gets nothing through
        L.save_masters(L.master_from_password("someone else"))
        job = self.wait(self.ble.new_job("send", [tid], bytes(4000)))
        self.assertEqual(job["tags"][0]["state"], "failed")
        self.assertEqual(t.screen, data)
        self.assertEqual(t.custom_image, True)
        job = self.wait(self.ble.new_job("clock", [tid]))
        self.assertEqual(job["tags"][0]["state"], "failed")
        self.assertIsNone(t.time_set)

    def test_remove_password(self):
        L = self.lock_env()
        L.set_app_password("tag password 1")
        tid = self.by_name["ESL_8B062B"]["id"]
        self.wait(self.ble.new_job("lock", [tid]))
        job = self.wait(self.ble.new_job("unlock", [tid]))
        self.assertEqual(job["tags"][0]["state"], "done", job)
        self.assertIsNone(self.sim("ESL_8B062B").lock.key)
        self.assertEqual(self.ble.tags[tid]["lock"], "none")

    def test_lock_old_firmware_refused(self):
        L = self.lock_env()
        L.set_app_password("tag password 1")
        job = self.wait(self.ble.new_job("lock", [self.by_name["ESL_7A1C03"]["id"]]))
        self.assertEqual(job["tags"][0]["state"], "failed")
        self.assertIn("no password lock", job["tags"][0]["message"])

    def test_scan_kinds(self):
        self.assertEqual(self.by_name["ESL_8079EB"]["kind"], "port")
        self.assertEqual(self.by_name["ESL_8079EB"]["battery"], 87)
        self.assertEqual(self.by_name["ESL_8079EB"]["battery_mv"], 2950)
        self.assertEqual(self.by_name["ESL_8079EB"]["mac"], "A0:40:FA:80:79:EB")
        self.assertEqual(self.by_name["NEMR62616005"]["kind"], "stock")

    def test_send_to_several(self):
        data = make_buffer()
        ids = [self.by_name[n]["id"] for n in ("ESL_8079EB", "ESL_8B062B")]
        job = self.wait(self.ble.new_job("send", ids, data))
        for entry in job["tags"]:
            self.assertEqual(entry["state"], "done", entry)
        for n in ("ESL_8079EB", "ESL_8B062B"):
            t = self.sim(n)
            self.assertEqual(t.screen, data)
            self.assertTrue(t.custom_image)

    def test_one_picture_per_tag(self):
        a, b = make_buffer(), bytes(4000)
        ids = {n: self.by_name[n]["id"] for n in ("ESL_8079EB", "ESL_8B062B")}
        job = self.wait(self.ble.new_job("send", list(ids.values()),
                                         {ids["ESL_8079EB"]: a, ids["ESL_8B062B"]: b}))
        self.assertTrue(all(e["state"] == "done" for e in job["tags"]), job)
        self.assertEqual(self.sim("ESL_8079EB").screen, a)
        self.assertEqual(self.sim("ESL_8B062B").screen, b)

    def test_older_firmware_warns(self):
        job = self.wait(self.ble.new_job("send", [self.by_name["ESL_7A1C03"]["id"]], make_buffer()))
        self.assertEqual(job["tags"][0]["state"], "done")
        self.assertIn("older firmware", job["tags"][0]["message"])

    def test_stock_refused_without_writes(self):
        job = self.wait(self.ble.new_job("send", [self.by_name["NEMR62616005"]["id"]], make_buffer()))
        self.assertEqual(job["tags"][0]["state"], "failed")
        self.assertEqual(self.sim("NEMR62616005").writes, 0)

    def test_retry_after_connect_timeout(self):
        self.sim("ESL_8B062B").fail_connects = 1
        job = self.wait(self.ble.new_job("send", [self.by_name["ESL_8B062B"]["id"]], make_buffer()))
        self.assertEqual(job["tags"][0]["state"], "done")

    def test_clock(self):
        t = self.sim("ESL_8079EB")
        self.wait(self.ble.new_job("send", [self.by_name["ESL_8079EB"]["id"]], make_buffer()))
        job = self.wait(self.ble.new_job("clock", [self.by_name["ESL_8079EB"]["id"]]))
        self.assertEqual(job["tags"][0]["state"], "done")
        self.assertEqual(t.shows, "clock")
        self.assertFalse(t.custom_image)
        self.assertLess(abs(t.time_set - (time.time() + time.localtime().tm_gmtoff)), 5)

    def test_read_screen(self):
        tid = self.by_name["ESL_8079EB"]["id"]
        t = self.sim("ESL_8079EB")
        job = self.wait(self.ble.new_job("read", [tid]))
        self.assertEqual(job["tags"][0]["state"], "done", job)
        import base64
        self.assertEqual(base64.b64decode(self.ble.screens[tid]["buffer"]), t.screen)
        self.assertEqual(self.ble.screens[tid]["source"], "tag")

    def test_send_is_checked_and_remembered(self):
        import base64
        data = make_buffer()
        tid = self.by_name["ESL_8B062B"]["id"]
        job = self.wait(self.ble.new_job("send", [tid], data))
        self.assertIn("checked", job["tags"][0]["message"])
        self.assertEqual(base64.b64decode(self.ble.screens[tid]["buffer"]), data)

    def test_read_older_firmware_refused(self):
        job = self.wait(self.ble.new_job("read", [self.by_name["ESL_7A1C03"]["id"]]))
        self.assertIn("can't report", job["tags"][0]["message"])
        self.assertNotIn(self.by_name["ESL_7A1C03"]["id"], self.ble.screens)

    def test_small_mtu(self):
        # a tag link with the smallest MTU: chunks must still fit
        import bleak
        orig = bleak.BleakClient.__init__

        def small(self_, dev, timeout=10.0):
            orig(self_, dev, timeout)
            self_.mtu_size = 23
        bleak.BleakClient.__init__ = small
        try:
            data = make_buffer()
            job = self.wait(self.ble.new_job("send", [self.by_name["ESL_8079EB"]["id"]], data))
            self.assertEqual(job["tags"][0]["state"], "done")
            self.assertEqual(self.sim("ESL_8079EB").screen, data)
        finally:
            bleak.BleakClient.__init__ = orig


class GalleryTest(unittest.TestCase):
    """The gallery folder listing and adding/removing your own pictures (no Bluetooth)."""

    def setUp(self):
        import tempfile
        import threading
        import server
        self.server = server
        self.tmp = tempfile.mkdtemp()
        self.old = server.GALLERY
        server.GALLERY = self.tmp
        for c in server.GALLERY_CATS:
            os.makedirs(os.path.join(self.tmp, c))
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
        for f in ("b_one.svg", "a_two.svg"):
            open(os.path.join(self.tmp, "pictures", f), "wb").write(svg)
        open(os.path.join(self.tmp, "pictures", "manifest_x.json"), "w").write('[{"file": "b_one.svg", "name": "One"}]')
        open(os.path.join(self.tmp, "pictures", "notes.txt"), "w").write("not a picture")
        self.httpd = server.make_server(18250)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server.GALLERY = self.old

    def req(self, path, method="GET", body=None):
        import urllib.request
        import urllib.error
        r = urllib.request.Request(self.base + path, data=body, method=method)
        try:
            with urllib.request.urlopen(r) as f:
                return f.status, f.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_listing_order_and_names(self):
        import json
        st, body = self.req("/api/gallery")
        self.assertEqual(st, 200)
        pics = json.loads(body)["gallery"]["pictures"]
        self.assertEqual([p["file"] for p in pics], ["b_one.svg", "a_two.svg"])  # manifest first
        self.assertEqual(pics[0]["name"], "One")
        self.assertEqual(pics[1]["name"], "A two")

    def test_serve_and_mine(self):
        import json
        st, body = self.req("/gallery/pictures/b_one.svg")
        self.assertEqual(st, 200)
        self.assertIn(b"<svg", body)
        self.assertNotEqual(self.req("/gallery/pictures/notes.txt")[0], 200)
        self.assertNotEqual(self.req("/gallery/pictures/..%2F..%2Fserver.py")[0], 200)
        self.assertEqual(self.req("/api/gallery/mine/cat.png", "POST", b"\x89PNG fake")[0], 200)
        mine = json.loads(self.req("/api/gallery")[1])["gallery"]["mine"]
        self.assertEqual([m["file"] for m in mine], ["cat.png"])
        self.assertEqual(self.req("/api/gallery/mine/evil.html", "POST", b"x")[0], 400)
        self.assertEqual(self.req("/api/gallery/mine/cat.png", "DELETE")[0], 200)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mine", "cat.png")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
