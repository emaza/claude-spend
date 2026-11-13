(() => {   // everything in its own scope: nothing leaks into the page's globals
  "use strict";

  const DIMS = JSON.parse(document.querySelector("main").dataset.groups);   // the tables, which are also the things a row click can filter by
  let data = null, seq = 0;
  let tab = null;            // name of the table on screen
  let filters = {};          // dimension -> value, set by clicking a row
  const sorts = {};          // per table name: [column id, desc]
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

  // The view (filters, tab, sort) lives in the page URL: reloading keeps it and Back undoes a filter.
  function query(withView) {
    const q = new URLSearchParams();
    if ($("since").value) q.set("since", $("since").value);
    if ($("project").value.trim()) q.set("project", $("project").value.trim());
    Object.entries(filters).forEach(([d, v]) => q.set("f_" + d, v));
    if (withView) {
      if (tab) q.set("tab", tab);
      Object.entries(sorts).forEach(([t, [c, desc]]) => q.append("sort", `${t}.${c}:${desc ? "desc" : "asc"}`));
    }
    return q;
  }

  function writeUrl(push) {
    const s = query(true).toString();
    const url = location.pathname + (s ? "?" + s : "");
    if (url === location.pathname + location.search) return;
    if (push) history.pushState(null, "", url); else history.replaceState(null, "", url);
  }

  function readUrl() {
    const q = new URLSearchParams(location.search);
    tab = q.get("tab");
    filters = {};
    DIMS.forEach((d) => { if (q.get("f_" + d)) filters[d] = q.get("f_" + d); });
    Object.keys(sorts).forEach((k) => delete sorts[k]);
    q.getAll("sort").forEach((s) => {
      const m = /^(\w+)\.(\w+):(asc|desc)$/.exec(s);
      if (m) sorts[m[1]] = [m[2], m[3] === "desc"];
    });
    const since = q.get("since") || "";
    $("since").value = /^\d{4}-\d{2}-\d{2}$/.test(since) ? since : "";
    $("project").value = q.get("project") || "";
  }

  function drill(by, value) {   // a row was clicked: keep only that row's records; the tab stays the same
    filters[by] = value;
    writeUrl(true); load();
  }

  function renderChips() {
    const box = $("chips"), dims = Object.keys(filters);
    box.replaceChildren();
    box.hidden = !dims.length;
    dims.forEach((d) => {
      const b = el("button", `${d}: ${filters[d]} ×`, "chip");
      b.type = "button"; b.title = "Remove this filter";
      b.onclick = () => { delete filters[d]; writeUrl(true); load(); };
      box.append(b);
    });
    if (dims.length > 1) {
      const b = el("button", "Clear all", "chip");
      b.type = "button";
      b.onclick = () => { filters = {}; writeUrl(true); load(); };
      box.append(b);
    }
  }

  function render() {
    const tabs = $("tabs"), out = $("out"), notes = $("notes");
    tabs.replaceChildren(); notes.replaceChildren();
    renderChips();
    if (data.empty) { out.className = "state"; out.textContent = data.message; return; }
    const t = data.tables.find((x) => x.by === tab) || data.tables[0];
    tab = t.by;
    data.tables.forEach((x) => {
      const b = el("button", x.cols[0].name);
      b.type = "button"; b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(x === t));
      b.onclick = () => { tab = x.by; writeUrl(false); render(); };
      tabs.append(b);
    });
    let [si, sd] = t.default_sort;
    if (sorts[tab]) {
      const i = t.cols.findIndex((c) => c.id === sorts[tab][0]);
      if (i >= 0) [si, sd] = [i, sorts[tab][1]];
    }
    const table = el("table"), thead = el("thead"), hr = el("tr");
    t.cols.forEach((c, i) => {
      const th = el("th", undefined, c.align === "right" ? "r" : "");
      th.scope = "col";
      th.setAttribute("aria-sort", i === si ? (sd ? "descending" : "ascending") : "none");
      const b = el("button", c.name + (i === si ? (sd ? " ▼" : " ▲") : ""));
      b.type = "button";
      b.onclick = () => {
        sorts[tab] = [c.id, i === si ? !sd : defaultDesc(c)];
        writeUrl(false); render();
      };
      th.append(b); hr.append(th);
    });
    thead.append(hr);
    const tbody = el("tbody");
    sortedRows(t, si, sd).forEach((row) => {
      const tr = el("tr");
      row.forEach((c, i) => {
        const td = el("td", i ? c.text : undefined, t.cols[i].align === "right" ? "r" : "");
        if (!i) {   // a real button, so the row can also be opened from the keyboard; its click bubbles to the row
          const b = el("button", c.text);
          b.type = "button"; b.title = `Show only this ${t.by}`;
          td.append(b);
        }
        tr.append(td);
      });
      tr.onclick = () => { if (!String(getSelection())) drill(t.by, row[0].sort); };
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
    const n = ++seq;   // if another load starts meanwhile, this answer is stale
    try {
      const r = await fetch("/api/report?" + query(false));
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      if (n !== seq) return;
      data = d;
      render();
    } catch (e) {
      if (n !== seq) return;
      $("out").className = "state err";
      $("out").textContent = "Could not load the report: " + e.message;
    }
  }

  $("filters").addEventListener("submit", (e) => { e.preventDefault(); writeUrl(true); load(); });
  window.addEventListener("popstate", () => { readUrl(); load(); });
  readUrl();
  load();
})();
