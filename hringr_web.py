#!/usr/bin/env python3
# Copyright (C) 2026 Daniele "DLx" Lussana
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versione web di hringr.py: serve hringr.html e i segnali (CPU, RAM, rete, audio) via SSE.

Il disegno gira nel browser (WebGL2); qui girano solo le misure, che un browser non può leggere.
Riusa da hringr.py i thread dei segnali (stats, bridge cava/mpd-spectrum, loopback).

Uso:   ./run_web.sh            # poi apri http://<ip-del-pi>:8765/
URL:   /?audio_source=bridge|loopback|auto&shape=bubbles&inner=liquid&liquid_color=7B5CFF&demo=1
Tasti nella pagina: F schermo intero (o doppio clic), D demo, A cambia audio bridge<->loopback.
I valori predefiniti vengono da config.ini; i parametri dell'URL prevalgono.
"""
import argparse
import configparser
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import hringr  # solo funzioni dei segnali: pygame/moderngl si importano in hringr.main()

HERE = Path(__file__).resolve().parent
DEFAULTS = {"shape": "triangles", "inner": "pulse", "liquid_color": "0083C8",
            "tri_size": "1.0", "max_mbps": "200", "audio_source": "auto"}

stats, bridge, loop = hringr.Signals(), hringr.Signals(), hringr.Signals()
started, lock = set(), threading.Lock()
args = None


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

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self.send((HERE / "hringr.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/config.json":
            self.send(json.dumps(args.defaults).encode(), "application/json")
        elif u.path == "/events":
            self.events(parse_qs(u.query).get("audio_source", [args.defaults["audio_source"]])[0])
        else:
            self.send_error(404)

    def events(self, src):
        if src not in ("bridge", "loopback"):   # auto: come hringr.py, bridge se raggiungibile
            src = "bridge" if hringr.bridge_available(args.bridge_url) else "loopback"
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
                     "up_mbps": stats.up_mbps, "cpu": stats.cpu, "ram": stats.ram, "source": src}
                self.wfile.write(f"data: {json.dumps(m)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    global args
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.ini"))
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = raggiungibile dalla rete; 127.0.0.1 = solo locale")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bridge-url", default="http://127.0.0.1:44883/bars")
    ap.add_argument("--bridge-scale", type=float, default=22.0)
    ap.add_argument("--audio-device", default=None)
    ap.add_argument("--audio-gain", type=float, default=0.0)
    args = ap.parse_args()
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cp.read(args.config)
    args.defaults = {k: cp.get("hringr", k.replace("_", "-"), fallback=cp.get("hringr", k, fallback=v))
                     for k, v in DEFAULTS.items()}
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"hringr web su http://{args.host}:{args.port}/  (audio_source predefinito: {args.defaults['audio_source']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
