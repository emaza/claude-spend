#!/usr/bin/env python3
"""Claude Code token spend, broken down by agent, project, model, day, session and task.

Usage:
  tracker.py hook                  # called by the Stop and SubagentStop hooks (JSON on stdin)
  tracker.py import                # import every transcript found on disk into the log
  tracker.py report [options]      # print the spend tables (the wrapper command is: claude-spend)
  tracker.py --interactive         # tables in the terminal; click a header to sort
  tracker.py --web                 # tables in a local web page; click a header to sort

The log lives in ~/.claude/claude-spend/token-log.jsonl. Each line is a cumulative
"snapshot" of one agent in one session; the report keeps the latest snapshot of every
(session, agent), so repeating records does not duplicate the spend.
"""
import argparse
import datetime as dt
import glob
import http.server
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import webbrowser
from collections import defaultdict

__version__ = "0.2.2"

BASE = os.path.expanduser("~/.claude/claude-spend")
LOG = os.path.join(BASE, "token-log.jsonl")
ERRLOG = os.path.join(BASE, "errors.log")
PRICES = os.path.join(BASE, "prices.json")  # el del usuario: si existe, manda
BUNDLED_PRICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.json")  # el que viene con el paquete
PROJECTS = os.path.expanduser("~/.claude/projects")
FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
MAIN = "main"
GROUPS = ("agent", "project", "model", "day", "session", "task")
# entradas del transcript que dan título a la sesión, de más a menos prioritaria
TITLE_FIELDS = {"custom-title": "customTitle", "ai-title": "aiTitle", "last-prompt": "lastPrompt"}
TITLE_WIDTH = 30
DEFAULT_PORT = 8765
NO_RECORDS = "No records. Run `claude-spend --import` to load the existing transcripts."


# ---------------------------------------------------------------- transcripts
def parse_transcript(path, skip_sidechain=False, prices=None):
    """Suma el uso por modelo de un transcript JSONL (sin contar dos veces un mensaje).

    Si el precio del modelo tiene "tiers" (precio distinto según la longitud del prompt),
    las peticiones que superan un umbral se suman además en t["tiers"][str(umbral)],
    porque el tramo se decide petición a petición y no se puede deducir del total.
    """
    by_id = {}
    cwd = None
    first = last = None
    titles = {}  # el transcript repite estas entradas al renombrar: vale la última de cada tipo
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            cwd = e.get("cwd") or cwd
            if e.get("type") in TITLE_FIELDS:
                titles[e["type"]] = e.get(TITLE_FIELDS[e["type"]])
                continue
            if e.get("type") != "assistant" or (skip_sidechain and e.get("isSidechain")):
                continue
            m = e.get("message") or {}
            u = m.get("usage")
            if not u or m.get("model") in (None, "<synthetic>"):
                continue
            ts = e.get("timestamp")
            if ts:
                first = first or ts
                last = ts
            by_id[m.get("id") or e.get("uuid")] = (m["model"], u)
    models = {}
    for model, u in by_id.values():
        cc = u.get("cache_creation") or {}
        w1h = cc.get("ephemeral_1h_input_tokens") or 0
        w5m = cc.get("ephemeral_5m_input_tokens")
        if w5m is None:
            w5m = (u.get("cache_creation_input_tokens") or 0) - w1h
        req = {
            "input": u.get("input_tokens") or 0,
            "output": u.get("output_tokens") or 0,
            "cache_read": u.get("cache_read_input_tokens") or 0,
            "cache_write_5m": max(w5m, 0),
            "cache_write_1h": w1h,
        }
        buckets = [models.setdefault(model, dict.fromkeys(FIELDS, 0) | {"requests": 0})]
        # longitud del prompt = todos los tokens de entrada, también lecturas y escrituras de caché
        prompt = req["input"] + req["cache_read"] + req["cache_write_5m"] + req["cache_write_1h"]
        tier = tier_for(price_for(model, prices or {}), prompt)
        if tier:
            buckets.append(buckets[0].setdefault("tiers", {}).setdefault(
                str(tier["prompt_over"]), dict.fromkeys(FIELDS, 0) | {"requests": 0}))
        for t in buckets:
            for k in FIELDS:
                t[k] += req[k]
            t["requests"] += 1
    # prioridad: el título que puso el usuario, el generado por Claude, el último mensaje del usuario
    title = next((" ".join(titles[k].split()) for k in TITLE_FIELDS if titles.get(k)), None)
    return models, cwd, first, last, title


