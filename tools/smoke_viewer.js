/**
 * Headless smoke test for the generated workbench.
 *
 *   node tools/smoke_viewer.js out/viewer.html
 *
 * The workbench is one self-contained HTML file with no dependencies, so there
 * is nothing to install: this script runs its inline JavaScript against a small
 * DOM/canvas shim, then drives the real interaction model (keyboard, rubber-band
 * zoom, wheel, overview drag, channel toggles, lap selection) and asserts that
 * the view actually changed. It is the cheapest way to catch "the UI throws
 * before you can see anything" without a browser.
 *
 * Set I3PRO_HASH=... to start from a URL hash (e.g. "mode=overlay").
 * Pass --expect-template when the file is the raw src template: it must not
 * throw, and it must show the "this is not a data file" instructions.
 */
const fs = require("fs");
const vm = require("vm");

const file = process.argv[2];
if (!file) {
  console.error("usage: node tools/smoke_viewer.js <viewer.html>");
  process.exit(2);
}

const html = fs.readFileSync(file, "utf8");
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) {
  console.error("no inline <script> found");
  process.exit(1);
}
const script = match[1];

const calls = { fillText: 0, stroke: 0, lineTo: 0, rect: 0, arc: 0, fillRect: 0 };

// The real DOM grows as innerHTML is assigned (e.g. <canvas id="overview">),
// and getElementById returns null until then. Mirror that so the viewer's
// "build it on first use" guards behave the same headless.
let REGISTRY = null;

function makeContext() {
  return new Proxy({}, {
    get(target, key) {
      if (key in target) return target[key];
      if (typeof key === "symbol") return undefined;
      return () => { if (key in calls) calls[key] += 1; };
    },
    set(target, key, value) { target[key] = value; return true; },
  });
}

class Element {
  constructor(tag, id) {
    this.tagName = String(tag || "div").toUpperCase();
    this.id = id || "";
    this._html = "";
    this._children = [];
    this._rows = [];
    this._q = {};
    this._attrs = {};
    this._handlers = {};
    this._chart = null;
    this.style = {};
    this.dataset = {};
    this._classes = new Set();
    this.classList = {
      add: (c) => this._classes.add(c),
      remove: (c) => this._classes.delete(c),
      toggle: (c, on) => { if (on) this._classes.add(c); else this._classes.delete(c); },
      contains: (c) => this._classes.has(c),
    };
    this.value = "";
    this.title = "";
    this.disabled = false;
    this.checked = false;
    this.textContent = "";
    this.innerHTML = "";
  }

  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = String(value);
    this._rows = [];
    const re = /<tr data-lap="([^"]+)"/g;
    let m;
    while ((m = re.exec(this._html))) {
      const row = new Element("tr");
      row.dataset.lap = m[1];
      this._rows.push(row);
    }
    const idre = /id="([^"]+)"/g;
    while ((m = idre.exec(this._html))) {
      if (REGISTRY && !REGISTRY.has(m[1])) REGISTRY.set(m[1], new Element("div", m[1]));
    }
  }

  appendChild(child) {
    if (child && child.tagName === "FRAGMENT") {
      this._children.push(...child._children);
      return child;
    }
    this._children.push(child);
    return child;
  }

  addEventListener(type, fn) {
    (this._handlers[type] = this._handlers[type] || []).push(fn);
  }

  dispatch(type, event) {
    (this._handlers[type] || []).forEach((fn) => fn(event));
  }

  closest(selector) {
    if (selector === "canvas") return this.tagName === "CANVAS" ? this : null;
    if (selector === ".chart") return this._chart;
    return null;
  }

  querySelector(selector) {
    if (!this._q[selector]) {
      const tag = selector === "canvas" ? "canvas" : "div";
      this._q[selector] = new Element(tag);
    }
    return this._q[selector];
  }

  querySelectorAll(selector) {
    if (selector === "tr[data-lap]" || selector === "tr") return this._rows;
    if (selector.indexOf("canvas") >= 0) return this._q.canvas ? [this._q.canvas] : [];
    if (selector === "input") return this._children.filter((c) => c.tagName === "INPUT");
    return [];
  }

  getAttribute(name) { return this._attrs[name]; }
  setAttribute(name, value) { this._attrs[name] = value; }
  getContext() { return makeContext(); }
  toDataURL() { return "data:image/png;base64,"; }
  click() { this.dispatch("click", { target: this }); }
  blur() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 150 }; }
  get clientWidth() { return 900; }
  get parentElement() { return this._parent || (this._parent = new Element("div")); }
}

