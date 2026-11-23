#!/usr/bin/env python3
# Copyright (C) 2026 Daniele "DLx" Lussana
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cerchio che vibra con l'audio + triangoli guidati dalla rete + impulso guidato dalla CPU.

- Il perimetro di un cerchio vibra come attraverso l'acqua: più l'audio in uscita
  è forte, più l'onda è ampia e veloce.
- Triangoli chiari arrivano da fuori schermo verso il cerchio: dipende dal DOWNLOAD.
- Triangoli azzurri/blu partono dal cerchio verso l'esterno: dipende dall'UPLOAD.
- Dentro il cerchio (nero) partono impulsi dal centro verso il perimetro: più la CPU
  è carica, più sono frequenti e veloci; più la RAM è occupata, più sono luminosi.
  In alternativa (--inner liquid) un liquido colorato (--liquid-color) scorre nel cerchio
  come in una sfera: la CPU ne determina la velocità, la RAM la luminosità.

Pensato per Raspberry Pi 5 (OpenGL 3.1 / GLSL 1.40, funziona anche su PC).

Uso:
  python hringr.py --demo       # segnali simulati
  python hringr.py --fullscreen # dati reali
Configurazione: config.ini (stessa cartella) fornisce i valori predefiniti, es. shape = bubbles.
Keys: F full screen, D demo on/off, ESC or Q quit.
"""
import argparse
import configparser
import json
import math
import threading
import time
from pathlib import Path

import numpy as np


def _hex(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _positive(s):
    v = float(s)
    if not v > 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0 (got '{s}')")
    return v


def _color(s):
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError(
            f"invalid color '{s}': 6 hex digits (RRGGBB) needed; in config.ini write it without '#'")
    return _hex(s)

# Costanti condivise con la versione web (che le riceve da hringr_web.py in /config.json).
PARAMS = json.loads((Path(__file__).resolve().parent / "params.json").read_text())
LIGHT = [_hex(h) for h in PARAMS["light"]]            # triangoli in arrivo
BLUES = [_hex(h) for h in PARAMS["blues"]]            # triangoli in uscita
RING_CORE, RING_GLOW, RING_ECHO = (_hex(PARAMS[k]) for k in ("ring_core", "ring_glow", "ring_echo"))
RING_HALO, PULSE_COL = _hex(PARAMS["ring_halo"]), _hex(PARAMS["pulse_col"])

# Onde che deformano il perimetro: (lobi, ampiezza relativa, velocità base rad/s)
WAVES = [tuple(w) for w in PARAMS["waves"]]
WK = np.array([w[0] for w in WAVES], np.float32)
WA = np.array([w[1] for w in WAVES], np.float32)
WS = np.array([w[2] for w in WAVES], np.float32)

TAB_N = PARAMS["tab_n"]           # campioni angolari della tabella del perimetro
GRID = np.linspace(-np.pi, np.pi, TAB_N, endpoint=False).astype(np.float32)
MAX_PULSES = PARAMS["max_pulses"]

# Valori predefiniti, unica fonte per hringr.py (argparse) e hringr_web.py.
DEFAULTS = {"shape": "triangles", "inner": "pulse", "liquid_color": "0083C8", "audio_source": "auto",
            "bridge_url": "http://127.0.0.1:44883/bars", "bridge_scale": 22.0,
            "max_mbps": 200.0, "tri_size": 1.0}


# ----------------------------------------------------------------------------- segnali
class Signals:
    def __init__(self):
        self.audio = 0.0          # 0..1
        self.down_mbps = 0.0
        self.up_mbps = 0.0
        self.cpu = 0.0            # 0..1
        self.ram = 0.0            # 0..1
        self.swap = 0.0           # 0..1: attività di swap (glitch)
        self.swap_pps = 0.0       # pagine/s scambiate con lo swap (in + out), per la barra del journal
        self.audio_status = "audio: starting"


def start_audio_thread(sig, device=None, gain_db=0.0):
    """Audio in uscita (loopback). Su Raspberry Pi OS (PipeWire/PulseAudio) è nativo."""
    def run():
        try:
            import soundcard as sc
            if device:
                mics = sc.all_microphones(include_loopback=True)
                mic = next((m for m in mics if device.lower() in m.name.lower()), None)
                if mic is None:
                    raise RuntimeError(f"device '{device}' not found: "
                                       + ", ".join(m.name for m in mics))
            else:
                mic = sc.get_microphone(sc.default_speaker().name, include_loopback=True)
            sig.audio_status = f"audio: {mic.name}"
            with mic.recorder(samplerate=44100, blocksize=1024) as rec:
                while True:
                    x = rec.record(numframes=1024)
                    rms = float(np.sqrt(np.mean(np.square(x)))) + 1e-9
                    db = 20 * math.log10(rms) + gain_db
                    sig.audio = min(1.0, max(0.0, (db + 55.0) / 45.0))  # -55 dB..-10 dB
        except Exception as e:  # noqa: BLE001
            sig.audio_status = f"audio NOT available ({e})"
            sig.audio = 0.0
    threading.Thread(target=run, daemon=True).start()


def start_bridge_thread(sig, url, scale):
    """Livello audio dalle barre di cava già calcolate da mpd-spectrum.py (HTTP).

    Non apre né la scheda audio né il FIFO di MPD (un FIFO ha UN solo lettore utile:
    un secondo lettore si spartirebbe i byte con cava e rovinerebbe anche la pagina web).
    """
    import json
    import urllib.request

    def run():
        while True:
            try:
                with urllib.request.urlopen(url, timeout=0.5) as r:
                    d = json.load(r)
                bars = d.get("bars") or []
                if d.get("error") or not bars or (d.get("age_ms") or 0) > 1500:
                    sig.audio = 0.0
                    sig.audio_status = "audio: bridge active, no signal"
                else:
                    sig.audio = min(1.0, (sum(bars) / len(bars)) / scale)
                    sig.audio_status = "audio: mpd-spectrum (cava)"
            except Exception as e:  # noqa: BLE001
                sig.audio = 0.0
                sig.audio_status = f"audio: bridge unreachable ({e})"
            time.sleep(0.04)
    threading.Thread(target=run, daemon=True).start()


def bridge_available(url):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=0.5):
            return True
    except Exception:  # noqa: BLE001
        return False


def read_net(path="/proc/net/dev"):
    """(byte ricevuti, byte inviati) sommati su tutte le interfacce tranne il loopback."""
    rx = tx = 0
    for line in Path(path).read_text().splitlines()[2:]:
        name, _, data = line.partition(":")
        if name.strip().startswith("lo"):
            continue
        f = data.split()
        rx, tx = rx + int(f[0]), tx + int(f[8])
    return rx, tx


def read_uptime(path="/proc/uptime"):
    """Secondi dall'avvio del sistema, da /proc/uptime (Linux)."""
    return float(Path(path).read_text().split()[0])


def read_cpu(path="/proc/stat"):
    """(tempo totale, tempo inattivo) in tick, dalla riga 'cpu' aggregata."""
    f = [int(x) for x in Path(path).read_text().splitlines()[0].split()[1:]]
    return sum(f[:8]), f[3] + f[4]            # idle + iowait


def read_ram(path="/proc/meminfo"):
    """Frazione di RAM in uso 0..1 (come psutil: 1 - disponibile/totale)."""
    m = {}
    for line in Path(path).read_text().splitlines():
        k, _, v = line.partition(":")
        m[k] = int(v.split()[0])
    return 1.0 - m["MemAvailable"] / m["MemTotal"]


def read_swap(path="/proc/vmstat"):
    """Pagine scambiate con lo swap (in + out) dall'avvio, da /proc/vmstat."""
    v = dict(line.split() for line in Path(path).read_text().splitlines())
    return int(v["pswpin"]) + int(v["pswpout"])


def swap_norm(pps):
    """Attività di swap 0..1 (log): 2000 pagine/s (~8 MB/s) = massimo."""
    return min(1.0, math.log1p(max(pps, 0.0)) / math.log1p(2000.0))