def prices_path():
    """prices.json del usuario si existe; si no, el que viene con el paquete (instalación con pip/uv)."""
    return PRICES if os.path.exists(PRICES) else BUNDLED_PRICES


def load_prices():
    try:
        with open(prices_path(), encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {}


def price_for(model, prices):
    if model in prices:
        return prices[model]
    # ids con sufijo (fechas, [1m], -fast…): coincidencia por prefijo más largo.
    # Si lo que sobra empieza por un número de versión corto ("-2" en claude-fable-5-2)
    # es otro modelo, no una variante: mejor "sin precio" que el precio de otro.
    def variant(k):
        return model.startswith(k) and not re.match(r"-\d{1,3}(?!\d)", model[len(k):])
    best = max((k for k in prices if variant(k)), key=len, default=None)
    return prices.get(best)


def tier_for(p, prompt):
    """Tramo de precio que aplica a una petición con `prompt` tokens de entrada, o None.

    Un modelo puede tener varios tiers en prices.json ("tiers": [{"prompt_over": N, ...}]);
    se aplica el de umbral más alto que el prompt supere.
    """
    valid = [t for t in (p or {}).get("tiers", []) if prompt > t.get("prompt_over", float("inf"))]
    return max(valid, key=lambda t: t["prompt_over"], default=None)


def prices_note():
    """Línea para el informe con la fecha de los precios y aviso si son antiguos."""
    meta = read_json(prices_path())
    date = meta.get("_price_date")
    if not date:
        return "Prices: no date in prices.json (add \"_price_date\")."
    line = f"List prices as of {date}"
    try:
        days = (dt.date.today() - dt.date.fromisoformat(date)).days
        line += f" ({days} days ago)"
        if days > 90:
            line += " ⚠ worth reviewing"
    except ValueError:
        pass
    return line + ". Source: " + meta.get("_source", "-")


def cost(models, prices):
    total, unknown = 0.0, []
    for model, t in models.items():
        p = price_for(model, prices)
        if not p:
            unknown.append(model)
            continue
        base = {k: t.get(k, 0) for k in FIELDS}
        for threshold, tt in (t.get("tiers") or {}).items():
            # si el tramo ya no está en prices.json, esos tokens se cobran a precio base
            pt = next((x for x in p.get("tiers", []) if str(x.get("prompt_over")) == threshold), None)
            if not pt:
                continue
            total += sum(tt.get(k, 0) * pt.get(k, 0) for k in FIELDS) / 1_000_000
            for k in FIELDS:
                base[k] -= tt.get(k, 0)
        total += sum(base[k] * p.get(k, 0) for k in FIELDS) / 1_000_000
    return round(total, 6), unknown


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main_agent_name(path):
    """Agente con el que se lanzó la sesión (claude --agent X queda como "agentSetting"), o "main"."""
    name = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"agentSetting"' in line:
                    try:
                        name = json.loads(line).get("agentSetting") or name
                    except ValueError:
                        pass
    except OSError:
        pass
    return name or MAIN


def record_for(transcript, session_id, agent_id, agent_type, description, event, cwd=None):
    prices = load_prices()
    if agent_id == MAIN:
        agent_type = main_agent_name(transcript)  # agent_id sigue siendo "main": es la clave del registro
    models, tcwd, first, last, title = parse_transcript(transcript, skip_sidechain=(agent_id == MAIN), prices=prices)
    if not models:
        return None
    usd, unknown = cost(models, prices)
    rec = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        "session_id": session_id,
        "project": cwd or tcwd,
        "agent_id": agent_id,
        "agent": agent_type,
        "task": description,
        "title": title if agent_id == MAIN else None,  # el título es de la sesión: lo lleva el registro principal
        "start": first,
        "end": last,
        "models": models,
        "cost_usd": usd,
    }
    if unknown:
        rec["models_without_price"] = unknown
    return rec


def subagent_record(transcript, session_id, event, cwd=None, agent_type=None):
    agent_id = os.path.basename(transcript)[len("agent-"):-len(".jsonl")]
    meta = read_json(transcript[: -len(".jsonl")] + ".meta.json")
    return record_for(
        transcript, session_id, agent_id,
        agent_type or meta.get("agentType") or "unknown",
        meta.get("description"), event, cwd,
    )


