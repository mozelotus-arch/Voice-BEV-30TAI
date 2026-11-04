#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
camera="$root/application/camera_continuous"
third="$camera/third_party"
build="$root/build/camera"
mkdir -p "$build/include/libuvc"
python3 - "$third/libuvc/include/libuvc/libuvc_config.h.in" "$build/include/libuvc/libuvc_config.h" <<'PY'
import sys
from pathlib import Path
text=Path(sys.argv[1]).read_text()
for key,value in [('MAJOR','0'),('MINOR','0'),('PATCH','9')]:
    text=text.replace('@libuvc_VERSION_'+key+'@',value)
text=text.replace('@libuvc_VERSION@','0.0.9').replace('#cmakedefine LIBUVC_HAS_JPEG 1','/* JPEG decoded by caller */')
Path(sys.argv[2]).write_text(text)
PY
"${CC:-cc}" -O2 -fPIC -shared -pthread -DLIBUVC_NUM_TRANSFER_BUFS=20 -I"$build/include" -I"$third" -I"$third/libuvc/include" "$third/libuvc/src/ctrl.c" "$third/libuvc/src/ctrl-gen.c" "$third/libuvc/src/device.c" "$third/libuvc/src/diag.c" "$third/libuvc/src/frame.c" "$third/libuvc/src/init.c" "$third/libuvc/src/stream.c" "$third/libuvc/src/misc.c" -Wl,-soname,libuvc_probe.so -l:libusb-1.0.so.0 -o "$build/libuvc_probe.so"
"${CC:-cc}" -O2 -pthread -I"$build/include" -I"$third/libuvc/include" "$camera/ring_stream.c" -L"$build" '-Wl,-rpath,$ORIGIN' -l:libuvc_probe.so -o "$build/ring_stream"
printf '%s\n' 'Build only. Verify target ABI, then regenerate release hashes before explicit deployment.'
