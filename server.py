#!/usr/bin/env python3
"""iPerf Deck - runs an iperf3 server and serves a live web dashboard for it.

Standard library only. iperf3 is run with --json-stream so every one-second
interval is pushed to the browser over Server-Sent Events as it happens.
"""
import argparse
import csv
import io
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

__version__ = "0.0.0.589"
__stage__ = "ALPHA"

WINDOWS = os.name == "nt"
FROZEN = getattr(sys, "frozen", False)  # running from a packaged build
ROOT = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
if WINDOWS:
    DATA_DIR = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "iPerf Deck"
else:
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "iperf-deck"


def find_iperf():
    """A packaged build carries its own iperf3; otherwise use the one on PATH."""
    bundled = ROOT / "iperf3" / ("iperf3.exe" if WINDOWS else "iperf3")
    return str(bundled) if bundled.is_file() else shutil.which("iperf3")


IPERF = find_iperf()
HISTORY_FILE = DATA_DIR / "history.json"
HISTORY_MAX = 300
SPARK_POINTS = 48

HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:%-]{0,252}$")
RATE_RE = re.compile(r"^\d+(\.\d+)?[KMGkmg]?$")


# --------------------------------------------------------------------------
# Turning iperf3's JSON events into sessions
# --------------------------------------------------------------------------

def new_session(role, start):
    ts = start.get("test_start") or {}
    conn = start.get("connected") or []
    first = conn[0] if conn else {}
    if role == "server":
        peer = (start.get("accepted_connection") or {}).get("host") or first.get("remote_host")
        peer_port = None
    else:
        target = start.get("connecting_to") or {}
        peer = target.get("host") or first.get("remote_host")
        peer_port = target.get("port")
    stamp = (start.get("timestamp") or {}).get("timemillisecs")
    return {
        "id": uuid.uuid4().hex[:12],
        "role": role,
        "peer": peer,
        "peer_ip": first.get("remote_host"),
        "peer_port": peer_port,
        "local_host": first.get("local_host"),
        "started": stamp / 1000 if stamp else time.time(),
        "protocol": ts.get("protocol") or "TCP",
        "streams": ts.get("num_streams") or len(conn) or 1,
        "duration": ts.get("duration") or 0,
        "reverse": bool(ts.get("reverse")),
        "bidir": bool(ts.get("bidir")),
        "blksize": ts.get("blksize"),
        "target_bitrate": ts.get("target_bitrate") or 0,
        "mss": start.get("tcp_mss_default") or None,
        "sndbuf": start.get("sndbuf_actual"),
        "rcvbuf": start.get("rcvbuf_actual"),
        "version": start.get("version"),
        "sockets": [c.get("socket") for c in conn],
        "sdir": {},
        "status": "running",
        "error": None,
        "points": [],
        "summary": None,
    }


def make_point(sess, data):
    """One chart point per interval. tx/rx are from this host's point of view."""
    streams = data.get("streams") or []
    if not streams or any(s.get("omitted") for s in streams):
        return None
    secs = max(s.get("seconds") or 0 for s in streams)
    if secs < 0.2:  # iperf3 emits a tiny trailing interval with a meaningless rate
        return None
    tx = rx = None
    txb = rxb = 0
    retr = lost = pkts = None
    rtts, cwnds, jits = [], [], []
    per = {}
    for s in streams:
        bps = s.get("bits_per_second") or 0
        sock = s.get("socket")
        per[sock] = bps
        if s.get("sender"):
            sess["sdir"].setdefault(str(sock), "tx")
            tx = (tx or 0) + bps
            txb += s.get("bytes") or 0
            if "retransmits" in s:
                retr = (retr or 0) + s["retransmits"]
            if "rtt" in s:
                rtts.append(s["rtt"] / 1000)
            if "snd_cwnd" in s:
                cwnds.append(s["snd_cwnd"])
        else:
            sess["sdir"].setdefault(str(sock), "rx")
            rx = (rx or 0) + bps
            rxb += s.get("bytes") or 0
            if "jitter_ms" in s:
                jits.append(s["jitter_ms"])
                lost = (lost or 0) + (s.get("lost_packets") or 0)
                pkts = (pkts or 0) + (s.get("packets") or 0)
    return {
        "t": round(max(s.get("end") or 0 for s in streams), 3),
        "d": round(secs, 3),
        "tx": tx, "rx": rx, "txb": txb, "rxb": rxb,
        "retr": retr,
        "rtt": round(sum(rtts) / len(rtts), 3) if rtts else None,
        "cwnd": round(sum(cwnds) / len(cwnds)) if cwnds else None,
        "jit": round(sum(jits) / len(jits), 4) if jits else None,
        "lost": lost, "pkts": pkts,
        "s": [per.get(sock) for sock in sess["sockets"]],
    }


