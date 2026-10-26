/* One Shot sidebar panel. Plain web component, no build step. Uses HA theme variables. */

const STATUS = {
  done: { label: "Done", color: "var(--success-color, #43a047)" },
  failed: { label: "Failed", color: "var(--error-color, #db4437)" },
  skipped: { label: "Skipped", color: "var(--warning-color, #ffa600)" },
  expired: { label: "Expired", color: "var(--warning-color, #ffa600)" },
  cancelled: { label: "Cancelled", color: "var(--secondary-text-color)" },
};

const KIND_ICON = {
  state: "⚡", numeric: "📈", time: "⏰", datetime: "📅", delay: "⏱", sun: "☀️", timer: "⏲",
};

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.style.cssText = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function remaining(iso) {
  const ms = new Date(iso) - Date.now();
  if (ms <= 0) return "any moment";
  const m = Math.floor(ms / 60000);
  const d = Math.floor(m / 1440);
  if (d >= 1) return `${d}d ${Math.floor((m % 1440) / 60)}h`;
  if (m >= 60) return `${Math.floor(m / 60)}h ${m % 60}m`;
  return m < 1 ? "<1m" : `${m}m`;
}

function fmtWhen(iso) {
  const d = new Date(iso);
  const now = new Date();
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const day0 = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const diff = Math.round((day0(now) - day0(d)) / 86400000);
  if (diff === 0) return `Today ${t}`;
  if (diff === 1) return `Yesterday ${t}`;
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

class OneShotPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = { watches: [], history: [] };
    this._error = "";
    this._timer = null;
    this._first = true;
    this._filter = "all";
  }

  set hass(hass) {
    this._hass = hass;
    if (this._first) {
      this._first = false;
      this._refresh();
    }
  }
  set narrow(v) { this._narrow = v; this._render(); }
  set panel(_) {}

  connectedCallback() {
    this._timer = setInterval(() => this._refresh(), 5000);
    this._render();
  }
  disconnectedCallback() { clearInterval(this._timer); }

  async _refresh() {
    if (!this._hass) return;
    try {
      this._data = await this._hass.callWS({ type: "one_shot/list" });
      this._error = "";
    } catch (e) {
      this._error = e.message || "Could not load One Shot data";
    }
    this._render();
  }

  async _cancel(w) {
    if (!confirm(`Cancel "${w.title}"?\n\n${w.summary}`)) return;
    try {
      await this._hass.callWS({ type: "one_shot/cancel", one_shot_id: w.id });
    } catch (e) {
      alert(e.message || "Cancel failed");
    }
    this._refresh();
  }

  _setFilter(f) {
    this._filter = this._filter === f ? "all" : f;
    this._render();
  }

  // --- flow chain: trigger -> only if -> actions ------------------------------

  _node(cls, label, text) {
    return h("div", { class: `node ${cls}` }, h("small", {}, label), text);
  }

  _flow(item, run) {
    const flow = item.flow;
    if (!flow) return h("div", { class: "sum" }, item.summary);

    const next = run ? run.next : 0;
    const waiting = run && run.resume_at;
    const nodes = [];

    const trigText = flow.triggers.map((t) => `${KIND_ICON[t.kind] || ""} ${t.text}`.trim());
    const trigNode = this._node(`trg${run ? " done" : ""}`, flow.triggers.length > 1 ? "Any of" : "Trigger",
      h("div", {}, trigText.map((t, i) => h("div", {}, i ? "or " : "", t))));
    nodes.push(trigNode);

    if (flow.conditions.length) {
      nodes.push(this._node(`cnd${run ? " done" : ""}`, "Only if",
        h("div", {}, flow.conditions.map((c, i) => h("div", {}, i ? "and " : "", c)))));
    }

    let actionNo = 0;
    flow.actions.forEach((a, i) => {
      let state = "";
      if (run) {
        if (waiting && i === next - 1) state = " now";
        else if (i < next) state = " done";
      }
      if (a.wait) {
        const left = state === " now" ? ` · ${remaining(run.resume_at)} left` : "";
        nodes.push(this._node(`wait${state}`, "Wait", a.text.replace(/^Wait /, "") + left));
      } else {
        actionNo += 1;
        const multi = flow.actions.filter((x) => !x.wait).length > 1;
        nodes.push(this._node(`act${state}`, multi ? `Action ${actionNo}` : "Action",
          a.text + (state === " done" ? " ✓" : "")));
      }
    });

    const chain = h("div", { class: "flow" });
    nodes.forEach((n, i) => {
      if (i) chain.append(h("div", { class: "link" }));
      chain.append(n);
    });
    return chain;
  }

  // --- cards -------------------------------------------------------------------

  _card(item, run) {
    const kind = run ? null : (item.flow && item.flow.triggers[0] && item.flow.triggers[0].kind);
    const icon = run ? "▶" : (KIND_ICON[kind] || "⚡");
    let when;
    if (run) {
      when = h("div", { class: "when" }, h("b", {}, run.resume_at ? remaining(run.resume_at) : "now"), "next step");
    } else if (item.fires) {
      when = h("div", { class: "when" }, h("b", {}, remaining(item.fires)), "fires in");
    } else {
      when = h("div", { class: "when" }, h("b", {}, remaining(item.expires)), "until expiry");
    }
    const meta = run
      ? `${item.owner} · ${item.steps_left} action${item.steps_left === 1 ? "" : "s"} left`
      : `${item.owner} · armed ${fmtWhen(item.created)}${item.fires ? "" : " · waiting for the next change"}`;

    return h("div", { class: "card" },
      h("div", { class: "head" },
        h("div", { class: `ico${run ? " run" : ""}` }, icon),
        h("div", { class: "grow" },
          h("div", { class: "ttl" }, item.title, run ? h("span", { class: "pill run" }, "Running") : null),
          h("div", { class: "meta" }, meta)),
        when,
        h("button", { class: "btn", onclick: () => this._cancel(item) }, "Cancel")),
      this._flow(item, run));
  }

  // --- history -----------------------------------------------------------------

  _history(history) {
    const counts = {};
    history.forEach((e) => { counts[e.status] = (counts[e.status] || 0) + 1; });
    const chips = [["all", "All", history.length]].concat(
      Object.keys(STATUS).filter((k) => counts[k]).map((k) => [k, STATUS[k].label, counts[k]]));
    const filtered = this._filter === "all" ? history : history.filter((e) => e.status === this._filter);

    const bar = h("div", { class: "chips" }, chips.map(([key, label, n]) =>
      h("button", { class: `chip${this._filter === key ? " on" : ""}`, onclick: () => this._setFilter(key) },
        `${label} ${n}`)));

    if (!filtered.length) return [bar, h("div", { class: "empty" }, history.length ? "Nothing matches that filter." : "No history yet.")];

    const rows = filtered.map((e) => {
      const s = STATUS[e.status] || { label: e.status, color: "gray" };
      return h("tr", {},
        h("td", {}, h("span", { class: "pill", style: `--c:${s.color}` }, s.label)),
        h("td", {},
          e.title || e.summary,
          e.brief ? h("div", { class: "meta" }, e.brief) : null,
          e.detail ? h("div", { class: "meta" }, e.detail) : null),
        h("td", { class: "hide-s" }, e.owner || ""),
        h("td", { class: "nowrap" }, fmtWhen(e.time)));
    });

    return [bar, h("div", { class: "scroll", id: "hist" },
      h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", {}, "Status"), h("th", {}, "One Shot"), h("th", { class: "hide-s" }, "Who"), h("th", {}, "When"))),
        h("tbody", {}, rows))),
      h("div", { class: "meta cap" }, `Showing ${filtered.length} most recent`)];
  }

  _render() {
    const root = this.shadowRoot;
    const { watches, history } = this._data;
    const runs = this._data.runs || [];

    const oldScroll = root.getElementById ? root.getElementById("hist") : null;
    const keepScroll = oldScroll ? oldScroll.scrollTop : 0;

    const style = h("style", {}, `
      :host { display:block; background:var(--primary-background-color); color:var(--primary-text-color);
              min-height:100vh; font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
      * { box-sizing:border-box; }
      header { display:flex; align-items:center; gap:8px; height:56px; padding:0 16px;
               background:var(--app-header-background-color, var(--primary-color));
               color:var(--app-header-text-color, #fff); font-size:20px; }
      header button { background:none; border:none; color:inherit; font-size:24px; cursor:pointer; padding:4px 8px; }
      main { max-width:900px; margin:0 auto; padding:16px; }
      h2 { font-size:12px; font-weight:600; margin:26px 0 10px; color:var(--secondary-text-color);
           text-transform:uppercase; letter-spacing:.08em; }
      .meta { font-size:12px; color:var(--secondary-text-color); }
      .sum { font-size:14px; line-height:1.4; color:var(--secondary-text-color); }
      .ttl { font-size:16px; font-weight:600; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
      .grow { flex:1; min-width:140px; }
      .nowrap { white-space:nowrap; }
      .empty { border:2px dashed var(--divider-color); border-radius:16px; padding:22px; text-align:center;
               color:var(--secondary-text-color); }
      .err { color:var(--error-color, #db4437); padding:8px 2px; }

      .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }
      .stat { background:var(--card-background-color, #fff); border-radius:14px; padding:14px 16px;
              box-shadow:var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.2)); border:0; text-align:left;
              color:inherit; font:inherit; }
      .stat.click { cursor:pointer; }
      .stat.sel { outline:2px solid var(--primary-color); }
      .stat .n { font-size:30px; font-weight:700; line-height:1.1; }
      .stat .l { font-size:12px; color:var(--secondary-text-color); }
      .stat.hl { background:linear-gradient(135deg, var(--primary-color), color-mix(in srgb, var(--primary-color) 60%, #000)); color:#fff; }
      .stat.hl .l { color:rgba(255,255,255,.8); }

      .card { background:var(--card-background-color, #fff); border-radius:16px; padding:16px; margin-bottom:14px;
              box-shadow:var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.2)); }
      .head { display:flex; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap; }
      .ico { width:44px; height:44px; border-radius:12px; display:grid; place-items:center; font-size:20px; flex:none;
             background:color-mix(in srgb, var(--primary-color) 15%, transparent); }
      .ico.run { color:var(--primary-color); }
      .when { text-align:right; font-size:13px; color:var(--secondary-text-color); }
      .when b { display:block; font-size:16px; color:var(--primary-text-color); }
      .btn { border:1px solid var(--divider-color); background:none; color:var(--secondary-text-color);
             border-radius:8px; padding:6px 12px; cursor:pointer; font-size:13px; font-family:inherit; }
      .btn:hover { border-color:var(--error-color, #db4437); color:var(--error-color, #db4437); }
      .pill { --c:var(--primary-color); font-size:11px; font-weight:600; padding:3px 9px; border-radius:999px;
              white-space:nowrap; color:var(--c); background:color-mix(in srgb, var(--c) 16%, transparent); }
      .pill.run { --c:var(--primary-color); }

      .flow { display:flex; align-items:stretch; overflow-x:auto; padding-bottom:4px; }
      .node { flex:none; min-width:130px; max-width:220px; border-radius:12px; padding:10px 12px; font-size:14px;
              border:1px solid var(--divider-color); background:var(--primary-background-color); }
      .node small { display:block; font-size:10px; letter-spacing:.08em; text-transform:uppercase;
                    color:var(--secondary-text-color); margin-bottom:3px; font-weight:600; }
      .node.trg { border-color:var(--primary-color); background:color-mix(in srgb, var(--primary-color) 14%, transparent); }
      .node.cnd { border-style:dashed; }
      .node.act { border-color:var(--success-color, #43a047); background:color-mix(in srgb, var(--success-color, #43a047) 14%, transparent); }
      .node.wait { border-color:var(--warning-color, #ffa600); background:color-mix(in srgb, var(--warning-color, #ffa600) 14%, transparent); }
      .node.done { opacity:.55; }
      .node.now { box-shadow:0 0 0 3px color-mix(in srgb, var(--primary-color) 30%, transparent), 0 0 0 4px var(--primary-color); }
      .link { flex:none; width:26px; align-self:center; height:2px; background:var(--divider-color); position:relative; }
      .link::after { content:""; position:absolute; right:-1px; top:-4px; border:5px solid transparent;
                     border-left:7px solid var(--divider-color); }

      .chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
      .chip { border:1px solid var(--divider-color); background:none; color:var(--secondary-text-color); border-radius:999px;
              padding:4px 12px; font-size:12px; cursor:pointer; font-family:inherit; }
      .chip.on { background:var(--primary-color); border-color:var(--primary-color); color:#fff; }
      .scroll { max-height:340px; overflow-y:auto; border-radius:14px; background:var(--card-background-color, #fff);
                box-shadow:var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.2)); }
      .tbl { width:100%; border-collapse:collapse; }
      .tbl th { position:sticky; top:0; z-index:1; background:var(--card-background-color, #fff); text-align:left;
                font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--secondary-text-color);
                padding:10px 16px; border-bottom:1px solid var(--divider-color); }
      .tbl td { padding:11px 16px; border-bottom:1px solid var(--divider-color); font-size:14px; vertical-align:top; }
      .tbl tr:last-child td { border-bottom:0; }
      .cap { margin-top:6px; }
      @media (max-width:640px) {
        .hide-s { display:none; }
        .when { text-align:left; }
        .stats { grid-template-columns:1fr 1fr; }
        .flow { flex-wrap:wrap; row-gap:8px; }
      }
    `);

    const menu = h("button", {
      title: "Menu",
      onclick: () => this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })),
    }, "☰");

    // Summary tiles. History is capped, so the counts cover the last 7 days of what is kept.
    const weekAgo = Date.now() - 7 * 86400000;
    const recent = (st) => history.filter((e) => e.status === st && new Date(e.time) >= weekAgo).length;
    const tile = (cls, n, label, filter, color) => h("button", {
      class: `stat ${cls}${filter ? " click" : ""}${filter && this._filter === filter ? " sel" : ""}`,
      onclick: filter ? () => this._setFilter(filter) : null,
    }, h("div", { class: "n", style: color ? `color:${color}` : "" }, n), h("div", { class: "l" }, label));

    const stats = h("div", { class: "stats" },
      tile("hl", watches.length, "Armed now"),
      tile("", runs.length, "Running"),
      tile("", recent("done"), "Completed, last 7 days", "done", "var(--success-color, #43a047)"),
      tile("", recent("failed"), "Failed, last 7 days", "failed", "var(--error-color, #db4437)"));

    const active = [
      ...runs.map((r) => this._card(r, r)),
      ...watches.map((w) => this._card(w, null)),
    ];

    root.replaceChildren(
      style,
      h("header", {}, this._narrow ? menu : null, "One Shot"),
      h("main", {},
        this._error ? h("div", { class: "err" }, this._error) : null,
        stats,
        h("h2", {}, `Active (${watches.length + runs.length})`),
        active.length ? active : h("div", { class: "empty" }, "No One Shots are active. Ask Assist to set one up."),
        h("h2", {}, "Recent"),
        ...this._history(history)));

    const hist = root.getElementById("hist");
    if (hist) hist.scrollTop = keepScroll;
  }
}

customElements.define("one-shot-panel", OneShotPanel);
