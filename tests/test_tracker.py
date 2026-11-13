"""Tests de claude-spend (solo librería estándar): python3 -m unittest discover -s tests

Todos usan un directorio personal temporal (HOME y USERPROFILE, que es lo que mira Python en
Windows), así que nunca leen ni escriben en ~/.claude. Base.setUp lo comprueba antes de escribir nada.
"""
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACKER = ROOT / "tracker.py"
PRICES = ROOT / "prices.json"


HOME_VARS = ("HOME", "USERPROFILE")  # Linux/macOS leen HOME; Windows, USERPROFILE


def home_env(home):
    return {name: str(home) for name in HOME_VARS}


def load_tracker(home):
    """Importa tracker.py con el directorio personal = home (las rutas se calculan al importar)."""
    old = {name: os.environ.get(name) for name in HOME_VARS}
    os.environ.update(home_env(home))
    try:
        spec = importlib.util.spec_from_file_location(f"tracker_{abs(hash(str(home)))}", TRACKER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return mod


def usage(inp=0, out=0, cr=0, w5=0, w1=0, requests=1):
    return {"input": inp, "output": out, "cache_read": cr, "cache_write_5m": w5, "cache_write_1h": w1,
            "requests": requests}


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="claude-spend-test-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.tr = load_tracker(self.home)
        # red de seguridad: si el directorio personal no se aísla, los tests no deben escribir nada
        home, base = os.path.realpath(self.home), os.path.realpath(self.tr.BASE)
        if os.path.commonpath([home, base]) != home:
            self.fail(f"the tests would use {base} instead of a temporary directory; aborting")
        self.base = Path(self.home) / ".claude" / "token-tracker"
        self.base.mkdir(parents=True)
        shutil.copy(PRICES, self.base / "prices.json")

    def write_log(self, records):
        with open(self.base / "token-log.jsonl", "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def run_cli(self, *args, stdin="", env=None):
        env = {**os.environ, **home_env(self.home), **(env or {})}
        return subprocess.run([sys.executable, str(TRACKER), *args], input=stdin, capture_output=True,
                              text=True, env=env)

    def write_transcript(self, entries):
        path = Path(self.home) / "transcript.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return path


def assistant(i, model, **tokens):
    u = {"input_tokens": tokens.get("inp", 0), "output_tokens": tokens.get("out", 0),
         "cache_read_input_tokens": tokens.get("cr", 0)}
    return {"type": "assistant", "uuid": f"u{i}", "cwd": "/work/demo", "timestamp": f"2026-11-13T10:0{i}:00.000Z",
            "message": {"id": f"m{i}", "model": model, "usage": u}}


class SnapshotTests(Base):
    """El log guarda "fotos" acumuladas; el informe se queda con una por (sesión, agente)."""

    def snap(self, requests, agent_id="main", agent="main", ts="2026-11-02T10:00:00+01:00"):
        return {"ts": ts, "event": "Stop", "session_id": "s1", "project": "/work/demo", "agent_id": agent_id,
                "agent": agent, "task": None, "title": "Hello", "start": "2026-11-01T09:00:00Z",
                "end": "2026-11-01T10:00:00Z",
                "models": {"claude-opus-5-5": usage(1000 * requests, 100, requests=requests)}, "cost_usd": 0}

    def test_the_fullest_snapshot_wins_and_repeats_are_not_counted_twice(self):
        self.write_log([self.snap(2), self.snap(10), self.snap(1)])
        recs = self.tr.latest_records()
        self.assertEqual(len(recs), 1)  # una sola foto por (sesión, agente)
        self.assertEqual(recs[0]["models"]["claude-opus-5-5"]["requests"], 10)  # gana la más completa

    def test_on_a_tie_the_latest_snapshot_wins(self):
        self.write_log([self.snap(3, ts="2026-11-02T10:00:00+01:00"), self.snap(3, ts="2026-11-03T10:00:00+01:00")])
        self.assertEqual(self.tr.latest_records()[0]["ts"], "2026-11-03T10:00:00+01:00")

    def test_different_agents_of_the_same_session_are_kept_apart(self):
        self.write_log([self.snap(5), self.snap(7, agent_id="a1", agent="tester")])
        self.assertEqual(sorted(r["agent"] for r in self.tr.latest_records()), ["main", "tester"])

    def test_broken_lines_in_the_log_are_skipped(self):
        self.write_log([self.snap(4)])
        with open(self.base / "token-log.jsonl", "a", encoding="utf-8") as f:
            f.write("this is not json\n")
        self.assertEqual(len(self.tr.latest_records()), 1)


class CostTests(Base):
    def test_tier_price_applies_per_request(self):
        t = self.write_transcript([assistant(1, "claude-haiku-5-5", inp=150000, out=1000),
                                   assistant(2, "claude-haiku-5-5", inp=1000, out=1000)])
        models, cwd, first, last, title = self.tr.parse_transcript(t, prices=self.tr.load_prices())
        h = models["claude-haiku-5-5"]
        self.assertEqual(h["requests"], 2)
        self.assertEqual(h["tiers"]["100000"]["input"], 150000)  # solo la petición larga está en el tramo
        total, unknown = self.tr.cost(models, self.tr.load_prices())
        self.assertEqual(unknown, [])
        # 150000 in x0.50 + 1000 out x2.50 (tramo)  +  1000 in x0.10 + 1000 out x0.50 (base), por millón
        self.assertAlmostEqual(total, 0.0775 + 0.0006, places=6)
        self.assertEqual(cwd, "/work/demo")

    def test_session_title_priority(self):
        t = self.write_transcript([assistant(1, "claude-opus-5-5", inp=1),
                                   {"type": "last-prompt", "lastPrompt": "last prompt"},
                                   {"type": "ai-title", "aiTitle": "AI title"}])
        self.assertEqual(self.tr.parse_transcript(t)[4], "AI title")
        t = self.write_transcript([assistant(1, "claude-opus-5-5", inp=1),
                                   {"type": "ai-title", "aiTitle": "AI title"},
                                   {"type": "custom-title", "customTitle": "Mine"}])
        self.assertEqual(self.tr.parse_transcript(t)[4], "Mine")

    def test_price_for_matches_variants_but_not_other_versions(self):
        p = self.tr.load_prices()
        self.assertIs(self.tr.price_for("claude-opus-5-5-20261101", p), p["claude-opus-5-5"])
        self.assertIs(self.tr.price_for("claude-sonnet-5-5[1m]", p), p["claude-sonnet-5-5"])
        # "claude-fable-5-3" no es una variante de "claude-fable-5": mejor sin precio que un precio ajeno
        self.assertIsNone(self.tr.price_for("claude-fable-5-3", p))

    def test_unpriced_model_costs_zero_and_is_reported(self):
        total, unknown = self.tr.cost({"some-new-model": usage(1_000_000)}, self.tr.load_prices())
        self.assertEqual((total, unknown), (0.0, ["some-new-model"]))


def record(session, agent_id, agent, models, start="2026-11-01T09:00:00Z", end="2026-11-01T10:00:00Z", **extra):
    return {"ts": end, "event": "Stop", "session_id": session, "project": "/work/demo", "agent_id": agent_id,
            "agent": agent, "task": extra.get("task"), "title": extra.get("title"), "start": start, "end": end,
            "models": models, "cost_usd": 0}


class TableTests(Base):
    def setUp(self):
        super().setUp()
        self.prices = self.tr.load_prices()
        self.records = [
            record("aaaaaaaa-1", "main", "main", {"claude-opus-5-5": usage(1_000_000, 0)}, title="Zeta"),
            record("bbbbbbbb-2", "x1", "tester", {"claude-opus-5-5": usage(2_000_000, 0)}, end="2026-11-03T10:00:00Z"),
            record("cccccccc-3", "main", "main", {"claude-sonnet-5-5": usage(500_000, 0)}, title="Alpha"),
        ]

    def test_default_order_is_usd_descending_and_day_is_ascending(self):
        agents = self.tr.build_table("agent", self.records, self.prices)
        self.assertEqual(agents["default_sort"], [len(agents["cols"]) - 1, True])
        rows = self.tr.sort_rows(agents, *agents["default_sort"])
        self.assertEqual([r[0]["text"] for r in rows], ["tester", "main"])
        days = self.tr.build_table("day", list(reversed(self.records)), self.prices)
        self.assertEqual(days["default_sort"], [0, False])
        rows = self.tr.sort_rows(days, *days["default_sort"])
        self.assertEqual([r[0]["text"] for r in rows], ["2026-11-01", "2026-11-03"])

    def test_total_row_adds_up_and_has_no_runs(self):
        t = self.tr.build_table("agent", self.records, self.prices)
        self.assertEqual(t["total"][0]["text"], "TOTAL")
        self.assertEqual(t["total"][1]["text"], "")  # sin recuento de ejecuciones
        usd = sum(r[-1]["sort"] for r in t["rows"])
        self.assertAlmostEqual(float(t["total"][-1]["text"]), usd, places=2)

    def test_session_table_has_date_and_title_columns(self):
        t = self.tr.build_table("session", self.records, self.prices)
        self.assertEqual([c["name"] for c in t["cols"]][:3], ["Session", "Date", "Title"])
        by_id = {r[0]["text"]: r for r in t["rows"]}
        self.assertEqual(by_id["aaaaaaaa"][2]["text"], "Zeta")
        self.assertEqual(by_id["bbbbbbbb"][2]["text"], "-")  # sin título
        self.assertIsNone(by_id["bbbbbbbb"][2]["sort"])
        self.assertIn("→", by_id["bbbbbbbb"][1]["text"])  # termina otro día

    def test_sort_rows_puts_missing_values_last_in_both_directions(self):
        t = self.tr.build_table("session", self.records, self.prices)
        title = 2
        for desc in (False, True):
            rows = self.tr.sort_rows(t, title, desc)
            self.assertEqual(rows[-1][0]["text"], "bbbbbbbb")  # el que no tiene título
        self.assertEqual([r[title]["text"] for r in self.tr.sort_rows(t, title, False)][:2], ["Alpha", "Zeta"])

    def test_sort_is_stable_and_numeric(self):
        t = self.tr.build_table("agent", self.records, self.prices)
        inp = [c["id"] for c in t["cols"]].index("input")
        rows = self.tr.sort_rows(t, inp, True)
        values = [r[inp]["sort"] for r in rows]
        self.assertEqual(values, sorted(values, reverse=True))  # 2.0M antes que 1.5M: no es orden de texto

    def test_next_sort_toggles_and_uses_natural_first_direction(self):
        t = self.tr.build_table("session", self.records, self.prices)
        self.assertEqual(self.tr.next_sort(t, (0, False), 0), (0, True))
        self.assertEqual(self.tr.next_sort(t, (0, False), 2), (2, False))  # texto: ascendente primero
        self.assertEqual(self.tr.next_sort(t, (0, False), 1), (1, True))  # fecha: descendente primero
        self.assertEqual(self.tr.next_sort(t, (0, False), len(t["cols"]) - 1), (len(t["cols"]) - 1, True))

    def test_parse_sort(self):
        self.assertEqual(self.tr.parse_sort("usd"), ("usd", None))
        self.assertEqual(self.tr.parse_sort("Cache-Read:ASC"), ("cache_read", False))
        self.assertEqual(self.tr.parse_sort("date:desc"), ("date", True))
        for bad in ("bogus", "usd:up", ""):
            with self.assertRaises(ValueError):
                self.tr.parse_sort(bad)

    def test_sort_for_falls_back_to_default_when_the_table_lacks_the_column(self):
        t = self.tr.build_table("agent", self.records, self.prices)
        self.assertEqual(self.tr.sort_for(t, ("date", None)), tuple(t["default_sort"]))
        self.assertEqual(self.tr.sort_for(t, ("agent", False)), (0, False))  # la columna de grupo responde a su nombre


class CliTests(Base):
    def test_sort_flag_orders_the_printed_table(self):
        self.write_log([record("s1", "main", "main", {"claude-opus-5-5": usage(1_000_000)}),
                        record("s2", "a1", "tester", {"claude-opus-5-5": usage(3_000_000)})])
        desc = self.run_cli("--by", "agent").stdout.splitlines()
        asc = self.run_cli("--by", "agent", "--sort", "usd:asc").stdout.splitlines()
        self.assertTrue(desc[3].startswith("tester"))
        self.assertTrue(asc[3].startswith("main"))
        self.assertTrue(asc[-5].startswith("TOTAL") or any(l.startswith("TOTAL") for l in asc))

    def test_invalid_sort_exits_with_an_error(self):
        out = self.run_cli("--sort", "bogus")
        self.assertEqual(out.returncode, 2)
        self.assertIn("invalid --sort", out.stderr)

    def test_sort_on_a_column_no_selected_table_has_is_an_error(self):
        self.write_log([record("s1", "main", "main", {"claude-opus-5-5": usage(1)})])
        self.assertEqual(self.run_cli("--by", "agent", "--sort", "title").returncode, 2)

    def test_no_records_message(self):
        out = self.run_cli()
        self.assertEqual(out.returncode, 0)
        self.assertIn("No records", out.stdout)

    def test_interactive_without_a_terminal_prints_the_plain_report(self):
        self.write_log([record("s1", "main", "main", {"claude-opus-5-5": usage(1)})])
        out = self.run_cli("--interactive", "--by", "agent")
        self.assertEqual(out.returncode, 0)
        self.assertIn("needs a terminal", out.stderr)
        self.assertIn("== Spend by agent", out.stdout)

    def test_since_and_project_filters(self):
        self.write_log([record("s1", "main", "main", {"claude-opus-5-5": usage(1)}, end="2026-10-01T10:00:00Z"),
                        record("s2", "main", "main", {"claude-opus-5-5": usage(1)}, end="2026-11-05T10:00:00Z")])
        out = self.run_cli("--by", "session", "--since", "2026-11-01").stdout
        self.assertIn("s2", out)
        self.assertNotIn("s1  ", out)
        self.assertIn("No records", self.run_cli("--project", "does-not-exist").stdout)


class PlatformTests(Base):
    def test_project_name_handles_unix_and_windows_paths(self):
        for path, expected in [("/home/me/work/demo", "demo"), ("/home/me/work/demo/", "demo"),
                               ("C:\\Users\\me\\proj", "proj"), ("C:\\Users\\me\\proj\\", "proj"),
                               ("C:/Users/me/proj", "proj"), ("/", "?"), ("", "?"), (None, "?")]:
            self.assertEqual(self.tr.project_name(path), expected, path)

    def test_interactive_without_curses_falls_back_to_the_plain_report(self):
        # en Windows `import curses` falla; se simula con un módulo curses que no se puede importar
        fake = Path(self.home) / "fake"
        fake.mkdir()
        (fake / "curses.py").write_text("raise ImportError('No module named _curses')\n")
        self.write_log([record("s1", "main", "main", {"claude-opus-5-5": usage(1)})])
        out = self.run_cli("--interactive", "--by", "agent", env={"PYTHONPATH": str(fake)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("not available here", out.stderr)
        self.assertIn("== Spend by agent", out.stdout)

    def test_output_survives_a_console_that_cannot_encode_the_symbols(self):
        # la fila de sesión usa "→" y el aviso de modelos sin precio usa "⚠"
        self.write_log([record("s1", "main", "main", {"unpriced-model": usage(1)},
                               start="2026-11-01T09:00:00Z", end="2026-11-03T09:00:00Z")])
        out = self.run_cli("--by", "session", env={"PYTHONIOENCODING": "ascii"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Models without a price", out.stdout)


class HookTests(Base):
    def test_stop_hook_writes_a_record_with_english_keys(self):
        t = self.write_transcript([assistant(1, "claude-haiku-5-5", inp=150000, out=1000),
                                   {"type": "ai-title", "aiTitle": "Demo session"}])
        payload = json.dumps({"hook_event_name": "Stop", "session_id": "abcdef12-0000",
                              "transcript_path": str(t), "cwd": "/work/demo"})
        self.assertEqual(self.run_cli("hook", stdin=payload).returncode, 0)
        lines = (self.base / "token-log.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        r = json.loads(lines[0])
        for key in ("event", "project", "agent", "agent_id", "start", "end", "models", "cost_usd", "title"):
            self.assertIn(key, r)
        self.assertEqual((r["agent"], r["agent_id"], r["title"]), ("main", "main", "Demo session"))
        self.assertAlmostEqual(r["cost_usd"], 0.0775, places=6)
        self.assertIn("requests", r["models"]["claude-haiku-5-5"])

    def test_a_broken_hook_never_fails_and_logs_the_error(self):
        out = self.run_cli("hook", stdin="not json")
        self.assertEqual(out.returncode, 0)
        self.assertTrue((self.base / "errors.log").exists())


class WebTests(Base):
    def setUp(self):
        super().setUp()
        self.write_log([record("s1", "main", "main", {"claude-opus-5-5": usage(1_000_000)}, title="Demo"),
                        record("s2", "a1", "tester", {"claude-opus-5-5": usage(2_000_000)})])
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.tr.Handler)
        self.port = self.server.server_address[1]
        self.server.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, host=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        return resp.status, body

    def test_page_and_report_api(self):
        status, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Claude Spend", html)
        self.assertNotIn("innerHTML", html)  # los títulos vienen de los transcripts: solo textContent
        status, body = self.get("/api/report")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(data["empty"])
        self.assertEqual([t["by"] for t in data["tables"]], list(self.tr.GROUPS))

    def test_filters_and_validation(self):
        status, body = self.get("/api/report?project=nothing-matches")
        self.assertTrue(json.loads(body)["empty"])
        self.assertEqual(self.get("/api/report?since=abc")[0], 400)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(self.get("/api/report", host="evil.example:80")[0], 403)
        self.assertEqual(self.get("/api/report", host=f"localhost:{self.port}")[0], 200)

    def test_only_get_is_served(self):
        self.assertEqual(self.get("/api/report", method="POST")[0], 501)


if __name__ == "__main__":
    unittest.main()