function buildDom(markup) {
  const registry = new Map();
  REGISTRY = registry;
  const idre = /id="([^"]+)"/g;
  let m;
  // only the real document markup counts; ids that appear inside the inline
  // <script> (e.g. the template string for the overview canvas) must not exist
  // until that element is actually inserted.
  while ((m = idre.exec(markup))) registry.set(m[1], new Element("div", m[1]));
  const document = {
    getElementById(id) {
      if (registry.has(id)) return registry.get(id);
      const found = document.body._children.find((c) => c.id === id);
      return found || null;
    },
    createElement(tag) { return new Element(tag); },
    createDocumentFragment() { return new Element("fragment"); },
    querySelectorAll(selector) {
      if (selector.indexOf("#") < 0) return [];
      const out = [];
      const host = registry.get("chartHost");
      if (host) out.push(...host._children);
      ["trackWrap", "scatterWrap", "deltaWrap", "overviewChart"].forEach((id) => {
        const el = registry.get(id);
        if (el) out.push(el);
      });
      return out;
    },
    body: new Element("body"),
    addEventListener() {},
  };
  return { registry, document };
}

function run(hash) {
  const { registry, document } = buildDom(html.split("<script>")[0]);
  const window = {
    devicePixelRatio: 1,
    _handlers: {},
    addEventListener(type, fn) { (this._handlers[type] = this._handlers[type] || []).push(fn); },
    dispatch(type, event) { (this._handlers[type] || []).forEach((fn) => fn(event)); },
  };
  const location = { hash: hash ? "#" + hash : "", origin: "http://localhost", pathname: "/x" };
  const navigator = { clipboard: { writeText: async () => {} } };
  const sandbox = {
    document, window, location, navigator, console,
    setTimeout, clearTimeout, URLSearchParams,
    fetch: () => Promise.reject(new Error("fetch unavailable headless")),
  };
  vm.runInNewContext(script, sandbox);
  return { window, registry, document, api: window.i3pro };
}

/* ------------------------------------------------------------------- drive */
const problems = [];
const check = (ok, message) => { if (!ok) problems.push(message); };
const expectTemplate = process.argv.indexOf("--expect-template") >= 0;

let ctx;
try {
  ctx = run(process.env.I3PRO_HASH || "");
} catch (error) {
  console.error("FAIL: viewer script threw:", error.message);
  console.error(error.stack.split("\n").slice(0, 8).join("\n"));
  process.exit(1);
}

const window = ctx.window;
const registry = ctx.registry;
const api = ctx.api;

if (expectTemplate) {
  const body = ctx.document.body.innerHTML;
  check(String(ctx.document.title).indexOf("模板") >= 0,
    "template notice did not set the page title");
  check(body.indexOf("启动.bat") >= 0 && body.indexOf("导出快照.bat") >= 0,
    "template notice does not tell the user which launcher to double-click");
  check(body.indexOf("viewer.html") >= 0,
    "template notice does not name the file that was opened");
  if (problems.length) {
    console.error("FAIL:\n  - " + problems.join("\n  - "));
    process.exit(1);
  }
  console.log("PASS - template without data shows instructions instead of throwing");
  process.exit(0);
}
const key = (k, extra) => window.dispatch("keydown",
  Object.assign({ key: k, target: { tagName: "BODY" }, preventDefault() {} }, extra || {}));