def start_stats_thread(sig):
    """Rete (download/upload), CPU, RAM e swap, campionati 5 volte al secondo (da /proc: Linux)."""
    def run():
        rx0, tx0 = read_net()
        c0 = read_cpu()
        sw0 = read_swap()
        t0 = time.time()
        while True:
            time.sleep(0.2)
            rx1, tx1 = read_net()
            c1 = read_cpu()
            sw1 = read_swap()
            t1 = time.time()
            dt = t1 - t0
            pps = max(0, sw1 - sw0) / dt
            sig.swap_pps += (pps - sig.swap_pps) * 0.5
            sig.swap = swap_norm(sig.swap_pps)
            d = max(0, rx1 - rx0) * 8 / 1e6 / dt
            u = max(0, tx1 - tx0) * 8 / 1e6 / dt
            sig.down_mbps += (d - sig.down_mbps) * 0.5
            sig.up_mbps += (u - sig.up_mbps) * 0.5
            dtot = c1[0] - c0[0]
            sig.cpu = min(1.0, max(0.0, 1.0 - (c1[1] - c0[1]) / dtot)) if dtot > 0 else sig.cpu
            sig.ram = read_ram()
            rx0, tx0, c0, t0, sw0 = rx1, tx1, c1, t1, sw1
    threading.Thread(target=run, daemon=True).start()


def demo_signals(sig, t):
    burst = max(0.0, math.sin(t * 0.7)) ** 2
    sig.audio = min(1.0, 0.08 + 0.9 * burst * (0.6 + 0.4 * math.sin(t * 9) ** 2))
    sig.down_mbps = 200 * max(0.0, math.sin(t * 0.37 + 1.0)) ** 3
    sig.up_mbps = 80 * max(0.0, math.sin(t * 0.53 + 3.0)) ** 3
    sig.cpu = 0.5 + 0.5 * math.sin(t * 0.3)
    sig.ram = 0.55 + 0.2 * math.sin(t * 0.1)
    sig.swap = max(0.0, math.sin(t * 0.23 + 2.0)) ** 4 * 0.9
    sig.swap_pps = sig.swap * 600


def net_norm(mbps, max_mbps):
    """Scala logaritmica 0..1."""
    return min(1.0, math.log1p(max(mbps, 0.0)) / math.log1p(max_mbps))


