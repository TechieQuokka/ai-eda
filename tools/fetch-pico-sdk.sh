#!/usr/bin/env bash
# Fetch the Raspberry Pi pico-sdk used by `eda fw --build` into tools/pico-sdk/
# (git-ignored; about 440 MB with submodules). Set PICO_SDK_PATH to use another checkout.
# The compiler comes from the system: sudo apt install gcc-arm-none-eabi libnewlib-arm-none-eabi cmake
set -euo pipefail
cd "$(dirname "$0")"
VERSION=2.3.1
if [ -d pico-sdk ]; then
  echo "tools/pico-sdk already exists ($(git -C pico-sdk describe --tags 2>/dev/null || echo unknown))"
  exit 0
fi
git clone --depth 1 --branch "${VERSION}" --recurse-submodules --shallow-submodules \
  https://github.com/raspberrypi/pico-sdk.git pico-sdk
