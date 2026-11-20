#!/usr/bin/env python3
"""Turn the firmware's Adafruit-GFX font headers into JSON for Tag Designer.

Usage: python3 extract_tag_fonts.py <firmware src dir> <out dir>
The headers come from atc1441/ATC_TLSR_Paper Firmware/src (font16.h, font_60.h,
font30.h, Roboto_Black_80.h). Each JSON holds the packed glyph bitmaps (base64)
and the glyph table exactly as the firmware's obdWriteStringCustom reads them.
"""
import base64
import json
import os
import re
import sys

FONTS = [  # header, C name, label shown in the app, nominal pixel size, used by the firmware for
    ("font16.h", "Dialog_plain_16", "Dialog 16 (tag default)", 16, "name, battery and status lines"),
    ("font_60.h", "DSEG14_Classic_Mini_Regular_40", "DSEG14 40 (tag clock)", 40, "the big clock digits"),
    ("font30.h", "Special_Elite_Regular_30", "Special Elite 30", 30, "the temperature on e-paper models"),
    ("Roboto_Black_80.h", "Roboto_Black_80", "Roboto Black 80", 80, "included in the firmware, not drawn by default"),
]


def parse(path, cname):
    src = open(path, encoding="latin-1").read()
    bm = re.search(re.escape(cname) + r"Bitmaps\[\][^{]*\{(.*?)\};", src, re.S).group(1)
    bitmap = bytes(int(h, 16) for h in re.findall(r"0x([0-9A-Fa-f]{1,2})", bm))
    gl = re.search(re.escape(cname) + r"Glyphs\[\][^{]*\{(.*?)\n\};", src, re.S).group(1)
    glyphs = [[int(v) for v in m] for m in re.findall(r"\{\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\s*\}", gl)]
    tail = re.search(r"const GFXfont " + re.escape(cname) + r"[^{]*\{(.*?)\};", src, re.S).group(1)
    first, last, yadv = [int(v, 0) for v in tail.split(",")[-3:]]
    # the squix.ch headers stop at '}' although they declare last = '~'; the firmware
    # would read past the table for '~', so treat it as missing (skipped, like other unknown characters)
    assert len(glyphs) in (last - first, last - first + 1), (cname, len(glyphs), first, last)
    last = first + len(glyphs) - 1
    for off, w, h, *_ in glyphs:
        assert off + (w * h + 7) // 8 <= len(bitmap), (cname, off, w, h)
    return bitmap, glyphs, first, last, yadv


def main(src, out):
    os.makedirs(out, exist_ok=True)
    index = []
    for header, cname, label, size, use in FONTS:
        bitmap, glyphs, first, last, yadv = parse(os.path.join(src, header), cname)
        drawn = [g for g in glyphs if g[1] * g[2] > 1]
        ascent = max(-g[5] for g in drawn)
        descent = max(g[2] + g[5] for g in drawn)
        data = {"id": cname, "name": label, "size": size, "use": use, "first": first, "last": last,
                "yAdvance": yadv, "ascent": ascent, "descent": max(0, descent),
                "glyphs": glyphs, "bitmap": base64.b64encode(bitmap).decode()}
        with open(os.path.join(out, cname + ".json"), "w") as f:
            json.dump(data, f, separators=(",", ":"))
        index.append({"id": cname, "name": label, "size": size, "use": use})
        print("%-32s %3d glyphs, %5d bytes, ascent %d, descent %d, yAdvance %d" % (cname, len(glyphs), len(bitmap), ascent, descent, yadv))
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump(index, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