# ----------------------------------------------------------------------------- scena
class Scene:
    MAX_TRI = PARAMS["max_tri"]

    def __init__(self, seed=None, size_mul=1.0, shape="triangles", inner="pulse", liquid_color=None):
        self.rng = np.random.default_rng(seed)
        self.inner = inner                # effetto dentro il cerchio: pulse | liquid
        self.liq = liquid_color or _hex("0083C8")
        self.flow = 0.0                   # fase del liquido: avanza più in fretta con la CPU
        self.phase = np.zeros(len(WAVES), np.float32)
        self.level = 0.0
        self.pin = 0.0                    # CPU smussata: velocità dell'effetto interno
        self.bri = 0.0                    # RAM smussata (40%..76% -> 0..1): luminosità dell'effetto interno
        self.glitch = 0.0                 # attività di swap smussata: intensità del glitch sull'anello
        self.pulses = []                  # [età normalizzata 0..1, forza, durata s]
        self.pulse_timer = 0.0
        self.flash = 0.0                  # lampo del perimetro quando arriva un impulso
        self.tab = np.zeros((2, TAB_N), np.float32)   # riga 0: perimetro, riga 1: eco
        self.pos = np.zeros((0, 2), np.float32)
        self.dir = np.zeros((0, 2), np.float32)
        self.spd = np.zeros(0, np.float32)
        self.size = np.zeros(0, np.float32)
        self.col = np.zeros((0, 3), np.float32)
        self.kind = np.zeros(0, np.int8)
        self.size_mul = size_mul
        self.shape = shape                # forma di ciò che arriva verso il cerchio: triangles | bubbles
        self.ph = np.zeros(0, np.float32)  # fase dell'ondeggiamento (bolle)
        self.time = 0.0
        self.r0 = 1.0

    def amp(self):
        return 0.006 + 0.11 * self.level ** 1.2

    def _build_table(self):
        a = self.amp()
        main = np.zeros(TAB_N, np.float32)
        echo = np.zeros(TAB_N, np.float32)
        for i, (k, am, ph) in enumerate(zip(WK, WA, self.phase)):
            main += am * np.sin(k * GRID + ph)
            echo += am * np.sin(k * GRID + ph + 0.6 * (i + 1))
        self.tab[0] = self.r0 * (1.0 + a * main)
        self.tab[1] = self.r0 * (1.0 + 0.02 + 0.07 * self.level + 0.8 * a * echo)

    def radius(self, theta):
        return np.interp(theta, GRID, self.tab[0], period=2 * np.pi)

    def _add(self, pos, dr, spd, size, col, kind):
        room = self.MAX_TRI - len(self.pos)
        n = min(len(pos), room)
        if n <= 0:
            return
        self.pos = np.vstack([self.pos, pos[:n]])
        self.dir = np.vstack([self.dir, dr[:n]])
        self.spd = np.concatenate([self.spd, spd[:n]])
        self.size = np.concatenate([self.size, size[:n]])
        self.col = np.vstack([self.col, col[:n]])
        self.kind = np.concatenate([self.kind, np.full(n, kind, np.int8)])
        self.ph = np.concatenate([self.ph, self.rng.uniform(0, 2 * np.pi, n).astype(np.float32)])

    def update(self, dt, audio, down, up, cpu, ram, w, h, swap=0.0):
        """audio, down, up, cpu, ram, swap in 0..1."""
        rng = self.rng
        self.time += dt
        self.glitch += (swap - self.glitch) * (1 - math.exp(-dt / (0.05 if swap > self.glitch else 0.5)))
        k = 1 - math.exp(-dt / (0.03 if audio > self.level else 0.25))
        self.level += (audio - self.level) * k
        self.phase += WS * (1.0 + 5.0 * self.level) * dt
        self.r0 = 0.30 * min(w, h)
        self._build_table()
        half_diag = math.hypot(w, h) / 2 + 40
        sc = min(w, h) / 800 * self.size_mul

        # --- impulsi interni (CPU/RAM)
        k = 1 - math.exp(-dt / 0.3)
        self.pin += (cpu - self.pin) * k
        self.bri += (min(1.0, max(0.0, (ram - 0.4) / 0.36)) - self.bri) * k
        self.flow += dt * (0.15 + 3.0 * self.pin * self.pin)
        self.pulse_timer -= dt
        if self.inner == "pulse" and self.pulse_timer <= 0 and len(self.pulses) <= MAX_PULSES - 2:
            # battito "lub-dub": un impulso forte e, poco dopo, uno più debole; poi pausa
            st, du = 0.3 + 0.7 * self.bri, 1.1 - 0.4 * self.pin
            self.pulses.append([0.0, st, du])
            self.pulses.append([-0.22 / (du * 0.9), 0.6 * st, du * 0.9])   # età negativa = ritardo
            self.pulse_timer = 1.5 - 0.9 * self.pin                       # 40..100 bpm
        for p in self.pulses:
            p[0] += dt / p[2]
            if p[0] >= 1.0:
                self.flash = max(self.flash, p[1])
        self.pulses = [p for p in self.pulses if p[0] < 1.0]
        self.flash *= math.exp(-dt / 0.12)

        # --- triangoli in arrivo (download): piccoli e numerosi
        n_in = rng.poisson((3 + 3000 * down ** 1.3) * dt)
        if n_in:
            a = rng.uniform(0, 2 * np.pi, n_in)
            p = np.stack([np.cos(a), np.sin(a)], 1) * (half_diag + rng.uniform(0, 60, n_in))[:, None]
            ta = a + rng.normal(0, 0.35, n_in)
            tgt = np.stack([np.cos(ta), np.sin(ta)], 1) * self.radius(ta)[:, None]
            d = tgt - p
            d /= np.linalg.norm(d, axis=1, keepdims=True)
            spd = (250 + 2200 * down) * rng.uniform(0.75, 1.25, n_in)
            col = np.array(LIGHT, np.float32)[rng.integers(0, len(LIGHT), n_in)]
            self._add(p.astype(np.float32), d.astype(np.float32), spd.astype(np.float32),
                      (rng.uniform(3, 11, n_in) * sc).astype(np.float32), col, 0)

        # --- triangoli in uscita (upload)
        n_out = rng.poisson((3 + 2000 * up ** 1.3) * dt)
        if n_out:
            a = rng.uniform(0, 2 * np.pi, n_out)
            p = np.stack([np.cos(a), np.sin(a)], 1) * self.radius(a)[:, None]
            b = a + rng.normal(0, 0.12, n_out)
            d = np.stack([np.cos(b), np.sin(b)], 1)
            spd = (150 + 1600 * up) * rng.uniform(0.75, 1.25, n_out)
            col = np.array(BLUES, np.float32)[rng.integers(0, len(BLUES), n_out)]
            self._add(p.astype(np.float32), d.astype(np.float32), spd.astype(np.float32),
                      (rng.uniform(3, 11, n_out) * sc).astype(np.float32), col, 1)

        # --- movimento e rimozione
        # velocità: in arrivo accelerano avvicinandosi all'anello; in uscita partono piano e accelerano
        u = (np.hypot(self.pos[:, 0], self.pos[:, 1]) - self.radius(np.arctan2(self.pos[:, 1], self.pos[:, 0]))) / self.r0
        m = np.where(self.kind == 0, 1 + 2.2 * np.exp(-u / 0.25), 0.3 + 1.7 * (1 - np.exp(-np.maximum(u, 0) / 0.2)))
        self.pos += self.dir * (self.spd * m * dt)[:, None]
        r = np.hypot(self.pos[:, 0], self.pos[:, 1])
        th = np.arctan2(self.pos[:, 1], self.pos[:, 0])
        ring = self.radius(th)
        dead = ((self.kind == 0) & (r <= ring)) | ((self.kind == 1) & (r > half_diag + 40))
        if dead.any():
            keep = ~dead
            for name in ("pos", "dir", "spd", "size", "col", "kind", "ph"):
                setattr(self, name, getattr(self, name)[keep])
            r, ring = r[keep], ring[keep]
        self._r, self._ring, self._sc = r, ring, sc

    def vertices(self):
        """Triangoli: array (N*3, 6) float32: x, y, r, g, b, a.
        Con shape=bubbles restano triangoli solo quelli in uscita."""
        m = (self.kind == 1) if self.shape == "bubbles" else np.ones(len(self.pos), bool)
        n = int(m.sum())
        if n == 0:
            return np.zeros((0, 6), np.float32)
        pos, d, spd, size = self.pos[m], self.dir[m], self.spd[m], self.size[m]
        col, kind = self.col[m], self.kind[m]
        perp = np.stack([-d[:, 1], d[:, 0]], 1)
        # nuoto a serpentina (solo visivo): scarto laterale sinusoidale e muso che segue la curva
        ph = self.ph[m]
        w_osc = 3.5 + 0.5 * np.sin(ph * 3.1)                        # rad/s, leggermente diverso per ciascuno
        amp = size * 1.6 + 2.0
        arg = self.time * w_osc + ph
        pos = pos + perp * (amp * np.sin(arg))[:, None]
        ang = np.arctan2(amp * w_osc * np.cos(arg), np.maximum(spd, 60.0)) * 0.9
        ca, sa = np.cos(ang)[:, None], np.sin(ang)[:, None]
        d = d * ca + perp * sa
        perp = np.stack([-d[:, 1], d[:, 0]], 1)
        L = (size * 0.87 * (1 + spd / 1500))[:, None]   # si allungano con la velocità
        W = size[:, None]
        tip = pos + d * L * 0.6
        b1 = pos - d * L * 0.4 + perp * W / 2
        b2 = pos - d * L * 0.4 - perp * W / 2
        dist = ((self._r - self._ring) / max(self._sc, 0.1))[m]
        alpha = np.where(kind == 1, np.clip(dist / 40, 0, 1), np.clip(dist / 25 + 0.25, 0.25, 1))
        v = np.empty((n, 3, 6), np.float32)
        for i, pt in enumerate((tip, b1, b2)):
            v[:, i, 0:2] = pt
            v[:, i, 2:5] = col
            v[:, i, 5] = alpha
        return v.reshape(-1, 6)

    def bubble_vertices(self):
        """Bolle in arrivo: 2 triangoli (un quadrato) ciascuna.
        Array (N*6, 9) float32: x, y, u, v, r, g, b, a, raggio_px."""
        m = self.kind == 0
        n = int(m.sum()) if self.shape == "bubbles" else 0
        if n == 0:
            return np.zeros((0, 9), np.float32)
        pos, d, size, col, ph = self.pos[m], self.dir[m], self.size[m], self.col[m], self.ph[m]
        perp = np.stack([-d[:, 1], d[:, 0]], 1)
        rad = np.maximum(size * 0.7 + 1.0, 2.2 * self._sc)                 # raggio in px
        wob = np.sin(self.time * 4.0 + ph) * size * 0.5                     # ondeggiamento laterale (solo visivo)
        c = pos + perp * wob[:, None]
        dist = ((self._r - self._ring) / max(self._sc, 0.1))[m]
        alpha = np.clip(dist / 25 + 0.25, 0.25, 1)
        mg = (rad + 1.5) / rad                                              # margine per i bordi sfumati
        corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, -1], [1, 1], [-1, 1]], np.float32)
        uv = corners[None] * mg[:, None, None]
        v = np.empty((n, 6, 9), np.float32)
        v[:, :, 0:2] = c[:, None, :] + uv * rad[:, None, None]
        v[:, :, 2:4] = uv
        v[:, :, 4:7] = col[:, None, :]
        v[:, :, 7] = alpha[:, None]
        v[:, :, 8] = rad[:, None]
        return v.reshape(-1, 9)

    def pulse_uniform(self):
        """MAX_PULSES coppie (raggio normalizzato, forza); forza 0 = inattivo."""
        out = np.zeros((MAX_PULSES, 2), np.float32)
        for i, (age, s, _) in enumerate(p for p in self.pulses if p[0] >= 0):
            out[i] = (age ** 0.8, s * (1.0 - 0.6 * age))
        return out


