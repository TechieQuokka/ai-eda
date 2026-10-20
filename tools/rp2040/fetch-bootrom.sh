#!/usr/bin/env bash
# Download the RP2040 boot ROM (revision B2, from raspberrypi/pico-bootrom-rp2040) that the
# rp2040js emulator needs, into tools/rp2040/dl/ (git-ignored: it is not ours to redistribute).
# Needs arm-none-eabi-objcopy (gcc-arm-none-eabi) to turn the ELF into a raw image.
set -euo pipefail
cd "$(dirname "$0")"
SHA256=3a3c4c617d83bc9b910a6fa7bfdffa11ce26ff89370271d861507be821cc49aa
mkdir -p dl
curl -fL -o dl/b2.elf https://github.com/raspberrypi/pico-bootrom-rp2040/releases/download/b2/b2.elf
echo "${SHA256}  dl/b2.elf" | sha256sum -c -
arm-none-eabi-objcopy -O binary dl/b2.elf dl/bootrom-b2.bin
