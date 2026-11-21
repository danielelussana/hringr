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

## Journal di systemd (web)

Sopra l'anello, una corsia per ogni unità systemd (`_SYSTEMD_UNIT`, altrimenti `SYSLOG_IDENTIFIER`/`_COMM`) con un punto per ogni messaggio, colorato per priorità; l'anello (con `inner = liquid`) si tinge del colore dei messaggi gravi e torna al colore base. In alto: messaggi degli ultimi 60 s e contatori per priorità. Un punto significa solo "questa unità ha registrato un messaggio": il journal non dice chi ha causato cosa, quindi la pagina non disegna legami tra corsie.

- Dipendenze: solo `journalctl` (systemd, presente su Debian e Raspberry Pi OS, x86_64 e ARM64). Nessun pacchetto Python in più.
- Sola lettura: `journalctl -f -n 0 -o json`, argomenti fissi, nessuna shell. Non avvia né ferma servizi. Se `journalctl` manca o i permessi non bastano, l'intestazione della pagina mostra l'errore.
- Permessi, senza root: basta far parte di un gruppo che legge il journal. `sudo usermod -aG systemd-journal $USER` (oppure `adm`, già presente su Raspberry Pi OS), poi nuova sessione. Dai il gruppo solo a chi deve leggere i log di sistema: contengono dati sensibili.
- Rete: il flusso `/journal` risponde solo a client e `Host` su localhost (apri `http://localhost:8766/`). Dall'esterno risponde 403 e la pagina non mostra il pannello. Via Tailscale: `network = tailscale` (in `config.local.ini`) e apri la pagina con l'IP tailscale, il nome `*.ts.net` o l'hostname del Pi; resta escluso il resto della rete. Per aprirlo a tutta la rete: `network = yes` in `[journal]`, ma i log possono contenere utenti, IP, percorsi e segreti e la porta non ha autenticazione. Dietro un reverse proxy locale tutti i client appaiono come localhost: non usarlo così.
- Privacy: nessuna connessione esterna, il testo dei log non va in URL, cookie o log dell'app. Restano in RAM, al massimo `max_events` nella pagina. Il salvataggio su file è spento; con `save_path` (meglio su tmpfs, es. `/dev/shm/hringr-journal.jsonl`, per non consumare la SD) il file viene azzerato oltre `save_max_kb`.
- Font delle bubble: Chakra Petch e Share Tech Mono (licenza OFL, `fonts/`), serviti dal server locale su `/fonts/`; se mancano resta il monospazio di sistema.
- Tutto si configura in `[journal]` di `config.ini` (colori, finestra, corsie, verso, altezza, opacità, limiti); le impostazioni locali in `config.local.ini`. `enabled = no` disattiva la funzione.
- Il lettore riparte da solo se `journalctl` termina (attesa crescente fino a 30 s) e lo chiude all'arresto (Ctrl+C o SIGTERM).
- Limiti noti: la versione desktop (`hringr.py`) non lo mostra. Righe JSON oltre 256 KiB vengono scartate. Con una raffica di log il server invia al massimo 5 pacchetti al secondo e i contatori restano esatti.

## License / Licenza

[GNU AGPL-3.0](LICENSE)

Copyright (C) 2026 Daniele "DLx" Lussana
