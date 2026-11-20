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
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import hringr  # solo funzioni dei segnali: pygame/moderngl si importano in hringr.main()

HERE = Path(__file__).resolve().parent
WEB_KEYS = ("shape", "inner", "liquid_color", "tri_size", "max_mbps", "audio_source")
BACKEND_KEYS = ("bridge_url", "bridge_scale", "audio_device", "audio_gain")
MAX_CLIENTS = 20                       # ogni scheda del browser = un thread che trasmette a 25 Hz

stats, bridge, loop = hringr.Signals(), hringr.Signals(), hringr.Signals()
started, lock = set(), threading.Lock()
clients = threading.BoundedSemaphore(MAX_CLIENTS)
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
            self.send(json.dumps({**args.defaults, "params": hringr.PARAMS}).encode(), "application/json")
        elif u.path == "/events":
            self.events(parse_qs(u.query).get("audio_source", [args.defaults["audio_source"]])[0])
        else:
            self.send_error(404)

    def events(self, src):
        if src not in ("bridge", "loopback"):   # auto: come hringr.py, bridge se raggiungibile
            src = "bridge" if hringr.bridge_available(args.bridge_url) else "loopback"
        if not clients.acquire(blocking=False):
            self.send_error(503, "troppi client collegati")
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
                     "up_mbps": stats.up_mbps, "cpu": stats.cpu, "ram": stats.ram, "source": src}
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
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = raggiungibile dalla rete; 127.0.0.1 = solo locale")
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
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"hringr web su http://{args.host}:{args.port}/  (audio_source predefinito: {args.defaults['audio_source']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