# ----------------------------------------------------------------------------- OpenGL (GLSL 1.40 = OpenGL 3.1)
RING_VS = """
#version 140
uniform vec2 u_res;
uniform float u_half;
in vec2 in_pos;
void main(){ gl_Position = vec4(in_pos * u_half / (0.5 * u_res), 0.0, 1.0); }
"""
RING_FS = """
#version 140
uniform vec2  u_res;
uniform float u_level, u_flash, u_sc;
uniform sampler2D u_tab;
uniform vec3  u_core, u_glow, u_halo, u_echo, u_pcol, u_liq;
uniform vec2  u_pulse[6];
uniform float u_liquid, u_flow, u_energy, u_r0;
uniform float u_glitch, u_time;
out vec4 frag;
const int N = 512;
const float PI = 3.14159265;

float tab(float u, int row){
    int i0 = int(floor(u));
    float f = u - float(i0);
    i0 = i0 % N;
    int i1 = (i0 + 1) % N;
    return mix(texelFetch(u_tab, ivec2(i0, row), 0).r,
               texelFetch(u_tab, ivec2(i1, row), 0).r, f);
}
vec3 shade(vec2 p){
    float r = length(p);
    float u = (atan(p.y, p.x) + PI) / (2.0 * PI) * float(N);
    float R = tab(u, 0);
    float d = r - R;

    float w = (1.8 + 2.6 * u_level) * u_sc;
    float core = 1.0 - smoothstep(w - 1.0, w + 1.0, abs(d));
    // alone: vicino (intenso) + lontano (ampio) fuori, strettissimo dentro (l'interno resta nero)
    float near = 0.0, far = 0.0;
    if (d > 0.0) {
        near = 0.80 * exp(-d / ((14.0 + 40.0 * u_level) * u_sc));
        far  = 0.42 * exp(-d / ((60.0 + 120.0 * u_level) * u_sc));
    } else {
        near = 0.35 * exp(d / (3.0 * u_sc));
    }
    // eco
    float echo = (1.0 - smoothstep(0.3, 1.6, abs(r - tab(u, 1)))) * (0.25 + 0.4 * u_level);

    // impulsi interni: anelli che seguono la forma del perimetro (rho = r / R)
    float pul = 0.0;
    vec3 liq = vec3(0.0);
    if (d < 0.0 && u_liquid < 0.5) {
        float rho = r / R;
        for (int i = 0; i < 6; i++) {
            float x = (rho - u_pulse[i].x) / 0.09;
            pul += u_pulse[i].y * exp(-x * x);
        }
        pul *= 0.55;
    }
    // liquido nel contenitore (stile "palla" degli assistenti vocali): campo sinusoidale deformato
    // da se stesso (domain warping) = correnti che si avvolgono; la sfera ombreggiata lo fa sembrare un volume.
    if (d < 0.0 && u_liquid > 0.5) {
        vec2 wv = p / u_r0;
        for (int i = 0; i < 3; i++)
            wv += 0.5 * vec2(sin(wv.y * 2.2 + u_flow + float(i) * 1.7),
                             cos(wv.x * 2.0 - 0.8 * u_flow + float(i) * 2.3));
        float v = 0.6 * (0.5 + 0.5 * sin(3.0 * wv.x + 2.0 * wv.y + 0.7 * u_flow))
                + 0.4 * (0.5 + 0.5 * sin(4.0 * length(wv) - 1.3 * u_flow));
        v = smoothstep(0.15, 0.95, v);
        float z = sqrt(max(0.0, 1.0 - (r / R) * (r / R)));            // centro in rilievo, bordo scuro
        vec3 n = normalize(vec3(p / R, z));
        float spec = pow(max(dot(n, normalize(vec3(-0.45, 0.55, 0.7))), 0.0), 28.0);
        float k = 0.10 + 0.90 * u_energy;                              // CPU: più carica = più luminoso
        liq = mix(u_liq * 0.22, u_liq * 1.25, v) * (0.35 + 0.65 * z) * k
            + mix(vec3(1.0), u_liq, 0.35) * spec * (0.10 + 0.70 * u_energy);
    }
    float f = 1.0 + 1.2 * u_flash;
    float fg = 1.0 + 0.35 * u_flash;   // l'alone reagisce al lampo meno del bordo (evita il ciano saturo)
    vec3 col = core * f * mix(u_core, vec3(0.93, 0.93, 0.94), min(1.0, u_level + 0.5 * u_flash))
             + near * fg * u_glow + far * fg * u_halo + echo * u_echo + pul * u_pcol + liq;
    return col;
}
float hash(vec2 q){ return fract(sin(dot(q, vec2(127.1, 311.7))) * 43758.5453); }
void main(){
    // glitch da swap: righe spezzate e sfarfallio nella zona dell'anello (dentro 1.7 r0), più fitti con u_glitch
    vec2 p = gl_FragCoord.xy - 0.5 * u_res;
    float g = u_glitch, zone = 1.0 - smoothstep(1.2 * u_r0, 1.7 * u_r0, length(p));
    float slot = floor(u_time * 12.0), band = floor(gl_FragCoord.y / 14.0);
    float cut = step(1.0 - 0.7 * g, hash(vec2(band, slot)));
    p.x += zone * cut * (hash(vec2(slot, band)) - 0.5) * 220.0 * u_sc * g;   // fette più larghe
    // separazione dei canali: rosso spostato a destra, blu a sinistra (un solo campionamento se il glitch è debole)
    float sep = zone * g * 5.0 * u_sc;
    vec3 c = g > 0.01 ? vec3(shade(p + vec2(sep, 0.0)).r, shade(p).g, shade(p - vec2(sep, 0.0)).b) : shade(p);
    c *= 1.0 + zone * g * 0.25 * (hash(vec2(slot, 7.0)) - 0.5);
    // linee orizzontali intere invece di puntini: righe che cambiano a scatti
    float streak = zone * g * step(1.0 - 0.18 * g, hash(vec2(floor(gl_FragCoord.y / 2.0), floor(u_time * 18.0))));
    c = min(c + streak * vec3(0.5, 0.9, 1.0) * 0.6, vec3(1.0));
    frag = vec4(c, max(max(c.r, c.g), c.b));   // premoltiplicato: identico sul nero, trasparente sopra il journal
}
"""

TRI_VS = """
#version 140
uniform vec2 u_res;
in vec2 in_pos;
in vec4 in_col;
out vec4 v_col;
out vec3 v_bary;
void main(){
    gl_Position = vec4(in_pos / (0.5 * u_res), 0.0, 1.0);
    v_col = in_col;
    int k = gl_VertexID % 3;                      // coordinate baricentriche del vertice
    v_bary = vec3(float(k == 0), float(k == 1), float(k == 2));
}
"""
# Bordi sfumati: l'opacità sale da 0 al bordo a 1 dentro il triangolo, su circa u_feather pixel.
TRI_FS = """
#version 140
uniform float u_feather;
in vec4 v_col;
in vec3 v_bary;
out vec4 frag;
void main(){
    float m  = min(min(v_bary.x, v_bary.y), v_bary.z);   // 0 sul bordo, 1/3 al centro
    float fw = max(fwidth(m), 1e-6);
    float dist_px = m / fw;                               // distanza dal bordo in pixel
    float F = min(u_feather, 0.6 * (1.0 / 3.0) / fw);     // i triangoli minuscoli restano pieni al centro
    float a = smoothstep(0.0, 1.0, clamp((dist_px + 0.35) / F, 0.0, 1.0));
    frag = vec4(v_col.rgb, v_col.a * a);
}
"""


BUBBLE_VS = """
#version 140
uniform vec2 u_res;
in vec2 in_pos;
in vec2 in_uv;
in vec4 in_col;
in float in_rad;
out vec2 v_uv;
out vec4 v_col;
out float v_rad;
void main(){
    gl_Position = vec4(in_pos / (0.5 * u_res), 0.0, 1.0);
    v_uv = in_uv; v_col = in_col; v_rad = in_rad;
}
"""
# Bolla subacquea: disco quasi trasparente, bordo più marcato, riflesso in alto a sinistra.
BUBBLE_FS = """
#version 140
uniform float u_feather;
in vec2 v_uv;
in vec4 v_col;
in float v_rad;
out vec4 frag;
void main(){
    float r  = length(v_uv);
    float fe = min(0.9, u_feather / v_rad);
    float edge = 1.0 - smoothstep(1.0 - fe, 1.0, r);
    float rim  = smoothstep(0.45, 0.97, r);
    float hl   = 1.0 - smoothstep(0.0, 0.34, length(v_uv - vec2(-0.36, 0.38)));
    vec3  rgb  = v_col.rgb * (0.40 + 0.60 * rim) + vec3(1.0) * hl * 0.85;
    float a    = edge * v_col.a * clamp(0.14 + 0.62 * rim + 0.85 * hl, 0.0, 1.0);
    frag = vec4(rgb, a);
}
"""


# ----------------------------------------------------------------------------- journal (desktop)
# Stesso journal della versione web (journal_stream.Journal, in sola lettura): bolle e barra dei dati disegnate
# su una superficie 2D, caricata come texture sotto l'anello. Stessi tasti e stessa sintassi del filtro.
OVERLAY_VS = """
#version 140
in vec2 in_pos;
out vec2 v_uv;
void main(){ gl_Position = vec4(in_pos, 0.0, 1.0); v_uv = in_pos * 0.5 + 0.5; }
"""
OVERLAY_FS = """
#version 140
uniform sampler2D u_img;
in vec2 v_uv;
out vec4 frag;
void main(){ vec4 c = texture(u_img, v_uv); frag = vec4(c.rgb * c.a, c.a); }
"""
PR_MAP = {"emerg": 0, "emergency": 0, "alert": 1, "crit": 2, "critical": 2, "err": 3, "error": 3,
          "warn": 4, "warning": 4, "notice": 5, "info": 6, "debug": 7}
