/* One Shot sidebar panel. Plain web component, no build step. Uses HA theme variables. */

const STATUS = {
  done: { label: "Done", color: "var(--success-color, #43a047)" },
  failed: { label: "Failed", color: "var(--error-color, #db4437)" },
  skipped: { label: "Skipped", color: "var(--warning-color, #ffa600)" },
  expired: { label: "Expired", color: "var(--warning-color, #ffa600)" },
  cancelled: { label: "Cancelled", color: "var(--secondary-text-color)" },
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
  return `${m}m`;
}

class OneShotPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = { watches: [], history: [] };
    this._error = "";
    this._timer = null;
    this._first = true;
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

  _render() {
    const root = this.shadowRoot;
    const { watches, history } = this._data;
    const runs = this._data.runs || [];

    const style = h("style", {}, `
      :host { display:block; background:var(--primary-background-color); color:var(--primary-text-color);
              min-height:100vh; font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
      header { display:flex; align-items:center; gap:8px; height:56px; padding:0 16px; box-sizing:border-box;
               background:var(--app-header-background-color, var(--primary-color));
               color:var(--app-header-text-color, #fff); font-size:20px; }
      header button { background:none; border:none; color:inherit; font-size:24px; cursor:pointer; padding:4px 8px; }
      main { max-width:760px; margin:0 auto; padding:16px; box-sizing:border-box; }
      h2 { font-size:16px; font-weight:500; margin:24px 0 8px; color:var(--secondary-text-color);
           text-transform:uppercase; letter-spacing:.06em; }
      .card { background:var(--card-background-color, #fff); border-radius:12px; padding:14px 16px; margin-bottom:10px;
              box-shadow:var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.2)); display:flex; gap:12px; align-items:center; }
      .grow { flex:1; min-width:0; }
      .ttl { font-size:16px; font-weight:600; margin-bottom:2px; }
      .sum { font-size:14px; line-height:1.4; color:var(--secondary-text-color); }
      .meta { font-size:12px; color:var(--secondary-text-color); margin-top:4px; }
      .btn { border:1px solid var(--divider-color); background:none; color:var(--primary-text-color);
             border-radius:8px; padding:6px 12px; cursor:pointer; font-size:13px; }
      .btn:hover { border-color:var(--error-color, #db4437); color:var(--error-color, #db4437); }
      .pill { font-size:11px; font-weight:600; padding:2px 8px; border-radius:10px; color:#fff; white-space:nowrap; }
      .empty { color:var(--secondary-text-color); padding:8px 2px; }
      .err { color:var(--error-color, #db4437); padding:8px 2px; }
    `);

    const menu = h("button", {
      title: "Menu",
      onclick: () => this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })),
    }, "☰");

    const active = watches.length
      ? watches.map((w) => h("div", { class: "card" },
          h("div", { class: "grow" },
            h("div", { class: "ttl" }, w.title),
            h("div", { class: "sum" }, w.summary),
            h("div", { class: "meta" }, `${w.fires ? "Fires in " + remaining(w.fires) : "Expires in " + remaining(w.expires)} · ${w.owner} · created ${new Date(w.created).toLocaleString()}`)),
          h("button", { class: "btn", onclick: () => this._cancel(w) }, "Cancel")))
      : (runs.length ? [] : [h("div", { class: "empty" }, "No One Shots are active. Ask Assist to set one up.")]);

    const running = runs.map((r) => h("div", { class: "card" },
      h("div", { class: "grow" },
        h("div", { class: "ttl" }, r.title),
        h("div", { class: "sum" }, r.summary),
        h("div", { class: "meta" }, `Running: next step in ${remaining(r.resume_at)} · ${r.steps_left} action${r.steps_left === 1 ? "" : "s"} left · ${r.owner}`)),
      h("span", { class: "pill", style: "background:var(--primary-color)" }, "Running"),
      h("button", { class: "btn", onclick: () => this._cancel(r) }, "Cancel")));

    const past = history.length
      ? history.map((e) => {
          const s = STATUS[e.status] || { label: e.status, color: "gray" };
          return h("div", { class: "card" },
            h("div", { class: "grow" },
              h("div", { class: "ttl" }, e.title || e.summary),
              h("div", { class: "sum" }, e.title ? e.summary : ""),
              h("div", { class: "meta" }, new Date(e.time).toLocaleString() + (e.owner ? ` · ${e.owner}` : "") + (e.detail ? ` · ${e.detail}` : ""))),
            h("span", { class: "pill", style: `background:${s.color}` }, s.label));
        })
      : [h("div", { class: "empty" }, "No history yet.")];

    root.replaceChildren(
      style,
      h("header", {}, this._narrow ? menu : null, "One Shot"),
      h("main", {},
        this._error ? h("div", { class: "err" }, this._error) : null,
        h("h2", {}, `Active (${watches.length + runs.length})`), running, active,
        h("h2", {}, "Recent"), past));
  }
}

customElements.define("one-shot-panel", OneShotPanel);
