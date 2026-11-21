# Copyright (C) 2026 Daniele "DLx" Lussana
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lettore del journal di systemd: `journalctl -f -o json` in sola lettura, messaggi solo in memoria.

Nessun comando passa da una shell, nessun valore dell'utente finisce nella riga di comando.
Il testo dei log non viene scritto da nessuna parte (né log dell'app, né URL), salvo `save_path` esplicito.
"""
import collections
import json
import os
import shutil
import subprocess
import threading
import time

MAX_LINE = 256 * 1024                  # righe JSON più lunghe vengono scartate (journalctl tronca già i campi > 4 KiB)
FIELDS = "MESSAGE,PRIORITY,_SYSTEMD_UNIT,_SYSTEMD_USER_UNIT,SYSLOG_IDENTIFIER,_COMM,_PID"
PRIO_NAMES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")


def _text(v):
    """MESSAGE può essere stringa, null (campo troppo lungo) o lista di byte (binario)."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return bytes(b & 255 for b in v if isinstance(b, int)).decode("utf-8", "replace")
    return ""


def parse(line, max_msg):
    """Una riga JSON del journal -> evento compatto, oppure None se inutilizzabile. Campi mancanti ammessi."""
    try:
        j = json.loads(line)
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    try:
        p = int(_text(j.get("PRIORITY")) or 6)
    except ValueError:
        p = 6
    lane = _text(j.get("_SYSTEMD_UNIT")) or _text(j.get("_SYSTEMD_USER_UNIT")) \
        or _text(j.get("SYSLOG_IDENTIFIER")) or _text(j.get("_COMM")) or "(unknown)"
    msg = " ".join(_text(j.get("MESSAGE")).split()) or "(messaggio vuoto o troppo lungo)"
    try:
        t = int(_text(j.get("__REALTIME_TIMESTAMP"))) / 1e6
    except ValueError:
        t = time.time()
    return {"t": t, "p": min(max(p, 0), 7), "u": lane[:80], "m": msg[:max_msg],
            "c": (_text(j.get("_COMM")) or _text(j.get("SYSLOG_IDENTIFIER")))[:40], "pid": _text(j.get("_PID"))[:12]}


class Journal:
    def __init__(self, max_msg=200, buffer=500, save_path="", save_max_kb=1024):
        self.max_msg, self.save_path, self.save_max = max_msg, save_path, save_max_kb * 1024
        self.buf = collections.deque(maxlen=buffer)     # i client lenti perdono i messaggi più vecchi
        self.seq, self.counts = 0, [0] * 8
        self.status = "starting"
        self.cond = threading.Condition()
        self.proc, self.stop_flag = None, threading.Event()

    # ---- produzione
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _set_status(self, s):
        with self.cond:
            self.status = s
            self.cond.notify_all()

    def _run(self):
        wait = 1
        while not self.stop_flag.is_set():
            if not shutil.which("journalctl"):
                self._set_status("journalctl not found: systemd is required")
                self.stop_flag.wait(30)
                continue
            err = []
            try:
                # -n 0: solo i messaggi nuovi. Argomenti fissi, nessuna shell.
                self.proc = subprocess.Popen(
                    ["journalctl", "-f", "-n", "0", "--no-pager", "-o", "json", "--output-fields=" + FIELDS],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            except OSError as e:
                self._set_status(f"cannot start journalctl: {e.strerror}")
                self.stop_flag.wait(30)
                continue
            threading.Thread(target=lambda p=self.proc: err.extend(p.stderr.read(4000).decode("utf-8", "replace").split("\n")),
                             daemon=True).start()
            self._set_status("running")
            t0 = time.monotonic()
            self._read(self.proc.stdout)
            self.proc.wait()
            if self.stop_flag.is_set():
                return
            hint = " ".join(x for x in err if x.strip())[:200]
            self._set_status("journal not readable (permissions? add the user to the systemd-journal or adm group): "
                             + hint if "permission" in hint.lower() or "insufficient" in hint.lower()
                             else f"journalctl exited, restarting ({hint})")
            wait = 1 if time.monotonic() - t0 > 60 else min(wait * 2, 30)   # backoff se muore subito
            self.stop_flag.wait(wait)

    def _read(self, f):
        while not self.stop_flag.is_set():
            line = f.readline(MAX_LINE)
            if not line:
                return
            if not line.endswith(b"\n"):                # riga enorme: la scarto fino a fine riga
                while (chunk := f.readline(MAX_LINE)) and not chunk.endswith(b"\n"):
                    pass
                continue
            ev = parse(line, self.max_msg)
            if ev:
                self._add(ev)

    def _add(self, ev):
        with self.cond:
            self.seq += 1
            ev["id"] = self.seq
            self.counts[ev["p"]] += 1
            self.buf.append(ev)
            self.cond.notify_all()
        if self.save_path:
            self._save(ev)

    def _save(self, ev):
        # shortcut: a save_max_kb il file viene azzerato (niente rotazione); usare una cartella tmpfs, es. /dev/shm
        try:
            if os.path.exists(self.save_path) and os.path.getsize(self.save_path) > self.save_max:
                open(self.save_path, "w").close()
            fd = os.open(self.save_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            self.save_path = ""                          # un disco pieno o un percorso errato non ferma la lettura

    # ---- consumo (un client SSE)
    def wait(self, last_id, timeout):
        """Eventi con id > last_id (attende fino a timeout). Ritorna (eventi, contatori, stato)."""
        with self.cond:
            self.cond.wait_for(lambda: self.seq > last_id, timeout)
            return [e for e in self.buf if e["id"] > last_id], list(self.counts), self.status
