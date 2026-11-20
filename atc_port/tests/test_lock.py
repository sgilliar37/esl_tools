"""Password lock: the Mac side (tag_lock.py) against a simulated tag.
Run with LOCK_HOST=<host build of tag_lock.c> to test the firmware's own code."""
import asyncio
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import tag_lock as L  # noqa: E402
from lock_tag_sim import FakeClient, make_tag  # noqa: E402

NAME = "ESL_8079EB"


def run(c):
    return asyncio.run(c)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["TAG_LOCK_FILE"] = os.path.join(self.tmp, "keys.json")
        L.PBKDF2_ROUNDS = 1000  # fast tests
        self.tag = make_tag()

    def tearDown(self):
        p = getattr(self.tag, "p", None)
        if p:
            p.stdin.close()
            p.wait()

    def connect(self):
        return FakeClient(self.tag)

    def test_aes_fips197(self):
        k = bytes(range(16))
        p = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(L.aes128(k, p).hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")

    def test_unlocked_tag_takes_anything(self):
        c = self.connect()
        run(c.write_gatt_char(L.EPD_CHAR, b"\x00\xff"))
        self.assertEqual(c.accepted, [b"\x00\xff"])
        self.assertEqual(run(L.read_status(c)), (3, L.NONE))

    def test_lock_then_strangers_are_refused(self):
        L.set_app_password("correct horse")
        c = self.connect()
        self.assertTrue(run(L.lock(c, NAME)))
        self.assertEqual(run(L.read_status(c))[1], L.OPEN)
        # a new connection without the password: everything is ignored
        c2 = self.connect()
        self.assertEqual(run(L.read_status(c2))[1], L.LOCKED)
        for w in (b"\x00\xff", b"\x03" + bytes(20), b"\x01", b"\x05\x00", b"\x06\x00\x00", b"\x13",
                  b"\x12" + bytes(32), b"\x11" + bytes(16)):
            run(c2.write_gatt_char(L.EPD_CHAR, w))
        self.assertEqual(c2.accepted, [])
        self.assertEqual(run(L.read_status(c2))[1], L.LOCKED)
        # the app unlocks and sends
        c3 = self.connect()
        self.assertEqual(run(L.unlock(c3, NAME))[0], "open")
        run(c3.write_gatt_char(L.EPD_CHAR, b"\x01"))
        self.assertEqual(c3.accepted, [b"\x01"])

    def test_wrong_password_and_try_limit(self):
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        L.save_masters(L.master_from_password("wrong password"))
        c = self.connect()
        with self.assertRaises(L.WrongPassword):
            run(L.unlock(c, NAME))
        for _ in range(3):
            try:
                run(L._answer(c, bytes(16)))
            except L.LockError:
                pass
        with self.assertRaises(L.LockError):  # out of tries in this connection
            run(L._challenge(c))
        run(c.write_gatt_char(L.EPD_CHAR, b"\x01"))
        self.assertEqual(c.accepted, [])

    def test_replayed_answer_fails(self):
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        c = self.connect()
        _, chal, salt = run(L._challenge(c))
        key = L.tag_key(L.load_masters()["current"], salt)
        good = b"\x11" + L.aes128(key, chal)
        run(c.write_gatt_char(L.EPD_CHAR, good))
        self.assertEqual(run(L.read_status(c))[1], L.OPEN)
        c2 = self.connect()  # an eavesdropper replays the answer in its own connection
        run(L._challenge(c2))
        run(c2.write_gatt_char(L.EPD_CHAR, good))
        self.assertEqual(run(L.read_status(c2))[1], L.LOCKED)

    def test_answer_without_challenge_fails(self):
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        c = self.connect()
        run(c.write_gatt_char(L.EPD_CHAR, b"\x11" + bytes(16)))
        self.assertEqual(run(L.read_status(c))[1], L.LOCKED)

    def test_change_password_moves_tags(self):
        L.set_app_password("first password")
        run(L.lock(self.connect(), NAME))
        L.set_app_password("second password")
        c = self.connect()
        self.assertEqual(run(L.unlock(c, NAME))[0], "open")  # unlocked with the old one, moved
        L.save_masters(L.master_from_password("second password"))  # forget the old one
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")

    def test_relock_with_new_password_same_connection(self):
        L.set_app_password("first password")
        run(L.lock(self.connect(), NAME))
        L.save_masters(L.master_from_password("first password"))
        c = self.connect()
        run(L.unlock(c, NAME))
        old = L.load_masters()["current"]
        L.save_masters(L.master_from_password("other password"), [old])
        run(L.lock(self.connect(), NAME))
        L.save_masters(L.master_from_password("other password"))
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")

    def test_remove(self):
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        self.assertTrue(run(L.remove(self.connect(), NAME)))
        self.assertEqual(run(L.read_status(self.connect()))[1], L.NONE)

    def test_key_file_private(self):
        L.set_app_password("correct horse")
        self.assertEqual(os.stat(os.environ["TAG_LOCK_FILE"]).st_mode & 0o777, 0o600)
        self.assertNotIn("correct horse", open(os.environ["TAG_LOCK_FILE"]).read())
        self.assertTrue(L.check_app_password("correct horse"))
        self.assertFalse(L.check_app_password("correct horsE"))

    def test_short_password_refused(self):
        with self.assertRaises(L.LockError):
            L.set_app_password("short pw")

    def test_spoofed_unlocked_tag_learns_nothing(self):
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        # a fake tag that claims "no password" gets locked by the app...
        fake = make_tag()
        run(L.lock(FakeClient(fake), NAME))
        stolen = fake.flash[0] if hasattr(fake, "flash") and isinstance(fake.flash, tuple) else None
        if stolen is None:
            self.skipTest("C tag hides its key")
        # ...but that key does not open the real tag
        c = self.connect()
        with self.assertRaises(L.WrongPassword):
            run(L._answer(c, stolen, is_key=True))
        self.assertEqual(run(L.read_status(c))[1], L.LOCKED)

    def test_guessing_slows_down_across_connections(self):
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        for _ in range(2):  # 6 wrong answers over two connections
            c = self.connect()
            for _ in range(3):
                with self.assertRaises(L.WrongPassword):
                    run(L._answer(c, bytes(16), is_key=True))
        with self.assertRaises(L.LockError):  # a new connection: still no tries for 30 s
            run(L._challenge(self.connect()))
        self.tag.set_time(1040)
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")

    def test_cooldown_does_not_lock_out_an_old_password_tag(self):
        L.set_app_password("password number one")
        run(L.lock(self.connect(), NAME))
        L.set_app_password("password number two")  # the tag still has number one
        for _ in range(2):  # a stranger runs it into the cooldown
            c = self.connect()
            for _ in range(3):
                with self.assertRaises(L.WrongPassword):
                    run(L._answer(c, bytes(16), is_key=True))
        self.tag.set_time(1031)
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")  # one try, the right password
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")  # moved to number two

    def test_old_and_current_password_slots(self):
        L.set_app_password("password number one")
        run(L.lock(self.connect(), NAME))
        L.set_app_password("password number two")
        L.set_app_password("password number three")  # the tag still has number one
        self.assertEqual(len(L.load_masters()["previous"]), 2)
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")
        L.save_masters(L.master_from_password("password number three"))
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")

    @unittest.skipUnless(os.environ.get("LOCK_HOST"), "needs the host build of tag_lock.c")
    def test_failed_flash_write_keeps_old_key(self):
        L.set_app_password("password number one")
        run(L.lock(self.connect(), NAME))
        L.set_app_password("password number two")
        self.tag.fail_writes(1)
        with self.assertRaises(L.LockError):
            run(L.unlock(self.connect(), NAME))  # the move to number two fails...
        L.save_masters(L.master_from_password("password number one"))
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")  # ...number one still works

    @unittest.skipUnless(os.environ.get("LOCK_HOST"), "needs the host build of tag_lock.c")
    def test_first_lock_write_failure_leaves_no_password(self):
        L.set_app_password("correct horse")
        self.tag.fail_writes(1)
        with self.assertRaises(L.LockError):
            run(L.lock(self.connect(), NAME))
        self.assertEqual(run(L.read_status(self.connect()))[1], L.NONE)

    @unittest.skipUnless(os.environ.get("LOCK_HOST"), "needs the host build of tag_lock.c")
    def test_other_firmware_data_is_not_a_password(self):
        self.tag._call("foreign")  # e.g. left in the sector by the original PickSmart firmware
        self.tag.cmd(b"\x00")
        self.assertEqual(run(L.read_status(self.connect()))[1], L.NONE)
        L.set_app_password("correct horse")
        self.assertTrue(run(L.lock(self.connect(), NAME)))

    @unittest.skipUnless(os.environ.get("LOCK_HOST"), "needs the host build of tag_lock.c")
    def test_damaged_record_stays_locked(self):
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        self.tag.tear()
        c = self.connect()
        self.assertEqual(run(L.read_status(c))[1], L.LOCKED)
        with self.assertRaises(L.WrongPassword):
            run(L.unlock(c, NAME))
        self.tag.erase_key_sector()
        self.assertEqual(run(L.read_status(self.connect()))[1], L.NONE)

    @unittest.skipUnless(os.environ.get("LOCK_HOST"), "needs the host build of tag_lock.c")
    def test_password_change_power_cut_keeps_a_key(self):
        # new record written, old one not yet erased: the newer one wins after a reboot
        L.set_app_password("password number one")
        run(L.lock(self.connect(), NAME))
        L.set_app_password("password number two")
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")
        self.tag._call("init")
        L.save_masters(L.master_from_password("password number two"))
        self.assertEqual(run(L.unlock(self.connect(), NAME))[0], "open")

    def test_updater_unlocks_or_refuses(self):
        import ota_update as U
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        c = self.connect()
        run(U.unlock_if_locked(c, NAME))  # the Mac knows the password
        self.assertEqual(run(L.read_status(c))[1], L.OPEN)
        L.save_masters(L.master_from_password("not the password"))
        with self.assertRaises(U.OtaError) as e:
            run(U.unlock_if_locked(self.connect(), NAME))
        self.assertFalse(e.exception.sent)

    @unittest.skipUnless(os.environ.get("LOCK_HOST"), "needs the host build of tag_lock.c")
    def test_usb_recovery_erases_the_key(self):
        L.set_app_password("correct horse")
        run(L.lock(self.connect(), NAME))
        self.tag.erase_key_sector()
        self.assertEqual(run(L.read_status(self.connect()))[1], L.NONE)

    @unittest.skipUnless(os.environ.get("LOCK_HOST"), "needs the host build of tag_lock.c")
    def test_firmware_aes_matches(self):
        out = self.tag._call("aes 000102030405060708090a0b0c0d0e0f 00112233445566778899aabbccddeeff")
        self.assertEqual(out[0], "69c4e0d86a7b0430d8cdb78070b4c55a")


if __name__ == "__main__":
    unittest.main()
