# hringr

*Old Norse for "ring".* A ring that reacts to your machine: audio, network, CPU and RAM drive a live OpenGL animation.
Runs as a desktop window (pygame + OpenGL 3.1, tuned for Raspberry Pi 5) or as a web page (WebGL2) served over the network.

*Un cerchio che reagisce alla macchina: audio, rete, CPU e RAM guidano un'animazione OpenGL in tempo reale.
Gira come finestra desktop (pygame + OpenGL 3.1, pensato per Raspberry Pi 5) o come pagina web (WebGL2) servita in rete.*

| Resource / Risorsa | Effect / Effetto |
|---|---|
| Audio out / Audio in uscita | Ring perimeter ripples like water / Il perimetro vibra come l'acqua |
| Download | Light triangles or bubbles fly in / Triangoli chiari o bolle in arrivo |
| Upload | Blue triangles fly out / Triangoli azzurri verso l'esterno |
| CPU | Inner effect **speed** / **Velocità** dell'effetto interno |
| RAM | Inner effect **brightness** / **Luminosità** dell'effetto interno |

## Run / Avvio

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt

./run.sh --fullscreen      # desktop window / finestra desktop (--demo = fake signals / segnali simulati)
./run_web.sh               # web: open http://<host>:8766/  (keys / tasti: F fullscreen, D demo, A audio source)
```

Audio source / Sorgente audio: `auto` (default: works everywhere), `loopback` (sound card capture) or `bridge` (cava bars from a custom `mpd-spectrum` HTTP service, not part of standard MPD).
Per-machine settings go in `config.local.ini` (git-ignored, overrides `config.ini`) / le impostazioni della singola macchina vanno in `config.local.ini` (ignorato da git, prevale su `config.ini`).
Defaults live in `config.ini`; command line (desktop) or URL parameters (web, e.g. `/?inner=pulse&liquid_color=7B5CFF`) override them.
I valori predefiniti sono in `config.ini`; riga di comando (desktop) o parametri URL (web) li sovrascrivono.

## Files / File

- `params.json`: costanti condivise (colori, onde, limiti) lette da `hringr.py` e inviate alla pagina web da `hringr_web.py` in `/config.json`. Si cambiano in un posto solo.
- `config.ini`: valori predefiniti; `max_mbps` e `tri_size` devono essere maggiori di 0.
- Il server web accetta al massimo 20 schede collegate (risposta 503 oltre il limite).
- Test: `.venv/bin/python -m unittest discover -s tests` (nessuno schermo o audio necessari). Le letture di CPU/RAM/rete usano `/proc`: solo Linux.

## License / Licenza

[GNU AGPL-3.0](LICENSE)

Copyright (C) 2026 Daniele "DLx" Lussana
