"""Run with:  python3 -m unittest discover -s tests -v   (from the stock_backup folder)"""
import contextlib, io, json, os, random, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import backup_stock_firmware as bsf  # noqa: E402

FAKE = os.path.join(HERE, 'fake_flasher.py')


def slurp(path):
    with open(path, 'rb') as f:
        return f.read()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def telink_image(size, seed=1, fill_upper_ff=False):
    rnd = random.Random(seed)
    img = bytearray(rnd.getrandbits(8) for _ in range(size))
    img[8:12] = b'KNLT'
    if fill_upper_ff:
        img[size // 2:] = b'\xff' * (size // 2)
    return bytes(img)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, 'backups')
        self.port = os.path.join(self.tmp, 'cu.usbserial-TEST')
        with open(self.port, 'w'):
            pass
        for k in ('FAKE_MODE', 'FAKE_HALT', 'FAKE_SLEEP_AFTER'):
            os.environ.pop(k, None)
        os.environ['FAKE_STATE'] = os.path.join(self.tmp, 'state')

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
        shutil.rmtree(self.tmp)

    def run_backup(self, image, mode='ok', extra=()):
        img = os.path.join(self.tmp, 'flash.bin')
        with open(img, 'wb') as f:
            f.write(image)
        os.environ['FAKE_IMAGE'] = img
        os.environ['FAKE_MODE'] = mode
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bsf.main(['backup', '--port', self.port, '--flasher', FAKE,
                           '--out-dir', self.out, '--no-wait'] + list(extra))
        return rc, buf.getvalue()

    def only_run_dir(self):
        dirs = os.listdir(self.out)
        self.assertEqual(len(dirs), 1)
        return os.path.join(self.out, dirs[0])

    def test_512k_detected_by_mirror(self):
        img = telink_image(512 * 1024)
        rc, out = self.run_backup(img)
        self.assertEqual(rc, 0, out)
        d = self.only_run_dir()
        data = slurp(os.path.join(d, 'stock_full_512KB.bin'))
        self.assertEqual(data, img)
        meta = load_json((os.path.join(d, 'backup_info.json')))
        self.assertEqual(meta['flash_size'], 512 * 1024)
        self.assertTrue(meta['checks']['telink_magic_at_0x0'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bsf.main(['verify', d]), 0)

    def test_1m_detected(self):
        img = telink_image(1024 * 1024, fill_upper_ff=True)
        rc, out = self.run_backup(img)
        self.assertEqual(rc, 0, out)
        d = self.only_run_dir()
        self.assertEqual(slurp(os.path.join(d, 'stock_full_1MB.bin')), img)

    def test_forced_1m_on_512k_chip_is_trimmed(self):
        img = telink_image(512 * 1024)
        rc, out = self.run_backup(img, extra=['--size', '1m'])
        self.assertEqual(rc, 0, out)
        d = self.only_run_dir()
        self.assertTrue(os.path.exists(os.path.join(d, 'stock_full_512KB.bin')))

    def test_all_ff_rejected(self):
        rc, out = self.run_backup(b'\xff' * (512 * 1024))
        self.assertEqual(rc, 1)
        self.assertIn('all 0xFF', out)

    def test_all_00_rejected(self):
        rc, out = self.run_backup(b'\x00' * (512 * 1024))
        self.assertEqual(rc, 1)
        self.assertIn('all 0x00', out)

    def test_one_bad_read_resolved_by_third(self):
        rc, out = self.run_backup(telink_image(512 * 1024), mode='flaky_once')
        self.assertEqual(rc, 0, out)
        self.assertIn('MISMATCH', out)
        meta = load_json((os.path.join(self.only_run_dir(), 'backup_info.json')))
        self.assertEqual(meta['reads_agreeing'], ['read2.bin', 'read3.bin'])  # read1 was the corrupted one

    def test_unstable_link_fails(self):
        # every read has one bad byte, each in a different place: the vote fixes it
        img = telink_image(512 * 1024)
        rc, out = self.run_backup(img, mode='always_flaky')
        self.assertEqual(rc, 0, out)
        self.assertIn('All three reads differ', out)
        self.assertEqual(slurp(os.path.join(self.only_run_dir(), 'stock_full_512KB.bin')), img)

    def test_flasher_error_fails(self):
        rc, out = self.run_backup(telink_image(512 * 1024), mode='fail')
        self.assertEqual(rc, 1)
        self.assertIn('exited with code 1', out)

    def test_partial_file_with_exit_0_fails(self):
        rc, out = self.run_backup(telink_image(512 * 1024), mode='interrupted')
        self.assertEqual(rc, 1)
        self.assertIn('Short read', out)

    def test_verify_detects_tampering(self):
        rc, _ = self.run_backup(telink_image(512 * 1024))
        d = self.only_run_dir()
        p = os.path.join(d, 'stock_full_512KB.bin')
        os.chmod(p, 0o644)
        with open(p, 'r+b') as f:
            f.write(b'\x00')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bsf.main(['verify', d]), 1)

    def test_single_log_file_across_runs(self):
        img = telink_image(512 * 1024)
        rc, _ = self.run_backup(img)
        self.assertEqual(rc, 0)
        rc, _ = self.run_backup(img, mode='fail')
        self.assertEqual(rc, 1)
        logp = os.path.join(self.tmp, 'backup.log')
        text = slurp(logp).decode()
        self.assertEqual(text.count('# RUN '), 2)
        self.assertIn('FAKE rf 0x000000', text)      # flasher output captured
        self.assertIn('BACKUP OK', text)             # wrapper messages too
        self.assertIn('ERROR: Flasher exited with code 1', text)
        for d in os.listdir(self.out):
            self.assertFalse(os.path.exists(os.path.join(self.out, d, 'flasher.log')))

    def test_one_manual_reset_for_whole_backup(self):
        os.environ['FAKE_HALT'] = os.path.join(self.tmp, 'halted')
        rc, out = self.run_backup(telink_image(512 * 1024))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count('>>> ACTION: unplug the 3V3'), 1)
        self.assertEqual(out.count('(tact 10000)'), 1)   # only the first read activates
        self.assertEqual(out.count('(tact 0)'), 4)       # two probes + two full reads
        self.assertEqual(out.count('>>>  PLUG 3V3 IN NOW  <<<'), 1)
        meta = load_json(os.path.join(self.only_run_dir(), 'backup_info.json'))
        self.assertEqual(meta['manual_resets'], 1)

    def test_lost_halt_asks_for_another_reset(self):
        os.environ['FAKE_HALT'] = os.path.join(self.tmp, 'halted')
        os.environ['FAKE_SLEEP_AFTER'] = '4'   # chip wakes during read1 (call 4: catch, 2 probes, read1)
        rc, out = self.run_backup(telink_image(512 * 1024))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count('>>> ACTION: unplug the 3V3'), 2)
        d = self.only_run_dir()
        self.assertTrue(os.path.exists(os.path.join(d, 'raw', 'read1_retry1.bin')))
        meta = load_json(os.path.join(d, 'backup_info.json'))
        self.assertEqual(meta['reads_agreeing'], ['read1_retry1.bin', 'read2.bin'])

    def test_plug_in_cue_printed_on_activate(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            c = bsf.PowerCues()
            for part in ('Reset module (RTS low)...\nActi', 'vate (10000 ms)...\n',
                         'SWire speed for CLK 48.0 MHz... ', 'ok.\n'):
                c.feed(part)
        out = buf.getvalue()
        self.assertEqual(out.count('>>>  PLUG 3V3 IN NOW  <<<'), 1)
        self.assertIn('\a', out)
        self.assertIn('Chip answered', out)

    def test_bad_first_probe_block_reread(self):
        img = telink_image(512 * 1024)
        rc, out = self.run_backup(img, mode='bad_probe')
        self.assertEqual(rc, 0, out)
        self.assertIn('Using the re-read', out)
        d = self.only_run_dir()
        self.assertEqual(slurp(os.path.join(d, 'stock_full_512KB.bin')), img)
        meta = load_json(os.path.join(d, 'backup_info.json'))
        self.assertEqual(meta['bytes_read_per_pass'], 512 * 1024)   # no 1 MB reads

    def test_macos_flasher_choice(self):
        tool = os.path.join(os.path.dirname(HERE), 'tools', 'TlsrComSwireWriter')
        old = sys.platform
        try:
            sys.platform = 'darwin'
            self.assertTrue(bsf.find_flasher(tool).endswith('TLSR825xComFlasher-macos.py'))
            self.assertTrue(bsf.find_flasher(tool, fastcatch=True).endswith('-macos-fastcatch.py'))
        finally:
            sys.platform = old

    def test_catch_block_never_kept(self):
        img = telink_image(512 * 1024)
        rc, out = self.run_backup(img, extra=['--size', '512k'])
        self.assertEqual(rc, 0, out)
        d = self.only_run_dir()
        self.assertEqual(slurp(os.path.join(d, 'stock_full_512KB.bin')), img)
        self.assertEqual(len(slurp(os.path.join(d, 'raw', 'catch1.bin'))), 256)
        self.assertEqual(load_json(os.path.join(d, 'backup_info.json'))['method'], 'two identical reads')

    def test_three_different_reads_majority_vote(self):
        img = telink_image(512 * 1024)
        rc, out = self.run_backup(img, mode='three_way', extra=['--size', '512k'])
        self.assertEqual(rc, 0, out)
        self.assertIn('Majority vote OK', out)
        d = self.only_run_dir()
        self.assertEqual(slurp(os.path.join(d, 'stock_full_512KB.bin')), img)
        meta = load_json(os.path.join(d, 'backup_info.json'))
        self.assertEqual(meta['method'], '2-of-3 byte majority')

    def test_no_majority_lists_ranges(self):
        rc, out = self.run_backup(telink_image(512 * 1024), mode='no_majority', extra=['--size', '512k'])
        self.assertEqual(rc, 1)
        self.assertIn('0x002000-0x002000', out)

    def test_merge_command_offline(self):
        img = telink_image(512 * 1024)
        run = os.path.join(self.out, 'stock_x')
        raw = os.path.join(run, 'raw')
        os.makedirs(raw)
        for k, off in enumerate((0x0, 0x3b000, 0x5000)):
            bad = bytearray(img)
            bad[off:off + 256] = bytes(256)
            with open(os.path.join(raw, 'read%d.bin' % (k + 1)), 'wb') as f:
                f.write(bad)
        with open(os.path.join(raw, 'read0_partial.bin'), 'wb') as f:
            f.write(img[:1000])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bsf.main(['merge', run, '--log', os.path.join(self.tmp, 'm.log')])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(slurp(os.path.join(run, 'stock_full_512KB.bin')), img)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bsf.main(['verify', run]), 0)

    def test_flasher_runs_unbuffered(self):
        r = bsf.Reader(sys.executable, FAKE, '/dev/x', 921600, 70, 0, 10)
        self.assertEqual(r.build_cmd(0, 16, 'x.bin')[1], '-u')

    def test_only_rf_commands_are_built(self):
        r = bsf.Reader(sys.executable, FAKE, '/dev/x', 921600, 70, 0, 10)
        cmd = r.build_cmd(0, 0x80000, 'x.bin')
        self.assertIn('rf', cmd)
        for bad in ('wf', 'we', 'es', 'ea', '-r'):
            self.assertNotIn(bad, cmd)
        with self.assertRaises(bsf.BackupError):
            bsf.Reader.assert_read_only([sys.executable, FAKE, 'wf', '0', 'x.bin'])
        with self.assertRaises(bsf.BackupError):
            bsf.Reader.assert_read_only([sys.executable, FAKE, 'rf', '0', '1', 'x', 'ea'])

    def test_never_overwrites(self):
        r = bsf.Reader(sys.executable, FAKE, '/dev/x', 921600, 70, 0, 10)
        existing = os.path.join(self.tmp, 'exists.bin')
        with open(existing, 'w'):
            pass
        with self.assertRaises(bsf.BackupError):
            r.read(0, 16, existing)

    def test_real_flasher_interface_check(self):
        tool = os.path.join(os.path.dirname(HERE), 'tools', 'TlsrComSwireWriter')
        if not os.path.isdir(tool):
            self.skipTest('vendored flasher not present')
        self.assertTrue(bsf.find_flasher(tool).endswith('.py'))


if __name__ == '__main__':
    unittest.main()