PRIO_LABEL = ["EMERG", "ALERT", "CRIT", "ERR", "WARN", "NOTICE", "INFO", "DEBUG"]
FONT_DIR = Path(__file__).resolve().parent / "fonts"


def match_query(e, q):
    """query: parole (in unità e messaggio), unit:nome, level:err (= err o più grave), -termine per escludere."""
    for t in q.strip().lower().split():
        neg = t.startswith("-") and len(t) > 1
        body = t[1:] if neg else t
        i = body.find(":")
        k, v = (body[:i], body[i + 1:]) if i > 0 else ("", "")
        if k in ("unit", "u"):
            ok = v in e["u"].lower()
        elif k in ("level", "prio", "p"):
            ok = v in PR_MAP and e["p"] <= PR_MAP[v]
        else:
            ok = body in (e["u"] + " " + e["m"]).lower()
        if ok == neg:
            return False
    return True


class JournalPanel:
    """Journal, barra dei dati e pulsanti. journal=None: solo il pulsante info (journal disabilitato)."""

    def __init__(self, cfg, journal):
        import pygame
        import socket
        self.pg, self.cfg, self.j = pygame, cfg, journal
        self.ev, self.last, self.status = [], (journal.seq if journal else 0), "starting"
        self.muted = self.filter_on = self.focus = self.err_only = self.info = False
        self.kick, self.t_prev = 0.0, time.time()     # impulso di glitch dagli errori del log, decade in ~0.4 s
        self.query, self.rects, self.fonts = "", {}, {}
        self.host = socket.gethostname().upper()
        self.col = ([tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) for c in cfg["colors"]]
                    if journal else [])

    def font(self, mono, size):
        key = (mono, size)
        if key not in self.fonts:
            f = FONT_DIR / ("ShareTechMono-Regular.ttf" if mono else "ChakraPetch-SemiBold.ttf")
            self.fonts[key] = self.pg.font.Font(str(f) if f.exists() else None, size)
        return self.fonts[key]

    def event(self, e):
        """True se l'evento è del journal (non va ai tasti dell'anello)."""
        pg = self.pg
        if e.type == pg.MOUSEBUTTONDOWN and e.button == 1:
            for k, r in self.rects.items():
                if r.collidepoint(e.pos):
                    if k == "mute":
                        self.muted = not self.muted
                    elif k == "err":
                        self.err_only = not self.err_only
                    elif k == "info":
                        self.info = not self.info
                    elif k == "filter":
                        self.filter_on = not self.filter_on
                    else:
                        self.set_focus(True)
                    return True
            self.set_focus(False)
            return False
        if self.focus:
            if e.type == pg.TEXTINPUT:
                self.query += e.text
            elif e.type == pg.KEYDOWN:
                if e.key == pg.K_BACKSPACE:
                    self.query = self.query[:-1]
                elif e.key in (pg.K_ESCAPE, pg.K_RETURN, pg.K_KP_ENTER):
                    self.set_focus(False)
            return True
        return False

    def set_focus(self, on):
        if on != self.focus:
            self.focus = on
            (self.pg.key.start_text_input if on else self.pg.key.stop_text_input)()

    def _button(self, surf, x, y, glyph, active, color):
        pg = self.pg
        r = pg.Rect(x, y, 26, 26)
        self.rects[glyph[0]] = r
        pg.draw.rect(surf, (6, 8, 12, 217), r)
        pg.draw.rect(surf, color if active else (74, 90, 106), r, 1)
        if glyph[1] == "x":                                     # silenzia: croce
            m = 8
            for a, b in (((r.x + m, r.y + m), (r.right - m, r.bottom - m)),
                         ((r.right - m, r.y + m), (r.x + m, r.bottom - m))):
                pg.draw.line(surf, color if active else (180, 194, 209), a, b, 2)
        else:
            img = self.font(False, 15).render(glyph[1], True, color if active else (180, 194, 209))
            surf.blit(img, img.get_rect(center=r.center))

    def _info_layer(self, W, H, s):
        """Riquadro info sopra l'anello, nella parte bassa: hostname, uptime, modalità."""
        pg = self.pg
        surf = pg.Surface((W, H), pg.SRCALPHA)
        scale = self.cfg["font_scale"] if self.j else 1.0
        fs = max(12, round(min(H * 0.016, W / 30) * scale))
        r0 = 0.30 * min(W, H)
        mode = "journal disabled" if not self.j else ("journal off" if self.muted else "journal on")
        up = read_uptime()
        d, rem = divmod(int(up), 86400)
        hh, rem = divmod(rem, 3600)
        mm, ss = divmod(rem, 60)
        upt = (f"{d}d " if d else "") + f"{hh:02d}:{mm:02d}:{ss:02d}"
        f_host, f_small = self.font(False, round(fs * 3.2)), self.font(True, round(fs * 1.4))
        lines = [(f_host, self.host, (232, 241, 251)), (f_small, ("uptime " + upt).upper(), (180, 194, 209)),
                 (f_small, mode.upper(), (141, 160, 179))]
        pad = round(fs * 0.6)
        w = min(round(r0 * 1.5), W - 24)
        h = pad * 2 + sum(f.get_linesize() for f, _, _ in lines)
        x, y = (W - w) // 2, round(H / 2 + r0 * 0.3)
        c = round(fs / 0.82 * 1.1)                                # stessi angoli tagliati del dettaglio journal
        pts = [(x + c, y), (x + w, y), (x + w, y + h - c), (x + w - c, y + h), (x, y + h), (x, y + c)]
        pg.draw.polygon(surf, (141, 160, 179, 60), pts, 3)        # alone, come il dettaglio del journal
        pg.draw.polygon(surf, (6, 8, 12, 128), pts)               # riquadro semitrasparente, testo pieno
        pg.draw.polygon(surf, (141, 160, 179, 230), pts, 1)
        yy = y + pad
        for f, text, colr in lines:
            while len(text) > 1 and f.size(text)[0] > w - 2 * pad:
                text = text[:-2] + "…"
            img = f.render(text, True, colr)
            surf.blit(img, img.get_rect(midtop=(x + w // 2, yy)))
            yy += f.get_linesize()
        return pg.image.tostring(surf, "RGBA", True)

    def draw(self, W, H, s):
        """(strato sotto l'anello, strato sopra l'anello): RGBA dal basso in alto, oppure None."""
        pg, cfg = self.pg, self.cfg
        surf = pg.Surface((W, H), pg.SRCALPHA)
        now = time.time()
        self.rects = {}
        self.kick *= math.exp(-(now - self.t_prev) / 0.4)
        self.t_prev = now
        if self.j:                                          # i messaggi arrivano anche se silenziati (come nel web)
            ev, _, self.status = self.j.wait(self.last, 0)
            for e in ev:
                e["born"] = now
                if e["p"] <= 3:
                    self.kick = min(1.0, self.kick + 0.8)
                self.last = e["id"]
                self.ev.append(e)
            del self.ev[:-cfg["max_events"]]
            self.ev = [e for e in self.ev if now - e["born"] < cfg["window_s"]]
        # pulsanti in alto a destra: silenzia, errori, info (info è sempre presente)
        n = 2 if self.j else 0
        if self.j:
            self._button(surf, W - 38, 12, ("mute", "x"), self.muted, (255, 42, 61))
            self._button(surf, W - 72, 12, ("err", "!"), self.err_only, (255, 214, 10))
        self._button(surf, W - 38 - 34 * n, 12, ("info", "i"), self.info, (141, 160, 179))
        top = self._info_layer(W, H, s) if self.info else None
        if not self.j or self.muted:
            return pg.image.tostring(surf, "RGBA", True), top

        # barra in basso: dati su cui si basa hringr, poi filtro (come jbar nella versione web)
        sc = min(W, H) / 800
        bar_h = max(30, round(40 * sc))
        y0 = H - bar_h
        pg.draw.rect(surf, (6, 8, 12, 235), (0, y0, W, bar_h))
        pg.draw.line(surf, (28, 42, 56), (0, y0), (W, y0))
        fb = self.font(True, max(11, round(bar_h * 0.36)))
        text = (f"CPU {round(s.cpu * 100):3d}%  RAM {round(s.ram * 100):3d}%  DOWN {s.down_mbps:6.1f}  "
                f"UP {s.up_mbps:6.1f} Mbit/s  SWAP {s.swap_pps:4.0f} p/s")
        qw = min(round(420 * sc), W * 2 // 5)
        qx = W - 12 - qw
        fw = fb.size("filter")[0]
        fx = qx - 12 - 20 - fw                       # casella del filtro, poi la scritta "filter"
        ty = y0 + (bar_h - fb.get_height()) // 2
        stat = text
        while stat and fb.size(stat)[0] > fx - 24:
            stat = stat[:-1]
        surf.blit(fb.render(stat, True, (141, 160, 179)), (12, ty))
        box = pg.Rect(fx, y0 + bar_h // 2 - 7, 14, 14)
        self.rects["filter"] = pg.Rect(fx - 4, y0, fw + 48, bar_h)
        pg.draw.rect(surf, (180, 194, 209), box, 1)
        if self.filter_on:
            pg.draw.rect(surf, (180, 194, 209), box.inflate(-6, -6))
        surf.blit(fb.render("filter", True, (180, 194, 209)), (fx + 20, ty))
        qbox = pg.Rect(qx, y0 + 6, qw, bar_h - 12)
        self.rects["query"] = qbox
        pg.draw.rect(surf, (11, 17, 24), qbox)
        pg.draw.rect(surf, (28, 42, 56), qbox, 1)
        q = self.query
        while q and fb.size(q)[0] > qw - 14:
            q = q[1:]
        caret = "_" if self.focus and int(now * 2) % 2 == 0 else ""
        surf.blit(fb.render(q + caret, True, (220, 230, 242)), (qx + 7, ty))
        if not self.query and not self.focus:
            surf.blit(fb.render("search: level:err unit:nginx -word", True, (74, 90, 106)), (qx + 7, ty))

        # bolle in alto: 1 colonna su telefono, fino a 3 su monitor larghi
        f_size = max(12, round(min(H * 0.016, W / 30) * cfg["font_scale"]))
        f = f_size / 0.82
        pad, gap, c = f, f * 0.6, f * 1.1
        cols = max(1, min(3, int((W - 2 * pad + gap) // (f * 24))))
        bw = (W - 2 * pad - (cols - 1) * gap) / cols
        bh = f * 3.4
        limit = round(H * cfg["height"])
        ev = self.ev
        if self.err_only:
            ev = [e for e in ev if e["p"] <= 4]     # solo emerg..warning, niente log "azzurri"
        ev = [e for e in ev if not self.filter_on or not self.query.strip() or match_query(e, self.query)]
        by, col = pad * 2 + f, 0                 # sotto i pulsanti in alto a destra (come nel web)
        for e in reversed(ev):
            if by + bh > limit:
                break
            age = now - e["born"]
            life = 1 - age / (cfg["window_s"] * 0.7)
            if life < 0.12:
                continue
            bx = pad + col * (bw + gap)
            alpha = cfg["opacity"] * min(1, age * 4) * min(1, life * 2)
            cc = self.col[e["p"]]
            pts = [(bx + c, by), (bx + bw, by), (bx + bw, by + bh - c), (bx + bw - c, by + bh), (bx, by + bh), (bx, by + c)]
            pg.draw.polygon(surf, (6, 8, 12, int(235 * alpha)), pts)
            pg.draw.polygon(surf, cc + (int(255 * alpha * (0.5 if e["p"] <= 4 else 0.35)),), pts, 1)
            fu = self.font(False, max(10, round(f * 0.72)))
            tm = time.strftime("%H:%M:%S", time.localtime(e["t"]))
            rx = bx + bw - pad - c * 0.3
            head = [(tm, (141, 160, 179)), ("  ", None), (PRIO_LABEL[e["p"]], cc)]
            hx = rx - sum(fu.size(t)[0] for t, _ in head)
            for t, tc in head:
                if tc:
                    img = fu.render(t, True, tc)
                    img.set_alpha(int(255 * alpha))
                    surf.blit(img, (hx, by + f * 0.95 - img.get_height() / 2))
                hx += fu.size(t)[0]
            unit = e["u"].replace(".service", "")
            while len(unit) > 1 and fu.size(unit)[0] > rx - hx - pad - f:
                unit = unit[:-2] + "…"
            img = fu.render(unit, True, (180, 194, 209))
            img.set_alpha(int(255 * alpha))
            surf.blit(img, (bx + pad, by + f * 0.95 - img.get_height() / 2))
            fm = self.font(True, max(11, round(f * 0.82)))
            body = e["m"]
            while len(body) > 1 and fm.size(body)[0] > bw - 2 * pad:
                body = body[:-2] + "…"
            img = fm.render(body, True, (232, 241, 251))
            img.set_alpha(int(255 * alpha))
            surf.blit(img, (bx + pad, by + f * 2.15 - img.get_height() / 2))
            col += 1
            if col == cols:
                col, by = 0, by + bh + gap
        return pg.image.tostring(surf, "RGBA", True), top


class Renderer:
    def __init__(self, ctx):
        import moderngl
        self.mgl = moderngl
        self.ctx = ctx
        self.ring = ctx.program(vertex_shader=RING_VS, fragment_shader=RING_FS)
        self.tri = ctx.program(vertex_shader=TRI_VS, fragment_shader=TRI_FS)
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], np.float32)
        self.quad_vao = ctx.vertex_array(self.ring, [(ctx.buffer(quad), "2f", "in_pos")])
        self.tri_buf = ctx.buffer(reserve=Scene.MAX_TRI * 3 * 6 * 4, dynamic=True)
        self.tri_vao = ctx.vertex_array(self.tri, [(self.tri_buf, "2f 4f", "in_pos", "in_col")])
        self.bub = ctx.program(vertex_shader=BUBBLE_VS, fragment_shader=BUBBLE_FS)
        self.bub_buf = ctx.buffer(reserve=Scene.MAX_TRI * 6 * 9 * 4, dynamic=True)
        self.bub_vao = ctx.vertex_array(self.bub, [(self.bub_buf, "2f 2f 4f 1f", "in_pos", "in_uv", "in_col", "in_rad")])
        self.tex = ctx.texture((TAB_N, 2), 1, dtype="f4")
        self.tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.ring["u_tab"].value = 0
        self.ov = ctx.program(vertex_shader=OVERLAY_VS, fragment_shader=OVERLAY_FS)
        self.ov_vao = ctx.vertex_array(self.ov, [(ctx.buffer(quad), "2f", "in_pos")])
        self.ov_tex = self.top_tex = None

    def _layer(self, tex, data, unit, w, h):
        """Strato RGBA premoltiplicato a schermo intero (journal, riquadro info)."""
        ctx, mgl = self.ctx, self.mgl
        if tex is None or tex.size != (w, h):
            tex = ctx.texture((w, h), 4)
        tex.write(data)
        ctx.enable(mgl.BLEND); ctx.blend_func = mgl.ONE, mgl.ONE_MINUS_SRC_ALPHA
        tex.use(unit); self.ov["u_img"].value = unit
        self.ov_vao.render(mgl.TRIANGLE_STRIP)
        return tex

    def draw(self, scene, w, h, overlay=None, top=None):
        ctx, mgl = self.ctx, self.mgl
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 1.0)
        if overlay is not None:                       # journal: sotto l'anello
            self.ov_tex = self._layer(self.ov_tex, overlay, 1, w, h)
        ctx.enable(mgl.BLEND); ctx.blend_func = mgl.ONE, mgl.ONE_MINUS_SRC_ALPHA   # anello premoltiplicato

        r = self.ring
        r["u_res"].value = (w, h)
        sc = min(w, h) / 800
        # il quadrato disegnato copre solo il cerchio, l'eco e l'alone: il resto è nero
        r["u_half"].value = scene.r0 * 1.3 + 580.0 * sc
        r["u_sc"].value = sc
        r["u_level"].value = scene.level
        r["u_flash"].value = scene.flash
        r["u_core"].value, r["u_glow"].value, r["u_halo"].value = RING_CORE, RING_GLOW, RING_HALO
        r["u_echo"].value, r["u_pcol"].value = RING_ECHO, PULSE_COL
        r["u_pulse"].write(scene.pulse_uniform().tobytes())
        r["u_liquid"].value = float(scene.inner == "liquid")
        r["u_flow"].value, r["u_energy"].value, r["u_r0"].value = scene.flow, scene.bri, scene.r0
        r["u_glitch"].value, r["u_time"].value = scene.glitch, scene.time
        r["u_liq"].value = scene.liq
        self.tex.write(scene.tab.tobytes())
        self.tex.use(0)
        self.quad_vao.render(mgl.TRIANGLE_STRIP)
        if top is not None:                           # riquadro info: sopra l'anello
            self.top_tex = self._layer(self.top_tex, top, 2, w, h)

        v = scene.vertices()
        if len(v):
            ctx.enable(mgl.BLEND)
            ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA
            self.tri_buf.write(v.tobytes())
            self.tri["u_res"].value = (w, h)
            self.tri["u_feather"].value = 1.5 * sc
            self.tri_vao.render(mgl.TRIANGLES, vertices=len(v))

        vb = scene.bubble_vertices()
        if len(vb):
            ctx.enable(mgl.BLEND)
            ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA
            self.bub_buf.write(vb.tobytes())
            self.bub["u_res"].value = (w, h)
            self.bub["u_feather"].value = 1.5 * sc
            self.bub_vao.render(mgl.TRIANGLES, vertices=len(vb))



# ----------------------------------------------------------------------------- monitor del terminale
def _wl_connect():
    import os
    import socket
    name = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
    path = name if name.startswith("/") else os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), name)
    sk = socket.socket(socket.AF_UNIX)
    sk.settimeout(1.0)
    sk.connect(path)
    return sk


def _wl_snapshot():
    """Parla il protocollo Wayland grezzo (wl_output + zwlr_foreign_toplevel_management_v1: nessuna
    dipendenza) e restituisce (outs, tops, order): output, finestre e ordine degli output. None se non possibile."""
    import struct
    try:
        sk = _wl_connect()
    except (OSError, KeyError):
        return None
    buf = bytearray()
    nid = [3]                            # gli id del client devono essere consecutivi

    def send(obj, op, payload=b""):
        sk.sendall(struct.pack("<II", obj, ((8 + len(payload)) << 16) | op) + payload)

    def pstr(t):
        b = t.encode() + b"\0"
        return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)

    def rstr(d, o):
        (n,) = struct.unpack_from("<I", d, o)
        return d[o + 4:o + 4 + n - 1].decode(errors="replace"), o + 4 + (n + 3) // 4 * 4

    def new_id():
        nid[0] += 1
        return nid[0]

    outs, tops, glob = {}, {}, []         # id -> dati
    state = {"done": 0}

    def pump(cb):
        """Legge messaggi finché arriva il done del callback `cb`."""
        while True:
            while len(buf) >= 8:
                obj, w = struct.unpack_from("<II", buf)
                size, op = w >> 16, w & 0xFFFF
                if len(buf) < size:
                    break
                d = bytes(buf[8:size])
                del buf[:size]
                if obj == cb and op == 0:
                    return True
                if obj == 2 and op == 0:                          # registry.global
                    (nm,) = struct.unpack_from("<I", d)
                    itf, o = rstr(d, 4)
                    (ver,) = struct.unpack_from("<I", d, o)
                    glob.append((nm, itf, ver))
                elif obj in outs:
                    od = outs[obj]
                    if op == 0:                                    # geometry
                        od["tr"] = _geo_transform(d)
                    elif op == 1:                                  # mode
                        fl, mw, mh, _ = struct.unpack_from("<Iiii", d)
                        if fl & 1:
                            od["mode"] = (mw, mh)
                    elif op == 3:
                        od["scale"] = struct.unpack_from("<i", d)[0]
                    elif op == 4:
                        od["name"] = rstr(d, 0)[0]
                elif obj in tops:
                    t = tops[obj]
                    if op == 2:                                    # output_enter
                        t["outs"].append(struct.unpack_from("<I", d)[0])
                    elif op == 3:
                        o2 = struct.unpack_from("<I", d)[0]
                        t["outs"] = [x for x in t["outs"] if x != o2]
                    elif op == 4:                                  # state: array di uint
                        (n,) = struct.unpack_from("<I", d)
                        t["active"] = 2 in struct.unpack_from(f"<{n // 4}I", d, 4)
                elif obj == mgr and op == 0:                      # toplevel
                    tops[struct.unpack_from("<I", d)[0]] = {"outs": [], "active": False}
            try:
                chunk = sk.recv(65536)
            except OSError:
                return False
            if not chunk:
                return False
            buf.extend(chunk)

    def _geo_transform(d):
        o = 20                                                     # x,y,pw,ph,subpixel
        _, o = rstr(d, o)
        _, o = rstr(d, o)
        return struct.unpack_from("<i", d, o)[0]

    mgr = -1
    try:
        send(1, 1, struct.pack("<I", 2))                           # get_registry
        send(1, 0, struct.pack("<I", 3))                           # sync
        if not pump(3):
            return None
        order = []
        for nm, itf, ver in glob:
            if itf == "wl_output":
                i, v = new_id(), min(ver, 4)
                send(2, 0, struct.pack("<I", nm) + pstr(itf) + struct.pack("<II", v, i))
                outs[i] = {"name": None, "tr": 0, "scale": 1, "mode": None}
                order.append(i)
            elif itf == "zwlr_foreign_toplevel_manager_v1":
                mgr, v = new_id(), min(ver, 3)
                send(2, 0, struct.pack("<I", nm) + pstr(itf) + struct.pack("<II", v, mgr))
        if mgr < 0:
            return None
        for _ in range(3):                                         # più roundtrip: output, toplevel, loro eventi
            cb = new_id()
            send(1, 0, struct.pack("<I", cb))
            if not pump(cb):
                return None
        return outs, tops, order
    except (OSError, struct.error):
        return None
    finally:
        sk.close()


def terminal_output():
    """Wayland (wlroots/labwc): (nome, larghezza, altezza, indice) dell'output su cui si trova la
    finestra attiva, cioè il terminale da cui si lancia il programma. None se non determinabile."""
    snap = _wl_snapshot()
    if not snap:
        return None
    outs, tops, order = snap
    act = [t for t in tops.values() if t["active"] and t["outs"]]
    if not act:
        return None
    oid = act[0]["outs"][0]
    od = outs.get(oid)
    if not od or not od["mode"]:
        return None
    w, h = od["mode"]
    if od["tr"] in (1, 3, 5, 7):
        w, h = h, w
    sc = max(od["scale"], 1)
    return od["name"], w // sc, h // sc, order.index(oid)


# ----------------------------------------------------------------------------- main
def read_config(path):
    """config.ini + config.local.ini (stessa cartella, non tracciato da git: impostazioni di questa macchina)."""
    path = Path(path)
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read([path, path.with_name(f"{path.stem}.local{path.suffix}")])
    return cp


def load_config(ap):
    """Legge [hringr] da config.ini e lo usa come valori predefiniti degli argomenti."""
    pre = argparse.ArgumentParser(add_help=False)      # solo --config, così --help vede i valori del file
    pre.add_argument("--config", default=ap.get_default("config"))
    path = pre.parse_known_args()[0].config
    cp = read_config(path)
    if not cp.has_section("hringr"):
        return
    defaults = {}
    for key, raw in cp.items("hringr"):
        dest = key.replace("-", "_")
        act = next((x for x in ap._actions if x.dest == dest and dest not in ("help", "config")), None)
        if act is None:
            ap.error(f"{path}: unknown option '{key}'")
        if isinstance(act, argparse._StoreTrueAction):
            val = cp.getboolean("hringr", key)
        else:
            try:
                val = act.type(raw) if act.type else raw
            except (ValueError, argparse.ArgumentTypeError) as e:
                ap.error(f"{path}: {key}: {e}")
        if act.choices and val not in act.choices:
            ap.error(f"{path}: {key} must be one of {', '.join(map(str, act.choices))} (got '{raw}')")
        defaults[dest] = val
    ap.set_defaults(**defaults)


EPILOG = '''Every option can also be set in config.ini, section [hringr], without "--" (dashes or "_"):
  shape, inner, liquid_color, demo, fullscreen, audio_source, bridge_url, bridge_scale, audio_device,
  audio_gain, max_mbps, tri_size, display, size
Command-line options take precedence over the file. Boolean values are written true/false.
Keys: F full screen, D demo on/off, ESC or Q quit.'''


def main():
    class Fmt(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
        pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=Fmt, add_help=False, epilog=EPILOG)
    ap.add_argument("-h", "--help", action="help", help="show this help and exit")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.ini")),
                    help="configuration file (default values; the command line takes precedence)")
    ap.add_argument("--shape", choices=("triangles", "bubbles"), default=DEFAULTS["shape"],
                    help="shape of what flows toward the circle: triangles or bubbles")
    ap.add_argument("--inner", choices=("pulse", "liquid"), default=DEFAULTS["inner"],
                    help="effect inside the circle: pulse = rings beating from the centre, liquid = moving liquid")
    ap.add_argument("--liquid-color", type=_color, default=DEFAULTS["liquid_color"],
                    help="liquid color, hex RRGGBB (in config.ini write it without '#')")
    ap.add_argument("--demo", action="store_true", help="simulated signals")
    ap.add_argument("--fullscreen", action="store_true", help="full screen (toggle with the F key)")
    ap.add_argument("--audio-source", choices=("auto", "bridge", "loopback"), default=DEFAULTS["audio_source"],
                    help="bridge = cava bars from mpd-spectrum (no conflict with MPD); "
                         "loopback = direct capture from the sound card; auto = bridge if reachable")
    ap.add_argument("--bridge-url", default=DEFAULTS["bridge_url"],
                    help="address to read the cava bars from (mpd-spectrum)")
    ap.add_argument("--bridge-scale", type=_positive, default=DEFAULTS["bridge_scale"],
                    help="average of the bars (0-100) that maps to full level")
    ap.add_argument("--audio-device", default=None, help="part of the capture device name")
    ap.add_argument("--audio-gain", type=float, default=0.0, help="dB added to the audio level")
    ap.add_argument("--max-mbps", type=_positive, default=DEFAULTS["max_mbps"], help="Mbit/s = maximum effect (log scale)")
    ap.add_argument("--tri-size", type=_positive, default=DEFAULTS["tri_size"], help="size multiplier for the triangles")
    ap.add_argument("--display", default="auto",
                    help="monitor to open the window on: auto = the one with the launching terminal "
                         "(Wayland/labwc), or the index 0, 1, ... in SDL order")
    ap.add_argument("--size", default="1280x800", help="window WxH")
    load_config(ap)
    a = ap.parse_args()
    import hringr_web          # solo journal_cfg (import qui: hringr_web importa questo modulo)
    import journal_stream
    jcfg = hringr_web.journal_cfg(read_config(a.config))
    journal = None
    if jcfg["enabled"]:
        journal = journal_stream.Journal(jcfg["message_max"], save_path=jcfg["save_path"],
                                         save_max_kb=jcfg["save_max_kb"])
        journal.start()
    panel = JournalPanel(jcfg, journal)

    import moderngl
    import pygame

    pygame.init()
    w, h = (int(x) for x in a.size.lower().split("x"))
    sizes = pygame.display.get_desktop_sizes()
    if a.display == "auto":
        disp = 0
        found = terminal_output()
        if found:
            nm, ow, oh, idx = found
            # l'ordine degli output di SDL coincide di norma con quello del registry; si verifica con la risoluzione
            if idx < len(sizes) and tuple(sizes[idx]) == (ow, oh):
                disp = idx
            elif (ow, oh) in [tuple(x) for x in sizes]:
                disp = [tuple(x) for x in sizes].index((ow, oh))
            print(f"hringr: terminal on {nm} ({ow}x{oh}) -> display {disp}")
        else:
            disp = 0
            print("hringr: terminal monitor not detected, using display 0")
    else:
        try:
            disp = int(a.display)
        except ValueError:
            ap.error("--display must be 'auto' or a number")
        if not 0 <= disp < len(sizes):
            ap.error(f"--display {disp}: available monitors 0..{len(sizes) - 1} {sizes}")
    flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.RESIZABLE

    def real_size():
        # Con schermo ruotato (Wayland) get_surface().get_size() può dare la
        # geometria non ruotata; la dimensione della finestra è quella reale.
        pygame.event.pump()
        ww, wh = pygame.display.get_window_size()
        return (ww, wh) if ww > 0 and wh > 0 else pygame.display.get_surface().get_size()

    def open_window(full):
        if full:
            pygame.display.set_mode((0, 0), flags | pygame.FULLSCREEN, display=disp, vsync=1)
        else:
            pygame.display.set_mode((w, h), flags, display=disp, vsync=1)
        pygame.mouse.set_visible(True)           # anche a schermo intero, come nel web
        ctx = moderngl.create_context(require=310)   # il Pi 5 offre OpenGL 3.1
        return Renderer(ctx), real_size()

    full = a.fullscreen
    rend, (W, H) = open_window(full)

    sig = Signals()
    demo, dsig = a.demo, Signals()     # la demo ha segnali propri: i thread reali non la disturbano
    # i thread partono sempre: con --demo il tasto D deve poter tornare ai dati reali
    # (in demo i valori reali vengono sovrascritti a ogni frame)
    use_bridge = a.audio_source == "bridge" or (a.audio_source == "auto" and bridge_available(a.bridge_url))
    if use_bridge:
        start_bridge_thread(sig, a.bridge_url, a.bridge_scale)
    else:
        start_audio_thread(sig, a.audio_device, a.audio_gain)
    start_stats_thread(sig)
    scene = Scene(size_mul=a.tri_size, shape=a.shape, inner=a.inner, liquid_color=a.liquid_color)
    clock = pygame.time.Clock()
    t0 = last = last_cap = time.time()
    running = True
    while running:
        for e in pygame.event.get():
            if panel and panel.event(e):
                continue
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN:
                if e.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif e.key == pygame.K_d:
                    demo = not demo
                elif e.key == pygame.K_f:
                    full = not full
                    rend, (W, H) = open_window(full)
        W, H = real_size()

        now = time.time()
        dt = min(now - last, 0.1)
        last = now
        if demo:
            demo_signals(dsig, now - t0)
        s = dsig if demo else sig
        scene.update(dt, s.audio, net_norm(s.down_mbps, a.max_mbps),
                     net_norm(s.up_mbps, a.max_mbps), s.cpu, s.ram, W, H, max(s.swap, panel.kick))
        ov, top = panel.draw(W, H, s)
        rend.draw(scene, W, H, ov, top)
        pygame.display.flip()
        clock.tick(60)

        if now - last_cap > 0.5:
            last_cap = now
            pygame.display.set_caption(
                f"audio {s.audio:.2f} | ↓ {s.down_mbps:.1f} ↑ {s.up_mbps:.1f} Mbit/s"
                f" | CPU {s.cpu:.0%} RAM {s.ram:.0%} | {len(scene.pos)} tri"
                f" | {'DEMO' if demo else sig.audio_status}")
    if journal:
        journal.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
