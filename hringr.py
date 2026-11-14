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
Tasti: F schermo intero, D demo on/off, ESC o Q esce.
"""
import argparse
import configparser
import math
import threading
import time
from pathlib import Path

import numpy as np


def _hex(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _color(s):
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError(
            f"colore non valido '{s}': servono 6 cifre esadecimali (RRGGBB); nel config.ini scrivere senza '#'")
    return _hex(s)

# Colori presi dall'immagine di riferimento (RGB 0..1)
LIGHT = [_hex(h) for h in ("E8E8EA", "C9D8E8", "A7C4E5")]   # triangoli in arrivo
BLUES = [_hex(h) for h in ("4399D3", "0083C8", "006CA8")]   # triangoli in uscita
RING_CORE, RING_GLOW, RING_ECHO = _hex("C9D8E8"), _hex("0083C8"), _hex("4399D3")
RING_HALO = _hex("006CA8")
PULSE_COL = _hex("0083C8")

# Onde che deformano il perimetro: (lobi, ampiezza relativa, velocità base rad/s)
WAVES = [(3, 0.55, 0.9), (5, 0.30, -1.4), (8, 0.20, 2.2), (13, 0.12, -3.5), (21, 0.07, 5.5)]
WK = np.array([w[0] for w in WAVES], np.float32)
WA = np.array([w[1] for w in WAVES], np.float32)
WS = np.array([w[2] for w in WAVES], np.float32)

TAB_N = 512                       # campioni angolari della tabella del perimetro
GRID = np.linspace(-np.pi, np.pi, TAB_N, endpoint=False).astype(np.float32)
MAX_PULSES = 6


# ----------------------------------------------------------------------------- segnali
class Signals:
    def __init__(self):
        self.audio = 0.0          # 0..1
        self.down_mbps = 0.0
        self.up_mbps = 0.0
        self.cpu = 0.0            # 0..1
        self.ram = 0.0            # 0..1
        self.audio_status = "audio: in avvio"


def start_audio_thread(sig, device=None, gain_db=0.0):
    """Audio in uscita (loopback). Su Raspberry Pi OS (PipeWire/PulseAudio) è nativo."""
    def run():
        try:
            import soundcard as sc
            if device:
                mics = sc.all_microphones(include_loopback=True)
                mic = next((m for m in mics if device.lower() in m.name.lower()), None)
                if mic is None:
                    raise RuntimeError(f"dispositivo '{device}' non trovato: "
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
            sig.audio_status = f"audio NON disponibile ({e})"
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
                    sig.audio_status = "audio: bridge attivo, nessun segnale"
                else:
                    sig.audio = min(1.0, (sum(bars) / len(bars)) / scale)
                    sig.audio_status = "audio: mpd-spectrum (cava)"
            except Exception as e:  # noqa: BLE001
                sig.audio = 0.0
                sig.audio_status = f"audio: bridge non raggiungibile ({e})"
            time.sleep(0.04)
    threading.Thread(target=run, daemon=True).start()


def bridge_available(url):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=0.5):
            return True
    except Exception:  # noqa: BLE001
        return False


def start_stats_thread(sig):
    """Rete (download/upload), CPU e RAM, campionati 5 volte al secondo."""
    import psutil

    def counters():
        rx = tx = 0
        for name, c in psutil.net_io_counters(pernic=True).items():
            if name.lower().startswith(("lo", "loopback")):
                continue
            rx += c.bytes_recv
            tx += c.bytes_sent
        return rx, tx

    def run():
        rx0, tx0 = counters()
        t0 = time.time()
        psutil.cpu_percent(None)
        while True:
            time.sleep(0.2)
            rx1, tx1 = counters()
            t1 = time.time()
            dt = t1 - t0
            d = max(0, rx1 - rx0) * 8 / 1e6 / dt
            u = max(0, tx1 - tx0) * 8 / 1e6 / dt
            sig.down_mbps += (d - sig.down_mbps) * 0.5
            sig.up_mbps += (u - sig.up_mbps) * 0.5
            sig.cpu = psutil.cpu_percent(None) / 100.0
            sig.ram = psutil.virtual_memory().percent / 100.0
            rx0, tx0, t0 = rx1, tx1, t1
    threading.Thread(target=run, daemon=True).start()


def demo_signals(sig, t):
    burst = max(0.0, math.sin(t * 0.7)) ** 2
    sig.audio = min(1.0, 0.08 + 0.9 * burst * (0.6 + 0.4 * math.sin(t * 9) ** 2))
    sig.down_mbps = 200 * max(0.0, math.sin(t * 0.37 + 1.0)) ** 3
    sig.up_mbps = 80 * max(0.0, math.sin(t * 0.53 + 3.0)) ** 3
    sig.cpu = 0.5 + 0.5 * math.sin(t * 0.3)
    sig.ram = 0.55 + 0.2 * math.sin(t * 0.1)


def net_norm(mbps, max_mbps):
    """Scala logaritmica 0..1."""
    return min(1.0, math.log1p(max(mbps, 0.0)) / math.log1p(max_mbps))


# ----------------------------------------------------------------------------- scena
class Scene:
    MAX_TRI = 12000

    def __init__(self, seed=None, size_mul=1.0, shape="triangles", inner="pulse", liquid_color=None):
        self.rng = np.random.default_rng(seed)
        self.inner = inner                # effetto dentro il cerchio: pulse | liquid
        self.liq = liquid_color or _hex("0083C8")
        self.flow = 0.0                   # fase del liquido: avanza più in fretta con la CPU
        self.phase = np.zeros(len(WAVES), np.float32)
        self.level = 0.0
        self.pin = 0.0                    # CPU smussata: velocità dell'effetto interno
        self.bri = 0.0                    # RAM smussata (40%..85% -> 0..1): luminosità dell'effetto interno
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

    def update(self, dt, audio, down, up, cpu, ram, w, h):
        """audio, down, up, cpu, ram in 0..1."""
        rng = self.rng
        self.time += dt
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
        self.bri += (min(1.0, max(0.0, (ram - 0.4) / 0.45)) - self.bri) * k
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
        self.pos += self.dir * (self.spd * dt)[:, None]
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
void main(){
    vec2 p = gl_FragCoord.xy - 0.5 * u_res;
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
    frag = vec4(min(col, vec3(1.0)), 1.0);
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

    def draw(self, scene, w, h):
        ctx, mgl = self.ctx, self.mgl
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 1.0)
        ctx.disable(mgl.BLEND)

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
        r["u_liq"].value = scene.liq
        self.tex.write(scene.tab.tobytes())
        self.tex.use(0)
        self.quad_vao.render(mgl.TRIANGLE_STRIP)

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


def terminal_output():
    """Wayland (wlroots/labwc): restituisce (nome, larghezza, altezza) dell'output su cui si trova la
    finestra attiva, cioè il terminale da cui si lancia il programma. None se non determinabile.
    Parla il protocollo grezzo (wl_output + zwlr_foreign_toplevel_management_v1): nessuna dipendenza."""
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
    except (OSError, struct.error):
        return None
    finally:
        sk.close()


# ----------------------------------------------------------------------------- main
def load_config(ap):
    """Legge [hringr] da config.ini e lo usa come valori predefiniti degli argomenti."""
    pre = argparse.ArgumentParser(add_help=False)      # solo --config, così --help vede i valori del file
    pre.add_argument("--config", default=ap.get_default("config"))
    path = pre.parse_known_args()[0].config
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if not cp.read(path) or not cp.has_section("hringr"):
        return
    defaults = {}
    for key, raw in cp.items("hringr"):
        dest = key.replace("-", "_")
        act = next((x for x in ap._actions if x.dest == dest and dest not in ("help", "config")), None)
        if act is None:
            ap.error(f"{path}: opzione sconosciuta '{key}'")
        if isinstance(act, argparse._StoreTrueAction):
            val = cp.getboolean("hringr", key)
        else:
            try:
                val = act.type(raw) if act.type else raw
            except (ValueError, argparse.ArgumentTypeError) as e:
                ap.error(f"{path}: {key}: {e}")
        if act.choices and val not in act.choices:
            ap.error(f"{path}: {key} deve essere uno tra {', '.join(map(str, act.choices))} (trovato '{raw}')")
        defaults[dest] = val
    ap.set_defaults(**defaults)


EPILOG = '''Ogni opzione si può impostare anche in config.ini, sezione [hringr], senza "--" (trattini o "_"):
  shape, inner, liquid_color, demo, fullscreen, audio_source, bridge_url, bridge_scale, audio_device,
  audio_gain, max_mbps, tri_size, display, size
Le opzioni a riga di comando prevalgono sul file. I valori booleani si scrivono true/false.
Tasti: F schermo intero, D demo on/off, ESC o Q esce.'''


def main():
    class Fmt(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
        pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=Fmt, add_help=False, epilog=EPILOG)
    ap.add_argument("-h", "--help", action="help", help="mostra questo aiuto ed esce")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.ini")),
                    help="file di configurazione (valori predefiniti; la riga di comando prevale)")
    ap.add_argument("--shape", choices=("triangles", "bubbles"), default="triangles",
                    help="forma di ciò che si dirige verso il cerchio: triangoli o bolle")
    ap.add_argument("--inner", choices=("pulse", "liquid"), default="pulse",
                    help="effetto dentro il cerchio: pulse = battito ad anelli, liquid = liquido che si muove")
    ap.add_argument("--liquid-color", type=_color, default="0083C8",
                    help="colore del liquido, esadecimale RRGGBB (nel config.ini scrivere senza '#')")
    ap.add_argument("--demo", action="store_true", help="segnali simulati")
    ap.add_argument("--fullscreen", action="store_true", help="schermo intero (si può alternare con il tasto F)")
    ap.add_argument("--audio-source", choices=("auto", "bridge", "loopback"), default="auto",
                    help="bridge = barre di cava da mpd-spectrum (nessun conflitto con MPD); "
                         "loopback = cattura diretta dalla scheda; auto = bridge se raggiungibile")
    ap.add_argument("--bridge-url", default="http://127.0.0.1:44883/bars",
                    help="indirizzo da cui leggere le barre di cava (mpd-spectrum)")
    ap.add_argument("--bridge-scale", type=float, default=22.0,
                    help="media delle barre (0-100) che corrisponde al livello massimo")
    ap.add_argument("--audio-device", default=None, help="parte del nome del dispositivo di cattura")
    ap.add_argument("--audio-gain", type=float, default=0.0, help="dB aggiunti al livello audio")
    ap.add_argument("--max-mbps", type=float, default=200.0, help="Mbit/s = effetto massimo (scala log)")
    ap.add_argument("--tri-size", type=float, default=1.0, help="moltiplicatore della dimensione dei triangoli")
    ap.add_argument("--display", default="auto",
                    help="monitor su cui aprire la finestra: auto = quello del terminale da cui si lancia "
                         "(Wayland/labwc), oppure l'indice 0, 1, ... nell'ordine di SDL")
    ap.add_argument("--size", default="1280x800", help="finestra LxA")
    load_config(ap)
    a = ap.parse_args()

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
            print(f"hringr: terminale su {nm} ({ow}x{oh}) -> display {disp}")
        else:
            disp = 0
            print("hringr: monitor del terminale non rilevabile, uso display 0")
    else:
        try:
            disp = int(a.display)
        except ValueError:
            ap.error("--display deve essere 'auto' o un numero")
        if not 0 <= disp < len(sizes):
            ap.error(f"--display {disp}: monitor disponibili 0..{len(sizes) - 1} {sizes}")
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
        pygame.mouse.set_visible(not full)
        ctx = moderngl.create_context(require=310)   # il Pi 5 offre OpenGL 3.1
        return Renderer(ctx), real_size()

    full = a.fullscreen
    rend, (W, H) = open_window(full)

    sig = Signals()
    demo = a.demo
    if not demo:
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
            demo_signals(sig, now - t0)
        scene.update(dt, sig.audio, net_norm(sig.down_mbps, a.max_mbps),
                     net_norm(sig.up_mbps, a.max_mbps), sig.cpu, sig.ram, W, H)
        rend.draw(scene, W, H)
        pygame.display.flip()
        clock.tick(60)

        if now - last_cap > 0.5:
            last_cap = now
            pygame.display.set_caption(
                f"audio {sig.audio:.2f} | ↓ {sig.down_mbps:.1f} ↑ {sig.up_mbps:.1f} Mbit/s"
                f" | CPU {sig.cpu:.0%} RAM {sig.ram:.0%} | {len(scene.pos)} tri"
                f" | {sig.audio_status if not demo else 'DEMO'}")
    pygame.quit()


if __name__ == "__main__":
    main()
