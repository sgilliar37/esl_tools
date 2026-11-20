#!/usr/bin/env python3
"""Stand-in for TLSR825xComFlasher.py used by the tests. No hardware involved.

Same CLI as the real tool: [-p PORT] [-b BAUD] [-t TACT] [-c CLK] rf ADDRESS SIZE FILENAME
Behaviour comes from environment variables:
  FAKE_IMAGE   file holding the simulated flash (its length = chip size; reads wrap)
  FAKE_MODE    ok | flaky_once | always_flaky | fail | short | interrupted
  FAKE_STATE   counter file so a mode can change between calls
"""
import argparse, os, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-p'); ap.add_argument('-b'); ap.add_argument('-t'); ap.add_argument('-c')
    ap.add_argument('-r', action='store_true')
    sub = ap.add_subparsers(dest='op')
    rf = sub.add_parser('rf')
    rf.add_argument('address', type=lambda x: int(x, 0))
    rf.add_argument('size', type=lambda x: int(x, 0))
    rf.add_argument('filename')
    for name in ('wf', 'we', 'es', 'ea'):
        sub.add_parser(name).add_argument('rest', nargs='*')
    a = ap.parse_args()
    if a.op != 'rf' or a.r:
        print('FAKE: forbidden operation %s' % a.op); sys.exit(99)
    # keep the strings the wrapper looks for: 'rf' add_argument('address' add_argument('size' add_argument('filename'
    flash = open(os.environ['FAKE_IMAGE'], 'rb').read()
    mode = os.environ.get('FAKE_MODE', 'ok')
    state = os.environ.get('FAKE_STATE')
    n = 0
    if state:
        n = int(open(state).read()) if os.path.exists(state) else 0
        open(state, 'w').write(str(n + 1))
    # Simulated halt: activation (-t > 0) soft-resets and halts the CPU. A run with
    # -t 0 only works while the halt from an earlier run still holds.
    # FAKE_SLEEP_AFTER=N: the halt is lost after N calls (the chip "wakes up").
    halt = os.environ.get('FAKE_HALT')
    if halt:
        if int(a.t or 0) > 0:
            print('Activate (%s ms)...' % a.t)
            print('SWire speed for CLK 48.0 MHz... ok.')
            open(halt, 'w').write('1')
        elif not os.path.exists(halt):
            print('Chip sleep? -> Use reset chip (RTS-RST): see option --tact'); sys.exit(1)
        wake = os.environ.get('FAKE_SLEEP_AFTER')
        if wake and n + 1 == int(wake):
            os.remove(halt)
            print('Chip sleep? -> Use reset chip (RTS-RST): see option --tact'); sys.exit(1)
    print('FAKE rf 0x%06x 0x%x -> %s (tact %s)' % (a.address, a.size, a.filename, a.t))
    if mode == 'fail':
        print('Error Read Flash data!'); sys.exit(1)
    data = bytearray(flash[(a.address + i) % len(flash)] for i in range(a.size))
    if int(a.t or 0) > 0:
        data[:256] = bytes(256)   # like the real board: first block after activation reads as zeros
    if mode == 'bad_probe' and n == 1:
        data[:256] = bytes(256)
    full = a.size >= 512 * 1024
    if full and mode == 'three_way' and n >= 1:
        # each full read has its own bad block, so no two reads are identical
        o = 0x1000 * n
        data[o:o + 256] = bytes(256)
    if full and mode == 'no_majority' and n >= 1:
        data[0x2000] = n
    if full and (mode == 'always_flaky' or (mode == 'flaky_once' and n == 3)):
        data[0x1234 + n] ^= 0x5A
    if mode in ('short', 'interrupted') and full:
        data = data[:a.size // 2]
    open(a.filename, 'wb').write(data)
    sys.exit(0)

if __name__ == '__main__':
    main()
