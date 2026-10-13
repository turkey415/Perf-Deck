#!/usr/bin/env python3
"""Starts a packaged build, runs a real test through its bundled iperf3 and checks the result."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXE = REPO / "dist" / "iperf-deck" / ("iperf-deck.exe" if os.name == "nt" else "iperf-deck")
URL = "http://127.0.0.1:8531"


def call(path, body=None):
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as res:
        return json.load(res)


def wait_for(what, check, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            value = check()
            if value:
                return value
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"FAILED: timed out waiting for {what}")


def main():
    scratch = tempfile.mkdtemp()  # keep test results out of the real history
    env = {**os.environ, "APPDATA": scratch, "XDG_DATA_HOME": scratch}
    proc = subprocess.Popen([str(EXE), "--no-browser", "--port", "8531", "--iperf-port", "5231"], env=env)
    try:
        state = wait_for("the iperf3 server to listen", lambda: (s := call("/api/state"))["status"]["server"]["running"] and s, 30)
        print("bundled:", state["host"]["iperf"], "| addresses:", state["host"]["addresses"])
        if b"Instruments" not in urllib.request.urlopen(URL + "/", timeout=5).read():
            raise SystemExit("FAILED: dashboard page did not load")
        call("/api/client/start", {"host": "127.0.0.1", "port": 5231, "duration": 3, "streams": 2})
        # A loopback test is seen twice: once by the client and once by the server.
        history = wait_for("both sides of the test to finish", lambda: len(h := call("/api/state")["history"]) >= 2 and h, 40)
        for s in history:
            rate = (s["summary"]["tx"] or s["summary"]["rx"])["avg"]
            print(f'{s["role"]:6} {s["status"]} {s["summary"]["seconds"]:.0f}s {rate / 1e9:.2f} Gbps retransmits={s["summary"]["retransmits"]}')
            if s["status"] != "done" or s["summary"]["seconds"] < 2 or rate <= 0:
                raise SystemExit("FAILED: test did not complete cleanly")
        print("OK")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
