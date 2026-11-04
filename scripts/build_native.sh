#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$root/build/native"
"${CC:-cc}" -O2 -fPIC -shared "$root/fpga/uart/driver/mmio.c" -o "$root/build/native/libfm_uart_mmio.so"
"${CXX:-c++}" -O2 -std=c++14 -I"$root/bev_postprocess" "$root/bev_postprocess/postprocess_main.cpp" "$root/bev_postprocess/filter.cpp" "$root/bev_postprocess/nms.cpp" -o "$root/build/native/postprocess"
printf '%s\n' 'Built native files only. Nothing deployed or executed against hardware.'
