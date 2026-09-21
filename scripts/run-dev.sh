#!/bin/sh
# Run Oversteer from this source tree (needs a meson build dir: see RELEASING.md).
cd "$(dirname "$0")/.."
[ -d build ] || meson setup build -Dprefix="$HOME/.local" -Dudev_rules_dir=packed
ninja -C build >/dev/null
MESON_BUILD_ROOT="$PWD/build" MESON_SOURCE_ROOT="$PWD" exec python3 build/bin/oversteer "$@"
