#!/usr/bin/env bash
# Download the Freerouting autorouter used by `eda pcb` into tools/freerouting/dl/.
# v2.1.0 is pinned: it is the newest release whose jar runs on Java 21
# (2.2+ needs Java 25; the 2.5.0 native CLI hangs on a GraalVM serialization error).
set -euo pipefail
cd "$(dirname "$0")"
VERSION=2.1.0
SHA256=2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def
mkdir -p dl
curl -fL -o "dl/freerouting-${VERSION}.jar" \
  "https://github.com/freerouting/freerouting/releases/download/v${VERSION}/freerouting-${VERSION}.jar"
echo "${SHA256}  dl/freerouting-${VERSION}.jar" | sha256sum -c -