def append(records):
    os.makedirs(BASE, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        for r in records:
            if r:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------- hook
def cmd_hook(_args):
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name") or "?"
        session = data.get("session_id")
        cwd = data.get("cwd")
        main_t = data.get("transcript_path")
        if event == "SubagentStop":
            t = data.get("agent_transcript_path")
            if not t and data.get("agent_id") and main_t:
                t = os.path.join(main_t[: -len(".jsonl")], "subagents", f"agent-{data['agent_id']}.jsonl")
            if t and os.path.exists(t):
                append([subagent_record(t, session, event, cwd, data.get("agent_type"))])
        elif main_t and os.path.exists(main_t):
            append([record_for(main_t, session, MAIN, MAIN, None, event, cwd)])
    except Exception:  # un hook nunca debe romper la sesión
        os.makedirs(BASE, exist_ok=True)
        with open(ERRLOG, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat()}\n{traceback.format_exc()}\n")
    return 0


# -------------------------------------------------------------------- import
def cmd_import(_args):
    records = []
    for main_t in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        session = os.path.basename(main_t)[: -len(".jsonl")]
        records.append(record_for(main_t, session, MAIN, MAIN, None, "import"))
        for sub in glob.glob(os.path.join(main_t[: -len(".jsonl")], "subagents", "agent-*.jsonl")):
            records.append(subagent_record(sub, session, "import"))
    records = [r for r in records if r]
    append(records)
    print(f"Imported {len(records)} records (sessions and subagents) to {LOG}")
    return 0


# -------------------------------------------------------------------- report
def latest_records():
    latest = {}
    try:
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                key = (r.get("session_id"), r.get("agent_id"))
                old = latest.get(key)
                # nos quedamos con la foto más completa (más peticiones contadas)
                n = sum(m.get("requests", 0) for m in r.get("models", {}).values())
                if not old or n >= old[0]:
                    latest[key] = (n, r)
    except OSError:
        pass
    return [r for _, r in latest.values()]


def filtered_records(since=None, project=None):
    records = []
    for r in latest_records():
        day = (r.get("end") or r.get("ts") or "")[:10]
        if since and day < since:
            continue
        if project and project not in (r.get("project") or ""):
            continue
        records.append(r)
    return records


def human(n):
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def project_name(path):
    """Última carpeta de la ruta del proyecto; vale para rutas con / y con \\ (Windows)."""
    parts = [p for p in re.split(r"[\\/]+", path or "") if p]
    return parts[-1] if parts else "?"


def group_key(r, by):
    if by == "agent":
        return r.get("agent") or "?"
    if by == "project":
        return project_name(r.get("project"))
    if by == "day":
        return (r.get("end") or r.get("ts") or "?")[:10]
    if by == "session":
        return (r.get("session_id") or "?")[:8]
    if by == "task":
        return f"{r.get('agent')}: {r.get('task') or '-'}"[:70]
    if by == "model":
        return None  # se reparte por modelo más abajo
    raise ValueError(by)


def local_dt(ts):
    """Timestamp ISO del transcript (UTC, 'Z') a datetime en la hora local del sistema."""
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()


def session_dates(records):
    """{id corto de sesión: (inicio, fin)} como datetimes locales: min(start) y max(end) de sus registros."""
    spans = {}
    for r in records:
        ini, fin = r.get("start"), r.get("end")
        if not ini or not fin:
            continue
        ini, fin = local_dt(ini), local_dt(fin)
        old = spans.get(group_key(r, "session"))
        spans[group_key(r, "session")] = (min(old[0], ini), max(old[1], fin)) if old else (ini, fin)
    return spans


def fmt_span(span):
    """'AAAA-MM-DD HH:MM' del inicio; si termina otro día, añade ' → AAAA-MM-DD'."""
    if not span:
        return "?"
    ini, fin = span
    text = ini.strftime("%Y-%m-%d %H:%M")
    return text if fin.date() == ini.date() else f"{text} → {fin.strftime('%Y-%m-%d')}"


def session_titles(records):
    """{id corto de sesión: título truncado}; las sesiones registradas antes de existir el campo no tienen."""
    titles = {}
    for r in records:
        if r.get("title"):
            t = r["title"]
            titles[group_key(r, "session")] = t if len(t) <= TITLE_WIDTH else t[: TITLE_WIDTH - 1] + "…"
    return titles


# Una tabla es un dict {by, title, cols, rows, total, default_sort}. Cada celda es
# {"text": lo que se muestra, "sort": valor crudo para ordenar (None = sin valor)}.
# El mismo dict sirve para imprimir, para la TUI y (como JSON) para la web.
def cell(text, sort=None):
    return {"text": text, "sort": sort}


def column(ident, name, kind):
    return {"id": ident, "name": name, "type": kind, "align": "right" if kind == "num" else "left"}


def build_table(by, records, prices):
    rows = defaultdict(lambda: dict.fromkeys(FIELDS, 0) | {"usd": 0.0, "runs": 0})
    for r in records:
        for model, t in r.get("models", {}).items():
            row = rows[model if by == "model" else group_key(r, by)]
            for f in FIELDS:
                row[f] += t.get(f, 0)
            row["usd"] += cost({model: t}, prices)[0]
        if by != "model":
            rows[group_key(r, by)]["runs"] += 1

    cols = [column("group", by.capitalize(), "text")]
    if by == "session":  # columnas Date y Title justo tras la sesión
        spans, titles = session_dates(records), session_titles(records)
        cols += [column("date", "Date", "date"), column("title", "Title", "text")]
    cols += [column("runs", "Runs", "num"), column("input", "Input", "num"), column("output", "Output", "num"),
             column("cache_read", "Cache read", "num"), column("cache_write", "Cache write", "num"),
             column("usd", "USD (est.)", "num")]

    def numbers(v):
        cw = v["cache_write_5m"] + v["cache_write_1h"]
        return [cell(str(v["runs"]) if v["runs"] else "-", v["runs"]),
                cell(human(v["input"]), v["input"]), cell(human(v["output"]), v["output"]),
                cell(human(v["cache_read"]), v["cache_read"]), cell(human(cw), cw),
                cell(f"{v['usd']:.2f}", v["usd"])]

    table_rows = []
    for key, v in rows.items():
        extra = []
        if by == "session":
            span = spans.get(key)
            title = titles.get(key)
            extra = [cell(fmt_span(span), span[0].timestamp() if span else None), cell(title or "-", title)]
        table_rows.append([cell(key, key)] + extra + numbers(v))

    tot = {f: sum(v[f] for v in rows.values()) for f in FIELDS}
    tot["runs"], tot["usd"] = 0, sum(v["usd"] for v in rows.values())
    nums = numbers(tot)
    nums[0] = cell("")  # sin recuento de ejecuciones en el total
    total = [cell("TOTAL")] + [cell("") for _ in range(len(cols) - 7)] + nums
    default = (0, False) if by == "day" else (len(cols) - 1, True)  # day por fecha, el resto por USD
    return {"by": by, "title": f"Spend by {by}", "cols": cols, "rows": table_rows,
            "total": total, "default_sort": list(default)}


def default_desc(col):
    """Sentido de la primera ordenación de una columna: los números y las fechas, de mayor a menor."""
    return col["type"] != "text"


def sort_rows(table, idx, desc):
    """Filas ordenadas por la columna idx (las que no tienen valor van siempre al final). La ordenación es estable."""
    def val(row):
        v = row[idx]["sort"]
        return v.casefold() if isinstance(v, str) else v
    have = [r for r in table["rows"] if r[idx]["sort"] is not None]
    missing = [r for r in table["rows"] if r[idx]["sort"] is None]
    return sorted(have, key=val, reverse=desc) + missing


def next_sort(table, current, idx):
    """Nuevo (columna, desc) al ordenar por idx: la misma columna invierte el sentido."""
    if current[0] == idx:
        return idx, not current[1]
    return idx, default_desc(table["cols"][idx])


def sort_choices():
    return ["group"] + list(GROUPS) + ["date", "title", "runs", "input", "output", "cache_read", "cache_write", "usd"]


def parse_sort(spec):
    """'usd', 'date:asc'… -> (id de columna, desc o None si no se indicó). Lanza ValueError si no es válido."""
    ident, _, direction = spec.partition(":")
    ident = ident.strip().lower().replace("-", "_")
    if ident not in sort_choices() or direction.lower() not in ("", "asc", "desc"):
        raise ValueError(f"invalid --sort '{spec}'; use COLUMN[:asc|desc] with COLUMN one of: "
                         + ", ".join(sort_choices()))
    return ident, (direction.lower() == "desc" if direction else None)


def column_index(table, ident):
    """Posición de la columna `ident` en la tabla (la de grupo también responde a su nombre, p. ej. 'agent'), o None."""
    for i, c in enumerate(table["cols"]):
        if ident == c["id"] or (c["id"] == "group" and ident == table["by"]):
            return i
    return None


def sort_for(table, parsed):
    """(idx, desc) que corresponde a un --sort ya parseado en esta tabla, o el orden por defecto si no tiene esa columna."""
    if parsed:
        i = column_index(table, parsed[0])
        if i is not None:
            return i, default_desc(table["cols"][i]) if parsed[1] is None else parsed[1]
    return tuple(table["default_sort"])


def print_table(table, sort=None):
    idx, desc = sort or table["default_sort"]
    cols = table["cols"]
    head = [c["name"] for c in cols]
    body = [[c["text"] for c in row] for row in sort_rows(table, idx, desc)]
    total = [c["text"] for c in table["total"]]
    widths = [max(len(row[i]) for row in [head] + body + [total]) for i in range(len(head))]
    left = {i for i, c in enumerate(cols) if c["align"] == "left"}  # columnas de texto a la izquierda
    fmt = lambda row: "  ".join(c.ljust(w) if i in left else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths)))
    sep = "  ".join("-" * w for w in widths)
    print(f"== {table['title']}")
    print(fmt(head))
    print(sep)
    for row in body:
        print(fmt(row))
    print(sep)
    print(fmt(total))
    print()


