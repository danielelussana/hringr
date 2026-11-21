# Copyright (C) 2026 Daniele "DLx" Lussana
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prova senza schermo né audio:  .venv/bin/python -m unittest discover -s tests"""
import argparse
import configparser
import io
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
import journal_stream  # noqa: E402


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

    def test_swap_rate_and_match(self):
        self.assertEqual(hringr.read_swap(self.sample("pswpin 3\npswpout 4\nother 9\n")), 7)
        self.assertEqual(hringr.swap_norm(0), 0.0)
        self.assertEqual(hringr.swap_norm(10**6), 1.0)
        e = {"u": "nginx.service", "p": 3, "m": "boom"}
        self.assertTrue(hringr.match_query(e, "level:err unit:ngin -zzz"))
        self.assertFalse(hringr.match_query(e, "level:emerg"))
        self.assertFalse(hringr.match_query(e, "-boom"))

    def test_real_system(self):
        self.assertTrue(0.0 <= hringr.read_ram() <= 1.0)
        self.assertGreaterEqual(hringr.read_swap(), 0)
        self.assertGreater(hringr.read_uptime(), 0)


class Shared(unittest.TestCase):
    def test_no_constants_duplicated_in_html(self):
        html = (ROOT / "hringr.html").read_text()
        self.assertNotIn("const WAVES", html)
        self.assertNotIn("const TAB_N", html)

    def test_shader_table_size_matches_params(self):
        self.assertIn(f"const int N = {hringr.TAB_N};", hringr.RING_FS)
        self.assertIn(f"const int N = {hringr.TAB_N};", (ROOT / "hringr.html").read_text())


class JournalParse(unittest.TestCase):
    def test_full_and_missing_fields(self):
        e = journal_stream.parse(b'{"MESSAGE":"ciao  mondo","PRIORITY":"3","_SYSTEMD_UNIT":"a.service","__REALTIME_TIMESTAMP":"2000000"}', 50)
        self.assertEqual((e["u"], e["p"], e["m"], e["t"]), ("a.service", 3, "ciao mondo", 2.0))
        e = journal_stream.parse(b'{"MESSAGE":null,"SYSLOG_IDENTIFIER":"x"}', 50)   # campo troppo lungo / priorità assente
        self.assertEqual((e["u"], e["p"]), ("x", 6))
        self.assertEqual(journal_stream.parse(b'{}', 50)["u"], "(unknown)")

    def test_garbage_binary_and_clamp(self):
        self.assertIsNone(journal_stream.parse(b"non json", 50))
        self.assertIsNone(journal_stream.parse(b"[1]", 50))
        self.assertEqual(journal_stream.parse(b'{"MESSAGE":[104,105],"PRIORITY":"99"}', 50)["m"], "hi")
        self.assertEqual(len(journal_stream.parse(b'{"MESSAGE":"' + b"x" * 500 + b'"}', 50)["m"]), 50)

    def test_oversized_line_is_skipped(self):
        j = journal_stream.Journal()
        big = b'{"MESSAGE":"' + b"x" * (journal_stream.MAX_LINE + 10) + b'"}\n{"MESSAGE":"ok"}\n'
        j._read(io.BytesIO(big))
        self.assertEqual([e["m"] for e in j.buf], ["ok"])

    def test_buffer_is_bounded_and_counts_exact(self):
        j = journal_stream.Journal(buffer=5)
        for _ in range(20):
            j._add({"p": 4})
        self.assertEqual((len(j.buf), j.counts[4]), (5, 20))


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

    def test_journal_forbidden_for_non_local_host(self):
        saved = hringr_web.journal, hringr_web.args.__dict__.get("jcfg")
        hringr_web.journal, hringr_web.args.jcfg = journal_stream.Journal(), {"network": "no"}
        try:
            req = urllib.request.Request(self.base + "/journal", headers={"Host": "192.168.1.5"})
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(req)
            self.assertEqual(c.exception.code, 403)
        finally:
            hringr_web.journal, hringr_web.args.jcfg = saved

    def test_journal_config_validation(self):
        cp = configparser.ConfigParser()
        cp.read_string("[journal]\ncolors = FF0000\n")
        with self.assertRaises(SystemExit):
            hringr_web.journal_cfg(cp)
        self.assertEqual(len(hringr_web.journal_cfg(configparser.ConfigParser())["colors"]), 8)

    def test_fonts_served_but_no_path_traversal(self):
        r = urllib.request.urlopen(self.base + "/fonts/ChakraPetch-Regular.ttf")
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, "font/ttf"))
        for bad in ("/fonts/../hringr.py", "/fonts/..%2fhringr.py", "/fonts/OFL.txt", "/fonts/nope.ttf"):
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(self.base + bad)
            self.assertEqual(c.exception.code, 404, bad)

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


class Tailnet(unittest.TestCase):
    def test_tailnet_ranges(self):
        self.assertTrue(hringr_web.in_tailnet("100.101.2.3"))
        self.assertTrue(hringr_web.in_tailnet("fd7a:115c:a1e0::1"))
        self.assertFalse(hringr_web.in_tailnet("192.168.1.5"))