def summarize(sess, end):
    pts = sess["points"]

    def direction(key, bkey):
        vals = [p[key] for p in pts if p[key] is not None]
        if not vals:
            return None
        total = sum(p[bkey] for p in pts)
        secs = sum(p["d"] for p in pts if p[key] is not None)
        return {"avg": total * 8 / secs if secs else 0, "max": max(vals), "min": min(vals), "bytes": total}

    def series(key):
        return [p[key] for p in pts if p[key] is not None]

    rtts, retr, jits = series("rtt"), series("retr"), series("jit")
    lost, pkts = sum(series("lost")), sum(series("pkts"))
    end = end or {}
    cpu = end.get("cpu_utilization_percent") or {}
    udp = None
    if jits:
        udp = {"jitter_ms": sum(jits) / len(jits), "lost": lost, "packets": pkts,
               "lost_percent": 100 * lost / pkts if pkts else 0}
    elif sess["protocol"] == "UDP" and sess["role"] == "client" and (end.get("sum") or {}).get("packets"):
        # We only sent, but the client's end report carries the server's receive stats.
        s = end["sum"]
        udp = {"jitter_ms": s.get("jitter_ms"), "lost": s.get("lost_packets"),
               "packets": s.get("packets"), "lost_percent": s.get("lost_percent")}
    return {
        "tx": direction("tx", "txb"),
        "rx": direction("rx", "rxb"),
        "seconds": pts[-1]["t"] if pts else 0,
        "retransmits": sum(retr) if retr else None,
        "rtt": {"min": min(rtts), "avg": sum(rtts) / len(rtts), "max": max(rtts)} if rtts else None,
        "udp": udp,
        "cpu_host": cpu.get("host_total"),
        "cpu_remote": cpu.get("remote_total") or None,
        "congestion": end.get("sender_tcp_congestion") or end.get("receiver_tcp_congestion"),
    }


def light(sess):
    """History-list form of a session: no points, just a sparkline."""
    out = {k: v for k, v in sess.items() if k != "points"}
    totals = [(p["tx"] or 0) + (p["rx"] or 0) for p in sess["points"]]
    if len(totals) > SPARK_POINTS:
        step = len(totals) / SPARK_POINTS
        totals = [max(totals[int(i * step):max(int((i + 1) * step), int(i * step) + 1)])
                  for i in range(SPARK_POINTS)]
    out["spark"] = [round(v) for v in totals]
    return out


# --------------------------------------------------------------------------
# App state, event fan-out, iperf3 process runners
# --------------------------------------------------------------------------

