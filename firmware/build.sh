#!/usr/bin/env bash
# Rebuild the tag firmware from source (Linux, x86-64).
#   firmware/build.sh            -> firmware/out/ATC_Paper_picksmart.bin (Telink OTA format)
# Takes atc1441/ATC_TLSR_Paper at commit 3778d29, applies atc_port/picksmart_st7305.patch
# (ST7305 LCD driver, picture keep/read-back, password lock) and builds with the
# repository's own tc32 toolchain.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
WORK="$HERE/build"
if [ ! -d "$WORK/ATC_TLSR_Paper" ]; then
  mkdir -p "$WORK"
  git clone https://github.com/atc1441/ATC_TLSR_Paper.git "$WORK/ATC_TLSR_Paper"
fi
cd "$WORK/ATC_TLSR_Paper"
git checkout -q -f 3778d29
git clean -fdq Firmware/src
git apply "$ROOT/atc_port/picksmart_st7305.patch"
chmod +x Firmware/tc32_linux/bin/* Firmware/tc32_linux/tc32-elf/bin/* 2>/dev/null || true
cd Firmware
make clean >/dev/null 2>&1 || true
make
mkdir -p "$HERE/out"
python3 -c "
import sys; sys.path.insert(0, '$ROOT/atc_port')
import telink_ota as T, hashlib
img = T.to_telink_image(open('ATC_Paper.bin', 'rb').read())
open('$HERE/out/ATC_Paper_picksmart.bin', 'wb').write(img)
print(len(img), 'bytes, sha256', hashlib.sha256(img).hexdigest())"
