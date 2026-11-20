# Copyright (C) 2026 Daniele "DLx" Lussana
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prova senza schermo né audio:  .venv/bin/python -m unittest discover -s tests"""
import argparse
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hringr  # noqa: E402
import hringr_web  # noqa: E402


class Config(unittest.TestCase):
    def test_positive_rejects_zero_and_negative(self):
        for bad in ("0", "-5", "abc"):
            with self.assertRaises((ValueError, argparse.ArgumentTypeError)):
                hringr._positive(bad)
        self.assertEqual(hringr._positive("200"), 200.0)

    def test_load_config_reads_values_and_rejects_bad_ones(self):
        def parse(text):
            ap = argparse.ArgumentParser(exit_on_error=False)
            ap.add_argument("--config")
            ap.add_argument("--max-mbps", type=hringr._positive, default=200.0)
            ap.add_argument("--shape", choices=("triangles", "bubbles"), default="triangles")
            with tempfile.NamedTemporaryFile("w", suffix=".ini") as f:
                f.write(text)
                f.flush()
                ap.set_defaults(config=f.name)
                hringr.load_config(ap)
            return ap.parse_args([])
        a = parse("[hringr]\nshape = bubbles   # commento\nmax_mbps = 50\n")
        self.assertEqual((a.shape, a.max_mbps), ("bubbles", 50.0))
        with self.assertRaises(SystemExit):
            parse("[hringr]\nmax_mbps = 0\n")


    def test_local_config_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.ini").write_text("[hringr]\naudio_source = auto\nshape = bubbles\n")
            (Path(d) / "config.local.ini").write_text("[hringr]\naudio_source = bridge\n")
            cp = hringr.read_config(Path(d) / "config.ini")
            self.assertEqual(cp.get("hringr", "audio_source"), "bridge")
            self.assertEqual(cp.get("hringr", "shape"), "bubbles")


class Scene(unittest.TestCase):
    def test_arrays_stay_aligned(self):
        for shape in ("triangles", "bubbles"):
            s = hringr.Scene(seed=1, shape=shape, inner="liquid")
            for i in range(400):
                s.update(0.016, 0.9, 1.0, 1.0, 0.5, 0.7, 1280, 800)
                n = len(s.pos)
                for name in ("dir", "spd", "size", "col", "kind", "ph", "_r", "_ring"):
                    self.assertEqual(len(getattr(s, name)), n, f"{name} dopo {i} passi")
                self.assertLessEqual(n, hringr.Scene.MAX_TRI)
            self.assertEqual(s.vertices().shape[1], 6)
            self.assertEqual(s.bubble_vertices().shape[1], 9)
            self.assertLessEqual(len(s.pulse_uniform()), hringr.MAX_PULSES)


class Proc(unittest.TestCase):
    def sample(self, text):
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write(text)
        f.close()
        return f.name

    def test_net_skips_loopback(self):
        p = self.sample("h1\nh2\n    lo: 100 0 0 0 0 0 0 0 900 0 0 0 0 0 0 0\n"
                        "  eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n")
        self.assertEqual(hringr.read_net(p), (1000, 2000))

    def test_cpu_and_ram(self):
        self.assertEqual(hringr.read_cpu(self.sample("cpu  10 0 10 70 10 0 0 0 0 0\n")), (100, 80))
        ram = hringr.read_ram(self.sample("MemTotal: 1000 kB\nMemAvailable: 250 kB\n"))
        self.assertAlmostEqual(ram, 0.75)

    def test_real_system(self):
        self.assertTrue(0.0 <= hringr.read_ram() <= 1.0)


class Shared(unittest.TestCase):
    def test_no_constants_duplicated_in_html(self):
        html = (ROOT / "hringr.html").read_text()
        self.assertNotIn("const WAVES", html)
        self.assertNotIn("const TAB_N", html)

    def test_shader_table_size_matches_params(self):
        self.assertIn(f"const int N = {hringr.TAB_N};", hringr.RING_FS)
        self.assertIn(f"const int N = {hringr.TAB_N};", (ROOT / "hringr.html").read_text())


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        defaults = {k: str(hringr.DEFAULTS[k]) for k in hringr_web.WEB_KEYS}
        defaults["audio_source"] = "loopback"
        hringr_web.args = argparse.Namespace(defaults=defaults, bridge_url=hringr.DEFAULTS["bridge_url"])
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), hringr_web.Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_config_carries_shared_params(self):
        d = json.load(urllib.request.urlopen(self.base + "/config.json"))
        self.assertEqual(d["params"], hringr.PARAMS)
        self.assertEqual(d["max_mbps"], "200.0")

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(self.base + "/nope")
        self.assertEqual(c.exception.code, 404)

    def test_client_cap_returns_503(self):
        taken = [hringr_web.clients.acquire(blocking=False) for _ in range(hringr_web.MAX_CLIENTS)]
        try:
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(self.base + "/events?audio_source=loopback")
            self.assertEqual(c.exception.code, 503)
        finally:
            for t in taken:
                if t:
                    hringr_web.clients.release()


if __name__ == "__main__":
    unittest.main()