class App:
    def __init__(self, iperf_port):
        self.lock = threading.RLock()
        self.subs = set()
        self.live = {}
        self.history = []
        self.status = {
            "server": {"running": False, "error": None, "port": iperf_port, "since": None},
            "client": {"running": False, "error": None, "target": None},
        }
        self.server = Runner(self, "server")
        self.client = Runner(self, "client")
        self.load_history()

    def load_history(self):
        try:
            self.history = json.loads(HISTORY_FILE.read_text())
        except (OSError, ValueError):
            self.history = []

    def save_history(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.history, separators=(",", ":")))
        tmp.replace(HISTORY_FILE)

    def publish(self, kind, **payload):
        msg = json.dumps({"type": kind, **payload}, separators=(",", ":"))
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    self.subs.discard(q)

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            q.put_nowait(json.dumps({"type": "snapshot", **self.snapshot()}, separators=(",", ":")))
            self.subs.add(q)
        return q

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status,
                "live": list(self.live.values()),
                "history": [light(s) for s in self.history],
                "host": host_info(),
            }

    def find(self, sid):
        with self.lock:
            if sid in self.live:
                return self.live[sid]
            return next((s for s in self.history if s["id"] == sid), None)

    def start_server(self, port):
        with self.lock:
            self.status["server"]["port"] = port
            self.server.start([IPERF, "-s", "-p", str(port), "--json-stream", "--forceflush"])

    def start_client(self, opts):
        host = str(opts.get("host", "")).strip()
        if not HOST_RE.match(host):
            raise ValueError("Enter a hostname or IP address to test against.")
        port = int_in(opts.get("port", 5201), 1, 65535, "Port")
        argv = [IPERF, "-c", host, "-p", str(port),
                "-t", str(int_in(opts.get("duration", 10), 1, 3600, "Duration")),
                "-P", str(int_in(opts.get("streams", 1), 1, 32, "Streams")),
                "--connect-timeout", "5000", "--json-stream", "--forceflush"]
        if opts.get("protocol") == "udp":
            argv.append("-u")
        rate = str(opts.get("bitrate") or "").strip()
        if rate:
            if not RATE_RE.match(rate):
                raise ValueError("Bitrate looks like 100M, 2.5G or 500K.")
            argv += ["-b", rate]
        direction = opts.get("direction")
        if direction == "download":
            argv.append("-R")
        elif direction == "both":
            argv.append("--bidir")
        with self.lock:
            self.status["client"]["target"] = f"{host}:{port}"
            self.client.start(argv)


class Runner:
    """Owns one iperf3 process and turns its event stream into sessions."""

    def __init__(self, app, role):
        self.app = app
        self.role = role
        self.proc = None
        self.session = None
        self.stopping = False
        self.last_error = None
        self.completed = 0
        self.argv = None
        self.started_at = 0

    def start(self, argv):
        app = self.app
        with app.lock:
            if self.proc and self.proc.poll() is None:
                raise ValueError(f"The iperf3 {self.role} is already running.")
            self.stopping = False
            self.last_error = None
            self.completed = 0
            self.argv = argv
            self.started_at = time.time()
            st = app.status[self.role]
            try:
                self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, text=True, bufsize=1)
            except OSError as e:
                st.update(running=False, error=f"Could not start iperf3: {e}")
                app.publish("status", status=app.status)
                raise ValueError(st["error"])
            st.update(running=True, error=None)
            if self.role == "server":
                st["since"] = self.started_at
            app.publish("status", status=app.status)
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def stop(self):
        with self.app.lock:
            proc = self.proc
            if not proc or proc.poll() is not None:
                return
            self.stopping = True
            proc.terminate()

        def kill():
            if proc.poll() is None:
                proc.kill()
        threading.Timer(3, kill).start()

    def _pump(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                self.last_error = line
                continue
            if isinstance(ev, dict):
                with self.app.lock:
                    self._handle(ev.get("event"), ev.get("data"))
        rc = proc.wait()
        app = self.app
        with app.lock:
            if self.session:
                self._finish("aborted", None, self.last_error or "iperf3 stopped before the test finished")
            st = app.status[self.role]
            st["running"] = False
            failed = not self.stopping and (rc != 0 or self.role == "server") and not self.completed
            st["error"] = (self.last_error or f"iperf3 exited with code {rc}") if failed else None
            if self.role == "server":
                st["since"] = None
            app.publish("status", status=app.status)
            # iperf3 -s occasionally dies after a bad client; bring it back.
            if self.role == "server" and not self.stopping and time.time() - self.started_at > 5:
                threading.Timer(1, self._restart).start()

    def _restart(self):
        try:
            self.start(self.argv)
        except ValueError:
            pass

    def _handle(self, event, data):
        app = self.app
        if event == "start" and isinstance(data, dict):
            if self.session:
                self._finish("aborted", None, "A new test started before this one finished")
            self.session = new_session(self.role, data)
            app.live[self.session["id"]] = self.session
            app.publish("session_start", session=self.session)
        elif event == "interval" and self.session and isinstance(data, dict):
            point = make_point(self.session, data)
            if point:
                first = not self.session["points"]
                self.session["points"].append(point)
                app.publish("point", id=self.session["id"], point=point,
                            **({"sdir": self.session["sdir"]} if first else {}))
        elif event == "end" and self.session:
            self._finish("done", data if isinstance(data, dict) else None, None)
        elif event == "error":
            self.last_error = str(data).removeprefix("error - ")
            if self.session:
                self._finish("aborted", None, self.last_error)

    def _finish(self, status, end, error):
        app, sess = self.app, self.session
        self.session = None
        sess["status"] = status if sess["points"] else "aborted"
        sess["error"] = error
        sess["summary"] = summarize(sess, end)
        app.live.pop(sess["id"], None)
        if sess["points"]:
            self.completed += 1
            app.history.insert(0, sess)
            del app.history[HISTORY_MAX:]
            app.save_history()
        app.publish("session_end", session=sess, kept=bool(sess["points"]))


def int_in(value, lo, hi, name):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number.")
    if not lo <= n <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}.")
    return n