def report_notes(records, prices):
    notes = []
    unpriced = sorted({m for r in records for m in r.get("models", {}) if not price_for(m, prices)})
    if unpriced:
        notes.append("⚠ Models without a price (counted as 0 USD): " + ", ".join(unpriced)
                     + ". Add them to prices.json.")
    notes.append("USD = estimate at list price (prices.json); "
                 "with a Pro/Max subscription it is not what you pay.")
    notes.append(prices_note())
    return notes


def cmd_report(args):
    parsed = None
    if args.sort:
        try:
            parsed = parse_sort(args.sort)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    prices = load_prices()
    records = filtered_records(args.since, args.project)
    if not records:
        print(NO_RECORDS)
        return 0
    by_list = by_list_for(args)
    tables = [build_table(by, records, prices) for by in by_list]
    if parsed and all(column_index(t, parsed[0]) is None for t in tables):
        print(f"--sort {parsed[0]}: none of the selected tables has that column.", file=sys.stderr)
        return 2
    for t in tables:
        print_table(t, sort_for(t, parsed))
    print("\n".join(report_notes(records, prices)))
    return 0


def by_list_for(args):
    by_list = args.by or ["agent", "project"]
    return list(GROUPS) if "all" in by_list else by_list


# --------------------------------------------------------------- interactive
def tui_layout(table):
    """Anchos y posición x de cada columna; la cabecera reserva 2 caracteres para la flecha ▲/▼."""
    cols = table["cols"]
    rows = [[c["text"] for c in r] for r in table["rows"]] + [[c["text"] for c in table["total"]]]
    widths = [max([len(c["name"]) + 2] + [len(r[i]) for r in rows]) for i, c in enumerate(cols)]
    xs, x = [], 0
    for w in widths:
        xs.append(x)
        x += w + 2
    return widths, xs


