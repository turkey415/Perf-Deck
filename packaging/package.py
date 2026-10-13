#!/usr/bin/env python3
"""Freezes iPerf Deck with PyInstaller and archives it with the bundled iperf3.

Run packaging/build_iperf3.sh first. Output: dist/Perf-Deck-<version>-<platform>-x64.(zip|tar.gz)
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / "vendor" / "iperf3"
WINDOWS = os.name == "nt"


def main():
    version = re.search(r'__version__ = "([^"]+)"', (REPO / "server.py").read_text()).group(1)
    if not (VENDOR / ("iperf3.exe" if WINDOWS else "iperf3")).is_file():
        raise SystemExit("vendor/iperf3 is missing: run packaging/build_iperf3.sh first")

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
           "--name", "iperf-deck", "--add-data", f"index.html{os.pathsep}."]
    for f in sorted(VENDOR.iterdir()):
        kind = "--add-binary" if f.suffix in ("", ".exe", ".dll") else "--add-data"
        cmd += [kind, f"{f}{os.pathsep}iperf3"]
    subprocess.run(cmd + ["server.py"], cwd=REPO, check=True)

    app = REPO / "dist" / "iperf-deck"
    for name in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.md"):
        shutil.copy(REPO / name, app)
    platform = "windows" if WINDOWS else "linux"
    archive = shutil.make_archive(str(REPO / "dist" / f"Perf-Deck-{version}-{platform}-x64"),
                                  "zip" if WINDOWS else "gztar", REPO / "dist", "iperf-deck")
    print(archive)


if __name__ == "__main__":
    main()