_host_cache = {"at": 0, "value": None}


def host_info():
    if time.time() - _host_cache["at"] < 30:
        return _host_cache["value"]
    addrs = []
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=3).stdout
        for link in json.loads(out):
            if "LOOPBACK" in link.get("flags", []) or link.get("operstate") == "DOWN":
                continue
            for a in link.get("addr_info", []):
                addrs.append({"ifname": link.get("ifname"), "ip": a.get("local")})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if not addrs:  # no `ip` command (Windows, macOS): ask the socket layer instead
        found = set()
        try:
            found.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
        except OSError:
            pass
        try:  # the address used for outbound traffic; connecting a UDP socket sends nothing
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))
                found.add(probe.getsockname()[0])
        except OSError:
            pass
        addrs = [{"ifname": "", "ip": ip} for ip in sorted(found) if not ip.startswith("127.")]
    version = None
    try:
        version = subprocess.run([IPERF, "--version"], capture_output=True, text=True,
                                 timeout=3).stdout.split("\n")[0].split(" (")[0]
    except (OSError, subprocess.SubprocessError):
        pass
    _host_cache.update(at=time.time(), value={
        "hostname": socket.gethostname(), "addresses": addrs, "iperf": version})
    return _host_cache["value"]


def to_csv(sess):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["second", "send_bps", "receive_bps", "send_bytes", "receive_bytes", "retransmits",
                "rtt_ms", "cwnd_bytes", "jitter_ms", "lost_packets", "packets"])
    for p in sess["points"]:
        w.writerow(["" if v is None else v for v in (
            p["t"], p["tx"], p["rx"], p["txb"], p["rxb"], p["retr"], p["rtt"], p["cwnd"],
            p["jit"], p["lost"], p["pkts"])])
    return buf.getvalue()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "iperf-deck"

    def log_message(self, *args):
        pass

    def send_body(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":"))
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        app = self.app
        if path in ("/", "/index.html"):
            return self.send_body(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send_body(200, app.snapshot())
        if path == "/api/events":
            return self.stream()
        m = re.fullmatch(r"/api/session/([0-9a-f]{12})", path)
        if m:
            sess = app.find(m.group(1))
            return self.send_body(200, sess) if sess else self.send_body(404, {"error": "Not found"})
        m = re.fullmatch(r"/api/export/([0-9a-f]{12})\.(csv|json)", path)
        if m:
            sess = app.find(m.group(1))
            if not sess:
                return self.send_body(404, {"error": "Not found"})
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(sess["started"]))
            name = f"iperf-{stamp}.{m.group(2)}"
            head = {"Content-Disposition": f'attachment; filename="{name}"'}
            if m.group(2) == "csv":
                return self.send_body(200, to_csv(sess), "text/csv", head)
            return self.send_body(200, json.dumps(sess, indent=1), "application/json", head)
        self.send_body(404, {"error": "Not found"})

    def stream(self):
        q = self.app.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                try:
                    self.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with self.app.lock:
                self.app.subs.discard(q)

    def same_origin(self):
        """Reject cross-site writes: a web page elsewhere must not be able to drive iperf3."""
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return False
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == self.headers.get("Host")

    def do_POST(self):
        self.write("POST")

    def do_DELETE(self):
        self.write("DELETE")

    def write(self, method):
        path = urlparse(self.path).path
        app = self.app
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self.send_body(400, {"error": "Bad request"})
        if not self.same_origin() or not isinstance(body, dict):
            return self.send_body(403, {"error": "Forbidden"})
        try:
            if method == "POST" and path == "/api/server/start":
                app.start_server(int_in(body.get("port", app.status["server"]["port"]), 1, 65535, "Port"))
            elif method == "POST" and path == "/api/server/stop":
                app.server.stop()
            elif method == "POST" and path == "/api/client/start":
                app.start_client(body)
            elif method == "POST" and path == "/api/client/stop":
                app.client.stop()
            elif method == "DELETE" and path == "/api/history":
                with app.lock:
                    app.history.clear()
                    app.save_history()
                    app.publish("history", history=[])
            elif method == "DELETE" and re.fullmatch(r"/api/session/[0-9a-f]{12}", path):
                with app.lock:
                    sid = path.rsplit("/", 1)[1]
                    app.history[:] = [s for s in app.history if s["id"] != sid]
                    app.save_history()
                    app.publish("history", history=[light(s) for s in app.history])
            else:
                return self.send_body(404, {"error": "Not found"})
        except ValueError as e:
            return self.send_body(400, {"error": str(e)})
        self.send_body(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, address reuse would let a second copy bind the same port silently.
    allow_reuse_address = not WINDOWS


def main():
    ap = argparse.ArgumentParser(description="iperf3 server with a live web dashboard")
    ap.add_argument("--listen", default="127.0.0.1",
                    help="address the dashboard binds to (default 127.0.0.1; use 0.0.0.0 for the LAN)")
    ap.add_argument("--port", type=int, default=8520, help="dashboard port (default 8520)")
    ap.add_argument("--iperf-port", type=int, default=5201, help="iperf3 server port (default 5201)")
    ap.add_argument("--no-autostart", action="store_true", help="do not start the iperf3 server on launch")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser on launch")
    ap.add_argument("--no-browser", action="store_true", help="packaged builds: do not open a browser")
    ap.add_argument("--version", action="version", version=f"Perf-Deck {__version__} {__stage__}")
    args = ap.parse_args()

    if not IPERF:
        raise SystemExit("iperf3 is not installed (for example: sudo dnf install iperf3)")

    url = f"http://{'127.0.0.1' if args.listen == '0.0.0.0' else args.listen}:{args.port}"
    browse = (args.open or FROZEN) and not args.no_browser
    try:
        httpd = Server((args.listen, args.port), Handler)
    except OSError as e:
        if browse:  # most likely a second launch: show the copy that is already running
            webbrowser.open(url)
        raise SystemExit(f"Cannot use port {args.port} ({e}). iPerf Deck may already be running at {url}")

    app = App(args.iperf_port)
    Handler.app = app
    if not args.no_autostart:
        try:
            app.start_server(args.iperf_port)
        except ValueError as e:
            print(e)

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)

    print(f"Perf-Deck {__version__} {__stage__}: {url}  (iperf3 server on port {args.iperf_port})", flush=True)
    if FROZEN:
        print("Leave this window open while you use the dashboard. Close it to stop.", flush=True)
    if browse:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server.stop()
        app.client.stop()


if __name__ == "__main__":
    main()