def column_at(widths, xs, x):
    """Columna bajo la posición x de la pantalla (el hueco entre columnas cuenta para la de la izquierda)."""
    for i in range(len(xs)):
        if xs[i] <= x < xs[i] + widths[i] + 2:
            return i
    return None


def tab_spans(tables):
    """[(x inicial, x final)] de la etiqueta de cada tabla en la fila de pestañas."""
    spans, x = [], 0
    for t in tables:
        label = len(t["cols"][0]["name"]) + 2
        spans.append((x, x + label))
        x += label + 1
    return spans


def tui_main(scr, tables, parsed):
    import curses
    curses.use_default_colors()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    # solo clics y rueda: pedir PRESSED/RELEASED además haría que un clic ordenara varias veces
    wheel_up, wheel_down = curses.BUTTON4_PRESSED, getattr(curses, "BUTTON5_PRESSED", 0)
    click = curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED
    curses.mousemask(click | wheel_up | wheel_down)

    layouts = [tui_layout(t) for t in tables]
    sorts = [sort_for(t, parsed) for t in tables]
    cur, sel, scroll = 0, 0, 0
    last_sort = (None, 0.0)  # (columna, instante): un doble clic no debe ordenar dos veces

    def put(y, x, text, attr=0):
        h, w = scr.getmaxyx()
        if 0 <= y < h and 0 <= x < w - 1:
            try:
                scr.addnstr(y, x, text, w - 1 - x, attr)
            except curses.error:
                pass

    def cell_text(c, text, width):
        return text.ljust(width) if c["align"] == "left" else text.rjust(width)

    def do_sort(idx):
        nonlocal sel, scroll, last_sort
        now = time.monotonic()
        if last_sort[0] == (cur, idx) and now - last_sort[1] < 0.35:
            return
        last_sort = ((cur, idx), now)
        sorts[cur] = next_sort(tables[cur], sorts[cur], idx)
        sel, scroll = idx, 0

    while True:
        t, (widths, xs) = tables[cur], layouts[cur]
        h, w = scr.getmaxyx()
        body_h = h - 6
        scr.erase()
        if h < 8 or w < 24:
            put(0, 0, "Terminal too small")
            scr.refresh()
        else:
            rows = sort_rows(t, *sorts[cur])
            scroll = max(0, min(scroll, len(rows) - body_h))
            for i, (a, b) in enumerate(tab_spans(tables)):
                put(0, a, f" {tables[i]['cols'][0]['name']} ", curses.A_REVERSE if i == cur else curses.A_BOLD)
            put(0, tab_spans(tables)[-1][1] + 2, t["title"], curses.A_DIM)
            for i, c in enumerate(t["cols"]):
                name = c["name"] + ((" ▼" if sorts[cur][1] else " ▲") if sorts[cur][0] == i else "")
                attr = curses.A_BOLD | (curses.A_REVERSE if i == sel else 0)
                put(1, xs[i], cell_text(c, name, widths[i]), attr)
            put(2, 0, "  ".join("─" * wd for wd in widths))
            for r, row in enumerate(rows[scroll:scroll + body_h]):
                for i, c in enumerate(row):
                    put(3 + r, xs[i], cell_text(t["cols"][i], c["text"], widths[i]))
            put(h - 3, 0, "  ".join("─" * wd for wd in widths))
            for i, c in enumerate(t["total"]):
                put(h - 2, xs[i], cell_text(t["cols"][i], c["text"], widths[i]), curses.A_BOLD)
            more = f"  [{scroll + 1}-{min(scroll + body_h, len(rows))}/{len(rows)}]" if len(rows) > body_h else ""
            put(h - 1, 0, "click header: sort | ←→ column, Enter: sort | Tab: next table | ↑↓ PgUp PgDn: scroll | q: quit"
                + more, curses.A_DIM)
            scr.refresh()

        ch = scr.getch()
        if ch in (ord("q"), ord("Q"), 27):
            return
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, state = curses.getmouse()
            except curses.error:
                continue
            if wheel_down and state & wheel_down:
                scroll += 3
            elif state & wheel_up:
                scroll -= 3
            elif state & click and my == 0:
                for i, (a, b) in enumerate(tab_spans(tables)):
                    if a <= mx < b and i != cur:
                        cur, sel, scroll = i, 0, 0
            elif state & click and my == 1:
                idx = column_at(widths, xs, mx)
                if idx is not None:
                    do_sort(idx)
        elif ch == curses.KEY_LEFT:
            sel = max(0, sel - 1)
        elif ch == curses.KEY_RIGHT:
            sel = min(len(t["cols"]) - 1, sel + 1)
        elif ch in (curses.KEY_ENTER, 10, 13, ord("s")):
            do_sort(sel)
        elif ch == 9 or ch == curses.KEY_BTAB:
            cur = (cur + (1 if ch == 9 else -1)) % len(tables)
            sel, scroll = 0, 0
        elif ch == curses.KEY_UP:
            scroll -= 1
        elif ch == curses.KEY_DOWN:
            scroll += 1
        elif ch == curses.KEY_PPAGE:
            scroll -= max(1, body_h - 1)
        elif ch == curses.KEY_NPAGE:
            scroll += max(1, body_h - 1)
        elif ch == curses.KEY_HOME:
            scroll = 0
        elif ch == curses.KEY_END:
            scroll = 10 ** 9


