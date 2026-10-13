# iPerf Deck

An iperf3 server with a live web dashboard: a dark instrument panel that shows every test as it
runs. Python standard library only, one HTML file, no build step and nothing loaded from the
internet, so it works on an offline LAN.

## What it shows

- **Receive and Send dials** with a peak marker, auto-ranging scale and seven-segment readouts for
  average, peak, low and data transferred.
- **Needle meters** for round-trip time, congestion window, jitter and retransmits per second, each
  with Live / Peak / Avg / Hold buttons.
- **LED bar meters** for elapsed time, packet loss and CPU on both machines, plus counters for
  retransmits, lost packets, packets and streams.
- **Charts** of throughput per second, per stream, retransmits, round-trip time, congestion window,
  jitter and loss, with a hover readout.
- **History** of the last 300 tests, with CSV and JSON export.

Tests run back to back with the same machine (a send test followed by a receive test) are treated
as one run, so meters that one direction cannot measure keep the reading from the other.

You can also start tests from the page: target host, port, length, streams, bitrate cap, TCP or
UDP, and send, receive or both.

## Requirements

- Python 3.9 or newer
- iperf3 3.17 or newer (it needs `--json-stream`)
- Linux (it uses `ip` to list this machine's addresses)

## Run it

    python3 server.py                     # dashboard on http://127.0.0.1:8520, iperf3 server on 5201
    python3 server.py --listen 0.0.0.0    # let other machines on the LAN open the dashboard

Other flags: `--port`, `--iperf-port`, `--no-autostart`.

Then run `iperf3 -c <this machine>` from another machine. Add `-R` to test the other direction;
retransmits, round-trip time and congestion window are only reported by whichever side sends.

The dashboard has no login. Anyone who can reach it can start and stop tests from this machine,
which is why it binds to localhost unless told otherwise.

## Run it as a service

With the repo cloned to `~/iperf-deck`:

    mkdir -p ~/.config/systemd/user
    cp iperf-deck.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now iperf-deck

It starts when you log in. For it to run without a login: `loginctl enable-linger $USER`.

## Files

- `server.py` runs iperf3, turns its event stream into sessions and serves the page and API.
- `index.html` is the whole interface. It is read from disk on each request, so edits only need a
  browser refresh.
- Results are kept in `~/.local/share/iperf-deck/history.json`.
