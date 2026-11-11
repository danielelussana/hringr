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
./run_web.sh               # web: open http://<host>:8765/  (keys / tasti: F fullscreen, D demo, A audio source)
```

Audio source / Sorgente audio: `bridge` (cava bars from `mpd-spectrum`, HTTP) or `loopback` (sound card capture), or `auto`.
Defaults live in `config.ini`; command line (desktop) or URL parameters (web, e.g. `/?inner=pulse&liquid_color=7B5CFF`) override them.
I valori predefiniti sono in `config.ini`; riga di comando (desktop) o parametri URL (web) li sovrascrivono.

## License / Licenza

[GNU AGPL-3.0](LICENSE)