def cmd_interactive(args):
    parsed = None
    if args.sort:
        try:
            parsed = parse_sort(args.sort)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    prices = load_prices()
    records = filtered_records(args.since, args.project)
    if not records:
        print(NO_RECORDS)
        return 0
    tables = [build_table(by, records, prices) for by in by_list_for(args)]
    try:
        import curses  # no viene con Python en Windows
    except ImportError as e:
        print(f"The interactive mode is not available here ({e}); printing the plain report instead.",
              file=sys.stderr)
        return cmd_report(args)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("--interactive needs a terminal; printing the plain report instead.", file=sys.stderr)
        return cmd_report(args)
    try:
        os.environ.setdefault("ESCDELAY", "25")
        curses.wrapper(tui_main, tables, parsed)
    except curses.error as e:
        print(f"The interactive mode is not available here ({e}); printing the plain report instead.",
              file=sys.stderr)
        return cmd_report(args)
    except KeyboardInterrupt:
        pass
    return 0


# ----------------------------------------------------------------------- web
def report_data(since=None, project=None):
    prices = load_prices()
    records = filtered_records(since, project)
    if not records:
        return {"empty": True, "message": NO_RECORDS, "tables": [], "notes": []}
    return {"empty": False, "message": "", "notes": report_notes(records, prices),
            "tables": [build_table(by, records, prices) for by in GROUPS]}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "claude-spend"

    def log_message(self, *_args):  # sin ruido en la terminal
        pass

    def _send(self, code, body, ctype, extra=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # solo se atiende a nuestro propio origen: evita que otra web lea los datos por DNS rebinding
        if (self.headers.get("Host") or "").lower() not in self.server.allowed_hosts:
            return self._send(403, "Forbidden", "text/plain; charset=utf-8")
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/":
                return self._send(200, PAGE.replace("__VERSION__", __version__), "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                               "script-src 'unsafe-inline'; connect-src 'self'"})
            if url.path == "/api/report":
                q = urllib.parse.parse_qs(url.query)
                since = (q.get("since") or [""])[0]
                if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
                    return self._send(400, json.dumps({"error": "since must be YYYY-MM-DD"}),
                                      "application/json; charset=utf-8")
                data = report_data(since or None, (q.get("project") or [""])[0] or None)
                return self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
            return self._send(404, "Not found", "text/plain; charset=utf-8")
        except Exception:
            traceback.print_exc()
            return self._send(500, "Internal error", "text/plain; charset=utf-8")


def cmd_web(args):
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"Cannot listen on 127.0.0.1:{args.port} ({e}). Try another port with --port N.", file=sys.stderr)
        return 1
    port = server.server_address[1]
    server.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    url = f"http://127.0.0.1:{port}/"
    print(f"Serving the report at {url} (Ctrl+C to stop)", flush=True)
    if not args.no_open:
        webbrowser.open(url)  # navegador por defecto del sistema
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Spend</title>
<style>
  :root { color-scheme: light dark; --bg:#fafaf9; --fg:#1c1917; --muted:#6b645f; --line:#e2dfdc;
          --head:#f1efed; --accent:#b45309; --hover:#f5f3f1; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#171413; --fg:#f2efed; --muted:#a39d98; --line:#3a3532; --head:#24201e;
            --accent:#fb923c; --hover:#1f1b1a; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg);
         font:15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { width:max-content; min-width:min(1100px, 100%); max-width:100%; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .ver { font-size:13px; font-weight:400; color:var(--muted); }
  .sub { color:var(--muted); margin:0 0 20px; }
  .bar { display:flex; flex-wrap:wrap; gap:12px 16px; align-items:end; margin-bottom:16px; }
  label { display:flex; flex-direction:column; gap:4px; font-size:13px; color:var(--muted); }
  input { font:inherit; color:var(--fg); background:var(--bg); border:1px solid var(--line);
          border-radius:6px; padding:6px 8px; min-width:0; }
  button { font:inherit; color:var(--fg); background:var(--head); border:1px solid var(--line);
           border-radius:6px; padding:6px 12px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button:focus-visible, input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
  .tabs button[aria-selected="true"] { background:var(--accent); border-color:var(--accent); color:#fff; }
  .wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
  table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
  th, td { padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { background:var(--head); position:sticky; top:0; padding:0; }
  th button { all:unset; box-sizing:border-box; display:block; width:100%; padding:8px 12px; cursor:pointer;
              font-weight:600; }
  th button:hover { color:var(--accent); }
  th button:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
  .r { text-align:right; } th.r button { text-align:right; }
  tbody tr:hover { background:var(--hover); }
  tfoot td { font-weight:700; border-bottom:0; background:var(--head); }
  .state { padding:24px; color:var(--muted); }
  .notes { margin:16px 0 0; padding:0; list-style:none; color:var(--muted); font-size:13px; }
  .notes li { margin-bottom:4px; }
  .err { color:#b91c1c; }
</style>
</head>
<body>
<main>
  <h1>Claude Spend <span class="ver">v__VERSION__</span></h1>
  <p class="sub">Estimated Claude Code token spend at list price. Click a column header to sort.</p>
  <form class="bar" id="filters">
    <label>Since <input type="date" id="since"></label>
    <label>Project contains <input type="text" id="project" placeholder="any"></label>
    <button type="submit">Refresh</button>
  </form>
  <div class="tabs" role="tablist" id="tabs"></div>
  <div class="wrap"><div id="out" class="state">Loading…</div></div>
  <ul class="notes" id="notes"></ul>
</main>
<script>
  let data = null, tab = 0;
  const sorts = {};   // per table: [column index, desc]
  const $ = (id) => document.getElementById(id);

  const defaultDesc = (col) => col.type !== "text";
  const key = (v) => (typeof v === "string" ? v.toLowerCase() : v);

  function sortedRows(t, idx, desc) {
    const have = t.rows.filter((r) => r[idx].sort !== null);
    const missing = t.rows.filter((r) => r[idx].sort === null);
    have.sort((a, b) => {
      const x = key(a[idx].sort), y = key(b[idx].sort);
      const c = x < y ? -1 : x > y ? 1 : 0;
      return desc ? -c : c;
    });
    return have.concat(missing);
  }

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  function render() {
    const tabs = $("tabs"), out = $("out"), notes = $("notes");
    tabs.replaceChildren(); notes.replaceChildren();
    if (data.empty) { out.className = "state"; out.textContent = data.message; return; }
    data.tables.forEach((t, i) => {
      const b = el("button", t.cols[0].name);
      b.type = "button"; b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(i === tab));
      b.onclick = () => { tab = i; render(); };
      tabs.append(b);
    });
    const t = data.tables[tab];
    const [si, sd] = sorts[tab] || t.default_sort;
    const table = el("table"), thead = el("thead"), hr = el("tr");
    t.cols.forEach((c, i) => {
      const th = el("th", undefined, c.align === "right" ? "r" : "");
      th.scope = "col";
      th.setAttribute("aria-sort", i === si ? (sd ? "descending" : "ascending") : "none");
      const b = el("button", c.name + (i === si ? (sd ? " ▼" : " ▲") : ""));
      b.type = "button";
      b.onclick = () => {
        sorts[tab] = i === si ? [i, !sd] : [i, defaultDesc(c)];
        render();
      };
      th.append(b); hr.append(th);
    });
    thead.append(hr);
    const tbody = el("tbody");
    sortedRows(t, si, sd).forEach((row) => {
      const tr = el("tr");
      row.forEach((c, i) => tr.append(el("td", c.text, t.cols[i].align === "right" ? "r" : "")));
      tbody.append(tr);
    });
    const tfoot = el("tfoot"), fr = el("tr");
    t.total.forEach((c, i) => fr.append(el("td", c.text, t.cols[i].align === "right" ? "r" : "")));
    tfoot.append(fr);
    table.append(thead, tbody, tfoot);
    out.className = ""; out.replaceChildren(table);
    data.notes.forEach((n) => notes.append(el("li", n)));
  }

  async function load() {
    const q = new URLSearchParams();
    if ($("since").value) q.set("since", $("since").value);
    if ($("project").value.trim()) q.set("project", $("project").value.trim());
    try {
      const r = await fetch("/api/report?" + q);
      if (!r.ok) throw new Error("HTTP " + r.status);
      data = await r.json();
      if (tab >= data.tables.length) tab = 0;
      render();
    } catch (e) {
      $("out").className = "state err";
      $("out").textContent = "Could not load the report: " + e.message;
    }
  }

  $("filters").addEventListener("submit", (e) => { e.preventDefault(); load(); });
  load();
</script>
</body>
</html>
"""


def main():
    p = argparse.ArgumentParser(prog="claude-spend",
                                description="Claude Code token spend by agent, project, model, day, session and task")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("mode", nargs="?", default="report", choices=["report", "hook", "import"])
    p.add_argument("--import", dest="do_import", action="store_true",
                   help="import the transcripts on disk first")
    p.add_argument("--by", action="append", choices=list(GROUPS) + ["all"],
                   help="grouping (can be repeated; 'all' shows every table); default: agent and project")
    p.add_argument("--since", help="only from this date (YYYY-MM-DD)")
    p.add_argument("--project", help="only projects whose path contains this text")
    p.add_argument("--sort", metavar="COLUMN[:asc|desc]",
                   help="sort the tables by a column (" + ", ".join(sort_choices()) + "); "
                        "text sorts ascending and numbers/dates descending unless :asc or :desc is given")
    p.add_argument("--interactive", action="store_true",
                   help="show the tables in the terminal; click a column header to sort")
    p.add_argument("--web", action="store_true", help="serve the tables as a web page on localhost")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port for --web (default {DEFAULT_PORT})")
    p.add_argument("--no-open", action="store_true", help="with --web, do not open the browser")
    args = p.parse_args()
    for stream in (sys.stdout, sys.stderr):  # una consola con codificación antigua no debe romper con → ▲ ⚠
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if args.interactive and args.web:
        p.error("--interactive and --web cannot be used together")
    if args.mode == "hook":
        return cmd_hook(args)
    if args.mode == "import" or args.do_import:
        cmd_import(args)
        if args.mode == "import":
            return 0
    if args.web:
        return cmd_web(args)
    if args.interactive:
        return cmd_interactive(args)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
