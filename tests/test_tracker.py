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
TRACKER = ROOT / "claude_spend" / "tracker.py"
PRICES = ROOT / "claude_spend" / "prices.json"


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
        self.base = Path(self.home) / ".claude" / "claude-spend"
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
        self.assertNotIn("tiers", h)  # los tramos van dentro de cada día
        self.assertEqual(h["days"]["2026-11-13"]["tiers"]["100000"]["input"], 150000)  # solo la petición larga
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
        self.assertIs(self.tr.price_for("claude-opus-5-5-20261101", p), p["claude-opus-5-5"][0])
        self.assertIs(self.tr.price_for("claude-sonnet-5-5[1m]", p), p["claude-sonnet-5-5"][0])
        # "claude-fable-5-3" no es una variante de "claude-fable-5": mejor sin precio que un precio ajeno
        self.assertIsNone(self.tr.price_for("claude-fable-5-3", p))

    def test_the_bundled_prices_are_used_when_the_user_has_no_prices_json(self):
        # instalación con pip/uv: nadie ha copiado prices.json a ~/.claude/claude-spend
        (self.base / "prices.json").unlink()
        self.assertEqual(self.tr.prices_path(), self.tr.BUNDLED_PRICES)
        self.assertIn("claude-opus-5-5", self.tr.load_prices())
        self.assertIn("List prices as of", self.tr.prices_note())

    def test_the_users_prices_json_takes_precedence_over_the_bundled_one(self):
        (self.base / "prices.json").write_text(
            json.dumps({"_price_date": "2026-01-01", "my-model": {"input": 1.0, "output": 2.0}}), encoding="utf-8")
        self.assertEqual(self.tr.prices_path(), self.tr.PRICES)
        self.assertEqual(list(self.tr.load_prices()), ["my-model"])

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
        # sin terminal se avisa de eso; en Windows (sin curses) se avisa antes de que no está disponible
        self.assertRegex(out.stderr, "needs a terminal|not available here")
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
        lines = (self.base / "token-log.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        r = json.loads(lines[0])
        for key in ("event", "project", "agent", "agent_id", "start", "end", "models", "cost_usd", "title"):
            self.assertIn(key, r)
        self.assertEqual((r["agent"], r["agent_id"], r["title"]), ("main", "main", "Demo session"))
        self.assertAlmostEqual(r["cost_usd"], 0.0775, places=6)
        self.assertIn("requests", r["models"]["claude-haiku-5-5"])

    def test_a_session_started_with_an_agent_is_recorded_under_that_agent(self):
        t = self.write_transcript([{"type": "agent-setting", "agentSetting": "cto"},
                                   assistant(1, "claude-haiku-5-5", inp=1000, out=10)])
        payload = json.dumps({"hook_event_name": "Stop", "session_id": "abcdef12-0001",
                              "transcript_path": str(t), "cwd": "/work/demo"})
        self.assertEqual(self.run_cli("hook", stdin=payload).returncode, 0)
        r = json.loads((self.base / "token-log.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((r["agent"], r["agent_id"]), ("cto", "main"))

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
        self.assertIn(f"v{self.tr.__version__}", html)
        self.assertNotIn("__", html)  # todos los marcadores sustituidos
        status, js = self.get("/main.js")
        self.assertEqual(status, 200)
        self.assertNotIn("innerHTML", js)  # los títulos vienen de los transcripts: solo textContent
        self.assertEqual(self.get("/styles.css")[0], 200)
        for path in ("/tracker.py", "/../tracker.py", "/web/main.js", "/index.html"):
            self.assertEqual(self.get(path)[0], 404, path)
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

    def test_row_filters_keep_only_the_clicked_row(self):
        self.write_log([
            record("s1", "main", "cto", {"claude-opus-5-5": usage(1_000_000), "claude-haiku-5-5": usage(1_000_000)}),
            record("s1", "a1", "tester", {"claude-opus-5-5": usage(2_000_000)}, task="check"),
            record("s2", "main", "main", {"claude-haiku-5-5": usage(4_000_000)}),
        ])
        before = self.tr.latest_records()
        by_agent = self.tr.filtered_records(filters={"agent": "cto"})
        self.assertEqual([(r["session_id"], r["agent"]) for r in by_agent], [("s1", "cto")])
        # un modelo: solo cuenta su parte de cada registro, y el registro original no se toca
        by_model = self.tr.filtered_records(filters={"model": "claude-opus-5-5"})
        self.assertEqual(sorted(r["agent"] for r in by_model), ["cto", "tester"])
        self.assertTrue(all(list(r["models"]) == ["claude-opus-5-5"] for r in by_model))
        self.assertEqual(self.tr.latest_records(), before)
        both = self.tr.filtered_records(filters={"session": "s1", "model": "claude-haiku-5-5"})
        self.assertEqual([r["agent"] for r in both], ["cto"])
        self.assertEqual(self.tr.filtered_records(filters={"session": "s1", "agent": "main"}), [])

    def test_report_api_filters_by_row(self):
        data = json.loads(self.get("/api/report?f_agent=tester&f_nonsense=1")[1])
        agents = next(t for t in data["tables"] if t["by"] == "agent")
        self.assertEqual([row[0]["sort"] for row in agents["rows"]], ["tester"])
        usd = [c["id"] for c in agents["cols"]].index("usd")
        everything = next(t for t in json.loads(self.get("/api/report")[1])["tables"] if t["by"] == "agent")
        self.assertLess(agents["total"][usd]["sort"], everything["total"][usd]["sort"])
        data = json.loads(self.get("/api/report?f_agent=nobody")[1])
        self.assertEqual((data["empty"], data["message"]), (True, self.tr.NO_MATCH))

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(self.get("/api/report", host="evil.example:80")[0], 403)
        self.assertEqual(self.get("/api/report", host=f"localhost:{self.port}")[0], 200)

    def test_only_get_is_served(self):
        self.assertEqual(self.get("/api/report", method="POST")[0], 501)


def price(inp, **extra):
    return {"input": inp, "output": 0.0, "cache_read": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0, **extra}


def request(i, ts, model="m", inp=0, out=0):
    e = {"type": "assistant", "uuid": f"u{i}", "cwd": "/work/demo",
         "message": {"id": f"m{i}", "model": model, "usage": {"input_tokens": inp, "output_tokens": out}}}
    if ts:
        e["timestamp"] = ts
    return e


NOV, DEC = "2026-11-30T23:30:00.000Z", "2026-12-01T00:30:00.000Z"  # a ambos lados del cambio de precio


class PricePeriodTests(Base):
    """Precios con vigencia: el modelo "m" cuesta 1.0 hasta el 2026-11-30 y 2.0 desde el 2026-12-01."""

    def write_prices(self, prices):
        (self.base / "prices.json").write_text(json.dumps({"_price_date": "2026-11-11", **prices}), encoding="utf-8")
        return self.tr.load_prices()

    def two_periods(self, **second):
        return self.write_prices({"m": [price(1.0), {"from": "2026-12-01", **price(2.0), **second}]})

    def test_normalize_prices(self):
        norm = self.tr.normalize_prices
        prices, problems = norm({"_x": "ignored", "flat": price(1.0),
                                 "unsorted": [{"from": "2026-12-01", **price(2.0)}, price(1.0)]})
        self.assertEqual(problems, [])
        self.assertEqual(list(prices), ["flat", "unsorted"])
        self.assertEqual(prices["flat"], [{"from": None, **price(1.0)}])
        self.assertEqual([p["from"] for p in prices["unsorted"]], [None, "2026-12-01"])
        bad = {"dup": [{"from": "2026-12-01", **price(1.0)}, {"from": "2026-12-01", **price(2.0)}],
               "month13": [{"from": "2026-13-01", **price(1.0)}],
               "two_open": [price(1.0), price(2.0)],
               "empty": [],
               "weird": None, "number": 3, "text": "abc", "item": [price(1.0), 5],
               "from_number": [{"from": 20261201, **price(1.0)}]}
        prices, problems = norm(bad)
        self.assertEqual(prices, {})
        for model in bad:
            self.assertTrue(any(f" {model} ignored" in p for p in problems), model)
        self.assertTrue(any("invalid from '2026-13-01'" in p for p in problems))
        for raw in (None, [], "x", 3):
            self.assertEqual(norm(raw)[0], {})  # nunca lanza

    def test_period_for(self):
        periods = self.two_periods()["m"]
        self.assertEqual(self.tr.period_for(periods, "2026-11-30")["input"], 1.0)
        self.assertEqual(self.tr.period_for(periods, "2026-12-01")["input"], 2.0)  # inclusivo
        self.assertEqual(self.tr.period_for(periods, "2030-01-01")["input"], 2.0)
        self.assertEqual(self.tr.period_for(periods, "1990-01-01")["input"], 1.0)  # sin "from": desde siempre
        later = self.tr.normalize_prices({"m": [{"from": "2026-06-01", **price(1.0)},
                                                {"from": "2026-12-01", **price(2.0)}]})[0]["m"]
        # antes del primer "from" se aplica el periodo más antiguo (decisión del CTO)
        self.assertEqual(self.tr.period_for(later, "2026-05-31")["input"], 1.0)
        self.assertIsNone(self.tr.period_for([], "2026-05-31"))

    def test_price_for_a_variant_on_a_date(self):
        prices = self.two_periods()
        self.assertEqual(self.tr.price_for("m-20261101", prices, "2026-12-05")["input"], 2.0)
        self.assertEqual(self.tr.price_for("m-20261101", prices, "2026-11-05")["input"], 1.0)

    def test_utc_day(self):
        self.assertEqual(self.tr.utc_day("2026-11-30T23:30:00.000Z"), "2026-11-30")
        self.assertEqual(self.tr.utc_day("2026-11-30T20:00:00-05:00"), "2026-12-01")
        self.assertEqual(self.tr.utc_day("2026-11-30T23:30:00"), "2026-11-30")  # sin zona = UTC
        for bad in ("basura", None, "", 3):
            self.assertIsNone(self.tr.utc_day(bad))

    def test_parse_transcript_splits_by_utc_day(self):
        t = self.write_transcript([request(1, NOV, inp=100, out=1), request(2, DEC, inp=30, out=2)])
        m = self.tr.parse_transcript(t, prices=self.two_periods())[0]["m"]
        self.assertEqual(sorted(m["days"]), ["2026-11-30", "2026-12-01"])
        self.assertEqual(m["days"]["2026-11-30"]["input"], 100)
        self.assertEqual(m["days"]["2026-12-01"]["input"], 30)
        for k in self.tr.FIELDS + ("requests",):
            self.assertEqual(sum(d[k] for d in m["days"].values()), m[k], k)

    def test_a_request_without_timestamp_goes_to_the_last_day_seen(self):
        t = self.write_transcript([request(1, NOV, inp=1), request(2, DEC, inp=2), request(3, None, inp=4)])
        days = self.tr.parse_transcript(t, prices=self.two_periods())[0]["m"]["days"]
        self.assertEqual(days["2026-12-01"]["input"], 6)

    def test_the_tier_is_decided_with_the_period_of_the_day(self):
        prices = self.two_periods(tiers=[{"prompt_over": 100000, **price(3.0)}])
        t = self.write_transcript([request(1, NOV, inp=150000), request(2, DEC, inp=150000)])
        m = self.tr.parse_transcript(t, prices=prices)[0]["m"]
        self.assertNotIn("tiers", m["days"]["2026-11-30"])
        self.assertEqual(m["days"]["2026-12-01"]["tiers"]["100000"]["input"], 150000)
        self.assertAlmostEqual(self.tr.cost({"m": m}, prices)[0], 0.15 + 0.45, places=6)

    def test_cost_charges_each_day_with_its_period(self):
        prices = self.two_periods()
        t = {**usage(2_000_000, requests=2),
             "days": {"2026-11-30": usage(1_000_000), "2026-12-01": usage(1_000_000)}}
        self.assertEqual(self.tr.cost({"m": t}, prices), (3.0, []))

    def test_old_record_without_days_uses_the_given_day(self):
        prices = self.two_periods()
        self.assertEqual(self.tr.cost({"m": usage(1_000_000)}, prices, "2026-11-30"), (1.0, []))
        self.assertEqual(self.tr.cost({"m": usage(1_000_000)}, prices, "2026-12-01"), (2.0, []))
        self.assertEqual(self.tr.record_day({"end": "2026-11-30T20:00:00-05:00", "ts": "x"}), "2026-12-01")
        self.assertEqual(self.tr.record_day({"end": None, "ts": "2026-11-29T10:00:00+01:00"}), "2026-11-29")

    def test_adding_a_new_period_does_not_change_the_cost_of_earlier_usage(self):
        # requisito del cliente: el coste pasado no cambia al añadir un precio nuevo con su "from"
        nov = [record("s1", "main", "main", {"m": usage(1_000_000)}, end="2026-11-20T10:00:00Z"),
               record("s2", "main", "main", {"m": {**usage(1_000_000), "days": {"2026-11-25": usage(1_000_000)}}},
                      end="2026-11-25T10:00:00Z")]
        dec = [record("s3", "main", "main", {"m": {**usage(1_000_000), "days": {"2026-12-02": usage(1_000_000)}}},
                      end="2026-12-02T10:00:00Z")]

        def usd(records, prices):
            return self.tr.build_table("model", records, prices)["total"][-1]["sort"]

        before = self.write_prices({"m": price(1.0)})
        nov_before, dec_before = usd(nov, before), usd(dec, before)
        after = self.two_periods()
        self.assertEqual((nov_before, dec_before), (2.0, 1.0))
        self.assertEqual(usd(nov, after), nov_before)
        self.assertEqual(usd(dec, after), 2.0)

    def test_report_notes(self):
        prices = self.write_prices({"m": [{"from": "2026-06-01", **price(1.0)}],
                                    "bad": [{"from": "2026-13-01", **price(1.0)}]})
        records = [record("s1", "main", "main", {"m": usage(1_000_000)}, end="2026-05-01T10:00:00Z"),
                   record("s2", "main", "main", {"bad": usage(1)})]
        notes = "\n".join(self.tr.report_notes(records, prices))
        # "m" antes de su primer "from" se cobra con el periodo más antiguo: no es "sin precio"
        self.assertEqual(self.tr.build_table("model", records[:1], prices)["total"][-1]["sort"], 1.0)
        self.assertIn("Models without a price (counted as 0 USD): bad.", notes)
        self.assertIn("prices.json: bad ignored: invalid from '2026-13-01'", notes)

    def test_bundled_flat_prices_give_the_same_usd_with_and_without_days(self):
        prices = self.tr.load_prices()
        u = usage(1_000_000, 50_000, cr=200_000)
        old = record("s1", "main", "main", {"claude-opus-5-5": u})
        new = record("s2", "main", "main", {"claude-opus-5-5": {**u, "days": {"2026-11-01": u}}})
        usd = [self.tr.build_table("model", [r], prices)["total"][-1]["sort"] for r in (old, new)]
        self.assertGreater(usd[0], 0)
        self.assertEqual(usd[0], usd[1])

    def test_hook_records_days_and_the_right_cost(self):
        self.two_periods()
        t = self.write_transcript([request(1, NOV, inp=1_000_000), request(2, DEC, inp=1_000_000)])
        payload = json.dumps({"hook_event_name": "Stop", "session_id": "abcdef12-0002",
                              "transcript_path": str(t), "cwd": "/work/demo"})
        self.assertEqual(self.run_cli("hook", stdin=payload).returncode, 0)
        r = json.loads((self.base / "token-log.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(sorted(r["models"]["m"]["days"]), ["2026-11-30", "2026-12-01"])
        self.assertAlmostEqual(r["cost_usd"], 3.0, places=6)

    def test_import_replaces_an_old_record_without_days(self):
        self.two_periods()
        project = Path(self.home) / ".claude" / "projects" / "demo"
        project.mkdir(parents=True)
        (project / "s1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in
                                                  [request(1, NOV, inp=10), request(2, DEC, inp=20)]),
                                          encoding="utf-8")
        self.write_log([record("s1", "main", "main", {"m": usage(30, requests=2)})])
        self.assertEqual(self.run_cli("import").returncode, 0)
        recs = self.tr.latest_records()
        self.assertEqual(len(recs), 1)
        self.assertIn("days", recs[0]["models"]["m"])


if __name__ == "__main__":
    unittest.main()


class LimitsTests(Base):
    def payload(self, now, five=86, week=64):
        return json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": now + 3600},
            "seven_day": {"used_percentage": week, "resets_at": now + 3 * 86400}}})

    def test_saves_and_loads(self):
        import time
        now = time.time()
        r = self.run_cli("limits", stdin=self.payload(now))
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        limits = self.tr.load_limits(now)
        self.assertEqual([(w["id"], w["pct"]) for w in limits], [("five_hour", 86.0), ("seven_day", 64.0)])
        self.assertTrue(limits[1]["resets"].startswith("Resets "))
        self.assertTrue(limits[0]["updated"].startswith("Updated today at "))
        self.assertAlmostEqual(limits[0]["updated_at"], now, delta=60)

    def test_no_rate_limits_or_bad_json_writes_nothing(self):
        for stdin in ('{"model": {}}', "not json"):
            self.assertEqual(self.run_cli("limits", stdin=stdin).returncode, 0)
        self.assertFalse((self.base / "limits.json").exists())
        self.assertIsNone(self.tr.load_limits())

    def test_expired_window_shows_zero(self):
        import time
        now = time.time()
        self.run_cli("limits", stdin=self.payload(now))
        limits = self.tr.load_limits(now + 2 * 3600)
        self.assertEqual((limits[0]["pct"], limits[0]["resets"]), (0.0, "Not started"))
        self.assertEqual(limits[1]["pct"], 64.0)

    def test_fmt_reset_today_and_other_day(self):
        import datetime as dt
        noon = dt.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        today = noon.replace(hour=13, minute=30).timestamp()
        self.assertEqual(self.tr.fmt_reset(today, noon.timestamp()), "Resets at 13:30")
        other = (noon + dt.timedelta(days=2)).replace(hour=22)
        self.assertEqual(self.tr.fmt_reset(other.timestamp(), noon.timestamp()),
                         f"Resets {other.strftime('%A')}, 22:00")

    def test_report_and_api_include_limits(self):
        import time
        self.run_cli("limits", stdin=self.payload(time.time()))
        self.assertEqual(len(self.tr.report_data()["limits"]), 2)
        out = self.run_cli("report").stdout
        self.assertIn("Current session", out)
        self.assertIn("Updated today at", out)
