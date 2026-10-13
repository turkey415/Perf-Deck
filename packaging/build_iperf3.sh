#!/usr/bin/env bash
# Builds the pinned iperf3 release from ESnet's official source into vendor/iperf3/.
# Runs on Linux and, for the Windows build, inside Cygwin.
set -euo pipefail

VERSION=3.22
SHA256=1c0d0fb02c52626111d6e132db80edfbf27bbaff8bd9245df2a371dcb0b35a92

out="$(cd "$(dirname "$0")/.." && pwd)/vendor/iperf3"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cd "$work"
curl -fsSLO "https://downloads.es.net/pub/iperf/iperf-$VERSION.tar.gz"
echo "$SHA256  iperf-$VERSION.tar.gz" | sha256sum -c -
tar xzf "iperf-$VERSION.tar.gz"
cd "iperf-$VERSION"

# Static libiperf and no OpenSSL, so the binary depends only on the C runtime.
./configure --disable-shared --without-openssl --without-sctp > /dev/null
make -j"$(nproc)" > /dev/null

exe=src/iperf3
[ -f src/iperf3.exe ] && exe=src/iperf3.exe
rm -rf "$out"
mkdir -p "$out"
cp "$exe" "$out/"
cp LICENSE "$out/iperf3-LICENSE.txt"
strip "$out/$(basename "$exe")"

# A Cygwin-built iperf3 needs the Cygwin runtime DLLs beside it.
if [ "$(uname -o)" = Cygwin ]; then
    ldd "$exe" | awk '$3 ~ /^\/usr\/bin\//{print $3}' | sort -u | xargs -r cp -t "$out"
    uname -r > "$out/cygwin-version.txt"
fi

# Version check on the build-tree binary: under Cygwin the vendored copy sits beside a second
# cygwin1.dll and cannot be launched from this shell. packaging/smoke_test.py runs the real one.
"$exe" --version
ls -la "$out"