check(!!api, "window.i3pro debug handle was not exported");
if (api) {
  const state = api.state;
  const host = registry.get("chartHost");
  let chartCanvas = null;
  for (const child of host._children) {
    if (child._q && child._q.canvas) {
      chartCanvas = child._q.canvas;
      chartCanvas._chart = child;
      break;
    }
  }
  const charts = registry.get("charts");
  const debug = process.env.I3PRO_DEBUG ? console.error : () => {};
  debug("charts handlers: " + Object.keys(charts._handlers).join(","));
  debug("window handlers: " + Object.keys(window._handlers).join(","));
  check(!!chartCanvas, "no chart canvas was created");

  if (chartCanvas) {
    const before = api.lane().slice();
    charts.dispatch("mousedown", { detail: 2, clientX: 100, clientY: 40,
      altKey: false, ctrlKey: false, preventDefault() {}, target: chartCanvas });
    debug("after mousedown, band active: " + String(api.state.view));
    charts.dispatch("mousemove", { clientX: 400, clientY: 40, target: chartCanvas });
    window.dispatch("mouseup", { clientX: 400, clientY: 40 });
    debug("view after mouseup: " + JSON.stringify(api.state.view));
    const after = api.lane();
    debug("before " + before + " after " + after);
    check(after[1] - after[0] < (before[1] - before[0]) * 0.95,
      "double-click drag did not zoom in");

    const mid = (after[0] + after[1]) / 2;
    charts.dispatch("mousedown", { detail: 2, clientX: 300, clientY: 40,
      altKey: false, ctrlKey: false, preventDefault() {}, target: chartCanvas });
    window.dispatch("mouseup", { clientX: 301, clientY: 41 });
    check(api.lane()[1] - api.lane()[0] < after[1] - after[0],
      "plain double click did not zoom in");
    check(mid >= api.lane()[0] - 1e-6 && mid <= api.lane()[1] + 1e-6,
      "zoom moved away from the clicked position");

    key("F2");
    check(state.view === null, "F2 did not reset to the full range");
    key("w");
    check(Array.isArray(state.view) && state.view[1] - state.view[0] > 0,
      "W did not zoom to the default (one lap) range");
    key("F2");

    const preWheel = api.lane().slice();
    charts.dispatch("wheel", { deltaY: -100, clientX: 300, clientY: 40,
      target: chartCanvas, altKey: false, preventDefault() {} });
    check(api.lane()[1] - api.lane()[0] < preWheel[1] - preWheel[0], "wheel did not zoom in");
    key("F2");
  }

  const s0 = state.style;
  key("s");
  check(state.style !== s0, "S did not toggle the trace style");
  const g0 = state.layout;
  key("g");
  check(state.layout !== g0, "G did not toggle the group layout");
  const m0 = state.show.measure;
  key("m");
  check(state.show.measure !== m0, "M did not toggle measurements");
  key("d");
  key(" ");
  check(state.datumOn && state.datum !== null, "D / space did not place the datum cursor");
  key("x");
  check(state.datum !== null, "X broke the datum cursor");
  const v0 = state.show.values;
  key("v");
  check(state.show.values !== v0, "V did not toggle the values panel");
  key("v");

  state.cursor = null;
  key("ArrowRight", { ctrlKey: false });
  const first = state.cursor;
  key("ArrowRight", { ctrlKey: true });
  check(state.cursor !== null && state.cursor > first, "arrow keys did not move the cursor");

  if ((api.data.laps || []).some((l) => l.complete)) {
    key("n");
    check(state.ref !== null, "N did not select a lap");
    key("p");
    key("q");
  }

  const overview = registry.get("overview");
  debug("overview element: " + !!overview
    + " handlers: " + (overview ? Object.keys(overview._handlers).join(",") : "-"));
  if (overview) {
    const full = api.fullRange();
    const span = full[1] - full[0];
    api.zoomTo(full[0] + span * 0.4, full[0] + span * 0.6);   // make room to move
    const before = api.lane().slice();
    overview.dispatch("mousedown", { detail: 1, clientX: 400, clientY: 20, preventDefault() {} });
    window.dispatch("mousemove", { clientX: 300, clientY: 20 });
    window.dispatch("mouseup", {});
    const after = api.lane();
    debug("overview before " + before + " after " + after);
    check(before[0] !== after[0] || before[1] !== after[1], "overview drag did not move the window");
  }

  // 9. vertical zoom on the focused group (Alt + arrows)
  state.panelZoom = {};
  key("ArrowUp", { altKey: true });
  check(Object.keys(state.panelZoom).length > 0, "Alt+Up did not zoom the focused group vertically");
  key("ArrowDown", { altKey: true });
  key("f");
  key("b");
  key("h");

  // 10. status band toggle must not flood the selection
  const selectedBefore = state.selected.length;
  key("e");
  check(state.show.status === true, "E did not switch on the status band");
  check(state.selected.length === selectedBefore,
    "E silently added channels to the selection");
  key("e");

  // 11. sidebar buttons and the scatter selectors must not throw
  ["chClear", "chDefault", "chVisible"].forEach((id) => {
    const el = registry.get(id);
    check(!!el, id + " is missing from the sidebar");
    if (el) el.dispatch("click", { target: el });
  });
  const scatterX = registry.get("scatterX");
  if (scatterX) {
    scatterX.value = state.selected[0] || "";
    scatterX.dispatch("change", { target: scatterX });
  }

  // 12. PNG export walks every visible panel
  const png = registry.get("pngBtn");
  check(!!png, "export button is missing");
  if (png) png.dispatch("click", { target: png });

  // 13. share link
  const share = registry.get("shareBtn");
  if (share) share.dispatch("click", { target: share });
}

/* --------------------------------------------------------------- DOM checks */
const header = registry.get("fileInfo");
check(header && header.innerHTML.indexOf(".ld") >= 0, "header was not populated");
const lapTable = registry.get("lapTable");
const expectsLaps = !!(api && api.data && (api.data.laps || []).length);
if (expectsLaps) {
  check(lapTable && lapTable._html.indexOf("<tr") >= 0, "lap table is empty");
}
const channelList = registry.get("channelList");
check(channelList && channelList._children.length > 5, "channel list is empty");
const count = registry.get("chCount");
check(count && String(count.textContent).indexOf("/") >= 0, "channel count missing");
const status = registry.get("statusLine");
check(status && String(status.innerHTML).indexOf("窗口") >= 0, "status line was not updated");
check(calls.fillText > 0, "no canvas text was drawn");
check(calls.lineTo > 0, "no canvas traces were drawn");
check(calls.fillRect > 0, "no canvas points were drawn");

const failures = problems.filter(Boolean);
if (failures.length) {
  console.error("FAIL:\n  - " + failures.join("\n  - "));
  process.exit(1);
}
console.log(
  "PASS - workbench ran headless (" + calls.lineTo + " line segments, "
  + calls.fillRect + " point rects, " + calls.fillText + " labels, "
  + (lapTable._rows || []).length + " lap rows, "
  + channelList._children.length + " channel rows, interactions verified)"
);
