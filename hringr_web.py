#!/usr/bin/env python3
# Copyright (C) 2026 Daniele "DLx" Lussana
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versione web di hringr.py: serve hringr.html e i segnali (CPU, RAM, rete, audio) via SSE.

Il disegno gira nel browser (WebGL2); qui girano solo le misure, che un browser non può leggere.
Riusa da hringr.py i thread dei segnali (stats, bridge cava/mpd-spectrum, loopback).

Uso:   ./run_web.sh            # poi apri http://<ip-del-pi>:8766/
URL:   /?audio_source=bridge|loopback|auto&shape=bubbles&inner=liquid&liquid_color=7B5CFF&demo=1
Tasti nella pagina: F schermo intero (o doppio clic), D demo, A cambia audio bridge<->loopback.
I valori predefiniti vengono da config.ini; i parametri dell'URL prevalgono.
"""
import argparse
import ipaddress
import json
import socket
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import journal_stream
import hringr  # solo funzioni dei segnali: pygame/moderngl si importano in hringr.main()

HERE = Path(__file__).resolve().parent
WEB_KEYS = ("shape", "inner", "liquid_color", "tri_size", "max_mbps", "audio_source")
BACKEND_KEYS = ("bridge_url", "bridge_scale", "audio_device", "audio_gain")
MAX_CLIENTS = 20                       # ogni scheda del browser = un thread che trasmette a 25 Hz

stats, bridge, loop = hringr.Signals(), hringr.Signals(), hringr.Signals()
started, lock = set(), threading.Lock()
clients = threading.BoundedSemaphore(MAX_CLIENTS)
args = None
journal = None                         # journal_stream.Journal, oppure None se disattivato in config.ini
JOURNAL_DEFAULTS = {
    "enabled": "yes", "network": "no", "max_events": "600", "message_max": "200", "window_s": "90",
    "max_lanes": "8", "height": "0.4", "opacity": "0.9", "font_scale": "1", "status_label": "yes", "direction": "ltr", "ring_tint": "0.9",
    "save_path": "", "save_max_kb": "1024",
    # emerg alert crit err warning notice info debug
    "colors": "B026FF, B026FF, FF2D55, FF7A1A, FFD60A, 5AC8FA, 0A84FF, 8E8E93",
}
TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

def ensure(name):
    """Avvia una sola volta il thread richiesto (il loopback apre la scheda audio solo se serve)."""
    with lock:
        if name in started:
            return
        started.add(name)
        if name == "stats":
            hringr.start_stats_thread(stats)
        elif name == "bridge":
            hringr.start_bridge_thread(bridge, args.bridge_url, args.bridge_scale)
        else:
            hringr.start_audio_thread(loop, args.audio_device, args.audio_gain)


def in_tailnet(ip):
    a = ipaddress.ip_address(ip)
    return any(a in n for n in TAILNET if n.version == a.version)


def journal_cfg(cp):
    """Sezione [journal] di config.ini, validata. Esce con un messaggio chiaro se un valore è sbagliato."""
    c = {k: cp.get("journal", k, fallback=v).strip() for k, v in JOURNAL_DEFAULTS.items()}
    try:
        yes = {"yes": True, "no": False, "true": True, "false": False, "on": True, "off": False, "1": True, "0": False}
        out = {"enabled": yes[c["enabled"].lower()]}
        assert c["network"].lower() in ("no", "tailscale", "yes"), "network: no, tailscale o yes"
        out["network"] = c["network"].lower()
        for k, lo, hi in (("max_events", 10, 20000), ("message_max", 20, 2000), ("window_s", 5, 3600), ("max_lanes", 1, 30),
                          ("save_max_kb", 16, 1048576)):
            out[k] = int(c[k])
            assert lo <= out[k] <= hi, f"{k} must be between {lo} and {hi}"
        for k in ("height", "opacity", "ring_tint", "font_scale"):
            out[k] = float(c[k])
            assert 0 <= out[k] <= (3 if k == "font_scale" else 1), f"{k} must be between 0 and {3 if k == 'font_scale' else 1}"
        assert out["font_scale"] > 0, "font_scale must be greater than 0"
        assert c["direction"] in ("ltr", "rtl"), "direction: ltr o rtl"
        out["direction"], out["save_path"] = c["direction"], c["save_path"]
        out["status_label"] = yes[c["status_label"].lower()]
        out["colors"] = [x.strip().lstrip("#") for x in c["colors"].split(",")]
        assert len(out["colors"]) == 8 and all(len(x) == 6 and int(x, 16) >= 0 for x in out["colors"]), \
            "colors: 8 RRGGBB colors needed (emerg, alert, crit, err, warning, notice, info, debug)"
    except (KeyError, ValueError, AssertionError) as e:
        raise SystemExit(f"config.ini [journal]: {e.args[0] if isinstance(e, AssertionError) else 'invalid value: ' + str(e)}")
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")   # pagina servita da un altro web server
        self.end_headers()
        self.wfile.write(body)

    def journal_ok(self):
        """Il journal contiene dati sensibili: solo da loopback (client e Host), salvo journal.network = yes."""
        if journal is None:
            return False
        mode = args.jcfg["network"]
        if mode == "yes":
            return True
        host = urlparse("//" + self.headers.get("Host", "")).hostname
        ip = self.client_address[0].removeprefix("::ffff:")
        if ip in LOOPBACK and host in LOOPBACK:
            return True
        # tailscale: client nella tailnet e Host = IP tailnet, nome *.ts.net o hostname di questa macchina
        # (un Host diverso è il segno di un attacco DNS rebinding da una pagina web esterna)
        if mode != "tailscale" or not host:
            return False
        try:
            if not in_tailnet(ip):
                return False
            return in_tailnet(host) if host[0] in "0123456789" else \
                host.endswith(".ts.net") or host == socket.gethostname().lower()
        except ValueError:
            return False

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/journal":
            self.journal_events()
        elif u.path == "/":
            self.send((HERE / "hringr.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path.startswith("/fonts/"):      # solo file .ttf della cartella fonts/, mai percorsi con "/"
            name = u.path[7:]
            f = HERE / "fonts" / name
            if "/" in name or not name.endswith(".ttf") or not f.is_file():
                self.send_error(404)
            else:
                self.send(f.read_bytes(), "font/ttf")
        elif u.path == "/config.json":
            j = {k: v for k, v in args.jcfg.items() if k not in ("save_path", "network")} if self.journal_ok() else None
            self.send(json.dumps({**args.defaults, "params": hringr.PARAMS, "journal": j, "hostname": socket.gethostname()}).encode(), "application/json")
        elif u.path == "/events":
            self.events(parse_qs(u.query).get("audio_source", [args.defaults["audio_source"]])[0])
        else:
            self.send_error(404)

    def journal_events(self):
        # Niente "Access-Control-Allow-Origin: *" qui: una pagina esterna non deve poter leggere il journal.
        # Si ammette solo un'origine con lo stesso nome host (pagina su :80, flusso su :8766).
        if not self.journal_ok():
            self.send_error(403, "journal available only from localhost (see config.ini [journal])")
            return
        if not clients.acquire(blocking=False):
            self.send_error(503, "too many connected clients")
            return
        origin = self.headers.get("Origin", "")
        same_host = urlparse(origin).hostname == urlparse("//" + self.headers.get("Host", "")).hostname
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        if origin and same_host:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        last = journal.seq                     # solo messaggi nuovi per questo client
        try:
            while True:
                ev, counts, status = journal.wait(last, 15)
                if ev:
                    last = ev[-1]["id"]
                self.wfile.write(f"data: {json.dumps({'ev': ev, 'counts': counts, 'status': status})}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.2)                # al massimo 5 pacchetti/s: una raffica di log non soffoca il browser
        except OSError:
            pass
        finally:
            clients.release()

    def events(self, src):
        if src not in ("bridge", "loopback"):   # auto: come hringr.py, bridge se raggiungibile
            src = "bridge" if hringr.bridge_available(args.bridge_url) else "loopback"
        if not clients.acquire(blocking=False):
            self.send_error(503, "too many connected clients")
            return
        ensure("stats")
        ensure(src)
        aud = bridge if src == "bridge" else loop
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                m = {"audio": aud.audio, "audio_status": aud.audio_status, "down_mbps": stats.down_mbps,
                     "up_mbps": stats.up_mbps, "cpu": stats.cpu, "ram": stats.ram, "source": src,
                     "uptime": hringr.read_uptime()}
                self.wfile.write(f"data: {json.dumps(m)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.04)
        except OSError:                        # client chiuso (broken pipe, reset, abort)
            pass
        finally:
            clients.release()


def main():
    global args
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.ini"))
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from the network; 127.0.0.1 = local only")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--bridge-url", default=hringr.DEFAULTS["bridge_url"])
    ap.add_argument("--bridge-scale", type=hringr._positive, default=hringr.DEFAULTS["bridge_scale"])
    ap.add_argument("--audio-device", default=None)
    ap.add_argument("--audio-gain", type=float, default=0.0)
    cp = hringr.read_config(ap.parse_known_args()[0].config)
    for act in ap._actions:                # bridge_url, bridge_scale, audio_device, audio_gain anche da config.ini
        if act.dest in BACKEND_KEYS and cp.has_option("hringr", act.dest):
            try:
                ap.set_defaults(**{act.dest: act.type(cp.get("hringr", act.dest)) if act.type else cp.get("hringr", act.dest)})
            except (ValueError, argparse.ArgumentTypeError) as e:
                ap.error(f"{ap.parse_known_args()[0].config}: {act.dest}: {e}")
    args = ap.parse_args()
    args.defaults = {k: cp.get("hringr", k.replace("_", "-"), fallback=cp.get("hringr", k, fallback=str(hringr.DEFAULTS[k])))
                     for k in WEB_KEYS}
    for k in ("max_mbps", "tri_size"):     # stessa regola del desktop: > 0
        try:
            hringr._positive(args.defaults[k])
        except (ValueError, argparse.ArgumentTypeError) as e:
            ap.error(f"{args.config}: {k}: {e}")
    global journal
    args.jcfg = journal_cfg(cp)
    if args.jcfg["enabled"]:
        journal = journal_stream.Journal(args.jcfg["message_max"], save_path=args.jcfg["save_path"],
                                         save_max_kb=args.jcfg["save_max_kb"])
        journal.start()
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))   # systemd stop: il finally chiude journalctl
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"hringr web on http://{args.host}:{args.port}/  (default audio_source: {args.defaults['audio_source']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if journal:
            journal.stop()                     # chiude journalctl


if __name__ == "__main__":
    main()
