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
      if (key === "measureText") {
        // 真 canvas 会去量字；假 canvas 给个像样的数就行——注释那行字要先量宽度
        // 才好铺底色条，返回 undefined 会让"文字画不出来"变成一个假 bug。
        return (text) => ({ width: String(text).length * 6 });
      }
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
    // 真 DOM 赋 innerHTML 会把旧子节点全部丢掉。shim 以前只换文本、留着旧
    // children，于是"重新 build 之后还找得到上一次那个元素"——那会掩盖
    // 真正的 bug（改动没生效，测试却读到了旧对象）。
    this._children = [];
    this._rows = [];
    // Beacon pills: one editable name box and one delete link per beacon. The
    // viewer wires these up with querySelectorAll, so the shim has to hand back
    // the same elements the markup describes.
    this._bnames = [];
    this._dels = [];
    const re = /<tr data-lap="([^"]+)"/g;
    let m;
    while ((m = re.exec(this._html))) {
      const row = new Element("tr");
      row.dataset.lap = m[1];
      this._rows.push(row);
    }
    const bname = /data-beacon-name="(\d+)"[^>]*?value="([^"]*)"/g;
    while ((m = bname.exec(this._html))) {
      const box = new Element("input");
      box.dataset.beaconName = m[1];
      box.value = m[2].replace(/&quot;/g, '"').replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">").replace(/&amp;/g, "&");
      this._bnames.push(box);
    }
    const del = /data-beacon="(\d+)"/g;
    while ((m = del.exec(this._html))) {
      const link = new Element("a");
      link.dataset.beacon = m[1];
      this._dels.push(link);
    }
    // Maths definitions: one row per definition (clicking it opens the editor)
    // and one delete link per row, same pattern as the beacon pills above.
    this._mdefs = [];
    this._mdels = [];
    const mrow = /data-maths-row="(\d+)"/g;
    while ((m = mrow.exec(this._html))) {
      const row = new Element("div");
      row.dataset.mathsRow = m[1];
      this._mdefs.push(row);
    }
    const mdel = /data-maths-del="(\d+)"/g;
    while ((m = mdel.exec(this._html))) {
      const link = new Element("a");
      link.dataset.mathsDel = m[1];
      this._mdels.push(link);
    }
    // Notes (#15): one text box and one delete link per note, same pattern.
    this._ntext = [];
    this._ndels = [];
    const ntext = /data-note-text="(\d+)"[^>]*?value="([^"]*)"/g;
    while ((m = ntext.exec(this._html))) {
      const box = new Element("input");
      box.dataset.noteText = m[1];
      box.value = m[2].replace(/&quot;/g, '"').replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">").replace(/&amp;/g, "&");
      this._ntext.push(box);
    }
    const ndel = /data-note-del="(\d+)"/g;
    while ((m = ndel.exec(this._html))) {
      const link = new Element("a");
      link.dataset.noteDel = m[1];
      this._ndels.push(link);
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
    if (selector === ".comp") return this._comp;
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
    if (selector === "input[data-beacon-name]") return this._bnames || [];
    if (selector === "a[data-beacon]") return this._dels || [];
    if (selector === "[data-maths-row]") return this._mdefs || [];
    if (selector === "[data-maths-del]") return this._mdels || [];
    if (selector === "input[data-note-text]") return this._ntext || [];
    if (selector === "a[data-note-del]") return this._ndels || [];
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
    // ticket #17：注册表里那条"只有标题"的自检形式只在无头驱动里注册——
    // 浏览器里没人设这个标记，所以队员永远看不到它。设在这里是为了让
    // "加一种显示形式只要一条声明"这句话每次回归都被真的走一遍。
    __I3PRO_SELFTEST__: true,
    _handlers: {},
    addEventListener(type, fn) { (this._handlers[type] = this._handlers[type] || []).push(fn); },
    removeEventListener(type, fn) {
      const list = this._handlers[type] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    dispatch(type, event) { (this._handlers[type] || []).slice().forEach((fn) => fn(event)); },
  };
  const location = { hash: hash ? "#" + hash : "", origin: "http://localhost", pathname: "/x" };
  const navigator = { clipboard: { writeText: async () => {} } };
  const sandbox = {
    document, window, location, navigator, console,
    setTimeout, clearTimeout, URLSearchParams,
    // browser globals the workbench relies on
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    escape: global.escape, unescape: global.unescape,
    localStorage: {
      _v: {},
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
      setItem(k, v) { this._v[k] = String(v); },
      removeItem(k) { delete this._v[k]; },
    },
    // Every request is recorded so the payloads the UI *sends* can be asserted;
    // none of them resolve, because nothing answers on the other end. The
    // response -> screen half is driven through api.applyLapsResponse instead.
    fetch: (url, options) => {
      httpCalls.push({
        url: String(url),
        method: (options && options.method) || "GET",
        body: options && options.body ? String(options.body) : null,
      });
      return Promise.reject(new Error("fetch unavailable headless"));
    },
  };
  vm.runInNewContext(script, sandbox);
  return { window, registry, document, api: window.i3pro, httpCalls: httpCalls };
}

/* ------------------------------------------------------------------- drive */
const problems = [];
const check = (ok, message) => { if (!ok) problems.push(message); };
const expectTemplate = process.argv.indexOf("--expect-template") >= 0;
const httpCalls = [];

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

/** Find the component element the app created for a component id. */
function SHEETEl(root, id) {
  let found = null;
  (function walk(node) {
    if (found || !node || !node._children) return;
    for (const child of node._children) {
      if (child.dataset && child.dataset.id === id) { found = child; return; }
      walk(child);
    }
  })(root);
  return found;
}

/** Everything inside one component element whose class matches `needle`. */
function insideOf(root, needle) {
  const out = [];
  (function walk(node) {
    if (!node || !node._children) return;
    for (const child of node._children) {
      if (String(child.className || "").indexOf(needle) >= 0) out.push(child);
      walk(child);
    }
  })(root);
  return out;
}

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
  const worksheet = registry.get("worksheet");
  const canvases = [];
  (function walk(node, owner) {
    if (!node || !node._children) return;
    for (const child of node._children) {
      const nextOwner = child.dataset && child.dataset.type ? child : owner;
      if (child.tagName === "CANVAS") {
        child._comp = nextOwner;
        canvases.push(child);
      }
      walk(child, nextOwner);
    }
  })(worksheet, null);
  let chartCanvas = canvases[0] || null;
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
  let graphComp = state.components.find((c) => c.type === "graph");
  const g0 = graphComp ? graphComp.config.mode : null;
  key("g");
  check(graphComp && graphComp.config.mode !== g0,
    "G did not toggle the focused graph's tiled/overlapped layout");
  // two graphs on one sheet must be switchable independently
  api.addComponentOfType("graph");
  const graphs = state.components.filter((c) => c.type === "graph");
  if (graphs.length >= 2) {
    const first = graphs[0], second = graphs[1];
    first.config.mode = "tiled";
    second.config.mode = "tiled";
    state.focusId = second.id;
    key("g");
    check(second.config.mode === "overlapped" && first.config.mode === "tiled",
      "G changed a graph that was not the focused one");
    state.focusId = first.id;
    key("g");
    check(first.config.mode === "overlapped" && second.config.mode === "overlapped",
      "G did not toggle the second graph");
  }
  api.applyPreset("分析");                      // back to a known worksheet
  const m0 = state.show.measure;
  key("m");
  check(state.show.measure !== m0, "M did not toggle measurements");
  // 恢复：后面第 15 组要看图例里的 min / max / avg，measure 关着的话那边
  // 只能靠上一次 build 留下的旧节点"看起来通过"。
  key("m");
  check(state.show.measure === m0, "M did not toggle measurements back");
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

  // 10. E adds / removes a status component instead of flooding the selection
  const compsBeforeE = state.components.length;
  const hadStatus = state.components.some((c) => c.type === "status");
  key("e");
  check(state.components.some((c) => c.type === "status") !== hadStatus,
    "E did not toggle the status component");
  key("e");
  check(state.components.length === compsBeforeE, "E left the worksheet changed");

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

  // 14. worksheet: the component model itself
  const before = state.components.length;
  check(before > 0, "the default worksheet has no components");
  check(state.preset === "分析", "expected the 分析 preset to start with, got " + state.preset);
  api.addComponentOfType("graph");
  check(state.components.length === before + 1, "adding a component did not change the worksheet");
  const added = state.components[state.components.length - 1];
  api.componentAction(added, "up");
  check(state.components.indexOf(added) === before - 1, "moving a component up did nothing");
  api.componentAction(added, "close");
  check(state.components.length === before, "removing a component did not shrink the worksheet");
  api.applyPreset("动力");
  check(state.preset === "动力", "switching preset did not take effect");
  const graphChans = api.groupsFor(state.components.find((c) => c.type === "graph"))
    .reduce((a, g) => a.concat(g.channels), []);
  check(graphChans.length > 0, "the 动力 preset produced a graph with no channels");
  api.applyPreset("分析");

  // 15. the graph header carries the cursor value next to min / max / avg
  if (worksheet) {
    const headers = [];
    const rows = [];
    (function walk(node) {
      if (!node || !node._children) return;
      for (const child of node._children) {
        const cls = String(child.className || "");
        if (cls.indexOf("graphhead") >= 0) headers.push(child);
        if (cls.indexOf("lrow") >= 0) rows.push(child);
        walk(child);
      }
    })(worksheet);
    check(headers.length > 0, "no graph header element was created");
    const mid = (api.lane()[0] + api.lane()[1]) / 2;
    state.cursor = mid;
    api.renderAll();
    check(rows.length > 0, "the graph header has no per-channel rows");
    const withValue = rows.filter((row) => {
      const cells = row._children || [];
      return cells.length >= 3 && /^[-+]?\d/.test(String(cells[2].textContent || ""));
    });
    check(withValue.length > 0,
      "the graph header does not show the value at the cursor (i2 Pro shows name | cursor | min | max | avg)");
    const withMeasure = rows.filter((row) => {
      const cells = row._children || [];
      return cells.length >= 6 && /^[-+]?\d/.test(String(cells[5].textContent || ""));
    });
    check(withMeasure.length > 0, "the graph header does not show min / max / avg");
  }

  // 16. the worksheet must survive a round trip through a share link
  const encoded = api.encodeLayout(state.components);
  check(!!encoded, "the worksheet could not be encoded for a share link");
  const decoded = api.decodeLayout(encoded);
  check(!!decoded && decoded.length === state.components.length,
    "the worksheet did not survive a link round trip");
  check(decoded && decoded.map((c) => c.type).join(",") === state.components.map((c) => c.type).join(","),
    "the worksheet link round trip lost or reordered components");

  // 16b. lap-splitting controls and the windowed GPS track option
  const modeSel = registry.get("lapMode");
  check(!!modeSel, "the lap splitting-mode selector is missing");
  check(modeSel && String(modeSel._html).indexOf('value="run"') >= 0
    && String(modeSel._html).indexOf('value="figure8"') >= 0,
    "the lap mode selector does not offer run / figure8");
  check(!!registry.get("addBeacon"), "the beacon button is missing");
  const trackComp = state.components.find((c) => c.type === "track");
  check(!!trackComp, "no track component on the default worksheet");
  check(trackComp && (trackComp.config.window || "all") === "all",
    "the track component should start in whole-session mode");
  const trackEl = trackComp && SHEETEl(worksheet, trackComp.id);
  if (trackEl) {
    const found = [];
    (function walk(node) {
      if (!node || !node._children) return;
      for (const child of node._children) {
        if (child.tagName === "SELECT" && String(child._html).indexOf('value="zoom"') >= 0) found.push(child);
        walk(child);
      }
    })(trackEl);
    check(found.length > 0,
      "the track component has no whole-session / time-range switch");
  }

  // 17. clicking a lap must jump the view to that lap
  const lapTableEl = registry.get("lapTable");
  const lapRows = lapTableEl && lapTableEl._rows ? lapTableEl._rows : [];
  const completeLap = (api.data.laps || []).find((l) => l.complete);
  if (lapRows.length && completeLap) {
    const row = lapRows.find((r) => r.dataset.lap === String(completeLap.lap)) || lapRows[0];
    key("F2");                                  // start from the full session
    check(state.view === null, "F2 did not clear the window before the lap click test");
    debug("lap test: rows=" + lapRows.length + " want=" + completeLap.lap
      + " got=" + (row && row.dataset.lap) + " handlers=" + (row ? Object.keys(row._handlers).join(",") : "-")
      + " ref=" + state.ref + " cmp=" + state.cmp + " mode=" + state.mode);
    row.dispatch("click", { target: row, shiftKey: false, ctrlKey: false });
    debug("lap test after click: view=" + JSON.stringify(state.view)
      + " ref=" + state.ref + " cmp=" + state.cmp);
    check(Array.isArray(state.view), "clicking a lap did not zoom to it");
    check(state.view && Math.abs(state.view[0] - completeLap.start_time) < 0.01
      && Math.abs(state.view[1] - completeLap.end_time) < 0.01,
      "clicking a lap zoomed to the wrong time range");

    // the 基 button makes it the Main lap
    const refBefore = state.ref;
    api.selectLap(completeLap.lap, false);
    check(String(state.ref) === String(completeLap.lap),
      "「基」did not make the lap the reference");
    check(state.cmp === null, "「基」should clear the comparison lap");

    // the 比 button arms the comparison and switches to overlay
    const other = (api.data.laps || []).find((l) => l.complete && l.lap !== completeLap.lap);
    if (other) {
      api.selectLap(other.lap, true);
      check(String(state.cmp) === String(other.lap), "「比」did not set the comparison lap");
      check(state.mode === "overlay", "「比」did not switch to the overlay comparison");
      // the lap table must expose both buttons on every row
      check(lapTableEl._html.indexOf('data-role="ref"') >= 0
        && lapTableEl._html.indexOf('data-role="cmp"') >= 0,
        "the lap table is missing the 基 / 比 buttons");
      api.selectLap(other.lap, true);           // toggling it off again
      check(state.cmp === null, "clicking 「比」twice should clear the comparison");
    }
  }

  // 18. datum cursor: space must produce a visible delta
  state.cursor = null;
  key("d");
  key(" ");
  check(state.datumOn && state.datum !== null, "space did not place the datum cursor");
  const mid2 = (api.lane()[0] + api.lane()[1]) / 2;
  state.cursor = mid2;
  api.renderAll();
  const statusNow = String(registry.get("statusLine").innerHTML || "");
  check(statusNow.indexOf("基准光标") >= 0, "the status line does not report the datum cursor");
  check(statusNow.indexOf("Δ") >= 0, "the status line does not report the delta");
  const deltaCells = [];
  (function walk(node) {
    if (!node || !node._children) return;
    for (const child of node._children) {
      if (String(child.className || "").indexOf("ldelta") >= 0) deltaCells.push(child);
      walk(child);
    }
  })(worksheet);
  check(deltaCells.some((cell) => String(cell.textContent).indexOf("Δ") === 0
    && /[-+]?\d/.test(String(cell.textContent))),
    "the graph header does not show the datum delta");
  key("d");                                     // back to a clean state

  // 19. gauges: every subtype must render something
  const gaugeSubtypes = ["numeric", "list", "bar", "dial", "wheel"];
  const beforeGauge = state.components.length;
  for (const subtype of gaugeSubtypes) {
    const comp = { id: "gauge-test-" + subtype, type: "gauge", x: 0, y: 0, w: 4, h: 13,
                   config: { subtype: subtype, channels: [] } };
    state.components.push(comp);
    api.buildWorksheet();
    api.syncScatterSelectors();
    const fills = calls.fillText;
    api.renderAll();
    check(calls.fillText > fills, "gauge subtype " + subtype + " drew no text");
  }
  while (state.components.length > beforeGauge) {
    api.componentAction(state.components[state.components.length - 1], "close");
  }
  check(state.components.length === beforeGauge, "cleaning up the gauge test components failed");

  // 20. free layout: drag to move, drag the corner to resize, both snap
  const target = state.components.find((c) => c.type === "scatter") || state.components[0];
  const element = SHEETEl(worksheet, target.id);
  check(!!element, "could not find the component element for the layout test");
  if (element) {
    const bar = element._children[0];
    const startX = target.x, startY = target.y;
    bar.dispatch("mousedown", { clientX: 100, clientY: 100, preventDefault() {}, stopPropagation() {} });
    window.dispatch("mousemove", { clientX: 260, clientY: 160 });
    window.dispatch("mouseup", {});
    check(target.x !== startX || target.y !== startY, "dragging a component did not move it");

    const beforeW = target.w, beforeH = target.h;
    const handle = element._children[element._children.length - 1];
    check(String(handle.className || "").indexOf("rz") >= 0,
      "the resize handles are missing from the component");
    handle.dispatch("mousedown", { clientX: 100, clientY: 100, preventDefault() {}, stopPropagation() {} });
    window.dispatch("mousemove", { clientX: 200, clientY: 200 });
    window.dispatch("mouseup", {});
    check(target.w !== beforeW || target.h !== beforeH, "dragging the corner did not resize it");
    check(target.x % 0.25 === 0 && target.y % 0.5 === 0 &&
          target.w % 0.25 === 0 && target.h % 0.25 === 0,
          "component geometry is not on the grid (snapping broken)");

    // the right-edge handle must change width only, the bottom-edge height only
    const handles = element._children.filter((c) => String(c.className || "").indexOf("rz") >= 0);
    check(handles.length === 3, "expected three resize handles, got " + handles.length);
    const rightHandle = handles.find((c) => String(c.className).indexOf("rzright") >= 0);
    const w0 = target.w, h0 = target.h;
    if (rightHandle) {
      rightHandle.dispatch("mousedown", { clientX: 100, clientY: 100, preventDefault() {}, stopPropagation() {} });
      window.dispatch("mousemove", { clientX: 160, clientY: 400 });
      window.dispatch("mouseup", {});
      check(target.w !== w0 && target.h === h0, "the right-edge handle must change width only");
    }
  }

  // 21. beacon editing: rename in place, insert a crossing at the cursor
  const beaconHost = registry.get("beaconList");
  check(!!beaconHost, "the beacon list element is missing");
  check(!!registry.get("addCrossing"), "the insert-crossing button is missing");
  if (beaconHost) {
    const apiBase = api.data.api;
    state.lapsConfig = {
      mode: "auto",
      beacons: [{ name: "左环", lat: 34.1, lon: 113.6 },
                { name: "右环", lat: 34.2, lon: 113.7 }],
      trusted: { "左环 1": false },
    };
    // A snapshot has no server to save to: both edits must say so rather than
    // silently doing nothing.
    if (!apiBase) {
      api.renameBeacon(0, "不该生效");
      check(state.lapsConfig.beacons[0].name === "左环",
        "a snapshot must not rename a beacon");
      api.insertCrossing();
      check(String(registry.get("toast").textContent).indexOf("serve") >= 0,
        "in snapshot mode the beacon edits must tell the user to run serve mode");
    }
    // ...and with the api base the payload carries in serve mode, they save.
    api.data.api = "/api";
    api.renderLapControls();
    const nameBoxes = beaconHost.querySelectorAll("input[data-beacon-name]");
    check(nameBoxes.length === 2,
      "expected one editable name box per beacon, got " + nameBoxes.length);
    check(beaconHost._html.indexOf('data-beacon-name="0"') >= 0,
      "beacon names are not editable in place");

    // a name is user text: it must not be able to break out of the markup
    state.lapsConfig.beacons[0].name = 'a"b<c>';
    api.renderLapControls();
    check(beaconHost._html.indexOf("&quot;") >= 0 && beaconHost._html.indexOf("<c>") < 0,
      "a beacon name with quotes / angle brackets was not escaped");
    state.lapsConfig.beacons[0].name = "左环";
    api.renderLapControls();

    // re-query: every render replaces the boxes (and re-attaches the handlers)
    const renameBox = beaconHost.querySelectorAll("input[data-beacon-name]")[0];
    const beforeRename = httpCalls.length;
    renameBox.value = "  左环A  ";
    renameBox.dispatch("keydown", { key: "Enter", preventDefault() {} });
    check(state.lapsConfig.beacons[0].name === "左环A",
      "Enter did not commit the new beacon name (got "
      + state.lapsConfig.beacons[0].name + ")");
    // ...and the edit reached the server as one PUT carrying the trimmed name
    const puts = httpCalls.slice(beforeRename).filter(
      (call) => call.method === "PUT" && call.url.indexOf("/laps") >= 0);
    check(puts.length === 1, "Enter must save through exactly one PUT, got " + puts.length);
    if (puts.length === 1) {
      const sent = JSON.parse(puts[0].body);
      check(sent.beacons[0].name === "左环A",
        "the payload does not carry the trimmed name: " + sent.beacons[0].name);
      check(puts[0].url.indexOf("/session/") >= 0,
        "the save did not address the open session: " + puts[0].url);
    }

    const beforeCancel = httpCalls.length;
    renameBox.value = "别改我";
    renameBox.dispatch("keydown", { key: "Escape", preventDefault() {} });
    check(state.lapsConfig.beacons[0].name === "左环A",
      "Esc must not send the edit it is cancelling");
    check(httpCalls.length === beforeCancel,
      "Esc sent a request anyway: " + JSON.stringify(httpCalls.slice(beforeCancel)));
    // The box goes back to the last *rendered* name - in a browser that is the
    // saved one, because a successful save re-renders the list.
    check(renameBox.value !== "别改我", "Esc must put the name back in the box");

    // Esc 之后再改一次名字，回车必须还能存：dirty 一旦被 Esc 关掉就再也回不来，
    // 用户看到的是"名字改不动了"（真浏览器里点得出来，见 A33）。
    const retryBox = beaconHost.querySelectorAll("input[data-beacon-name]")[0];
    const beforeRetry = httpCalls.length;
    retryBox.value = "左环B";
    retryBox.dispatch("input", {});
    retryBox.dispatch("keydown", { key: "Enter", preventDefault() {} });
    check(state.lapsConfig.beacons[0].name === "左环B",
      "after Esc the same name box must still save (got "
      + state.lapsConfig.beacons[0].name + ")");
    check(httpCalls.slice(beforeRetry).filter(
      (call) => call.method === "PUT" && call.url.indexOf("/laps") >= 0).length === 1,
      "the retry after Esc must save through exactly one PUT");
    state.lapsConfig.beacons[0].name = "左环A";     // 后面的断言按这个名字算
    api.renderLapControls();

    api.renameBeacon(0, "   ");
    check(state.lapsConfig.beacons[0].name === "左环A",
      "an empty name must not rename a beacon");

    state.mode = "time";
    state.cursor = null;
    const beforeInsert = state.lapsConfig.beacons.length;
    api.insertCrossing();
    check(state.lapsConfig.beacons.length === beforeInsert,
      "inserting with no cursor must not add a beacon at t = 0");

    state.cursor = 123.456;
    const beforeInsertCall = httpCalls.length;
    api.insertCrossing();
    const added = state.lapsConfig.beacons[state.lapsConfig.beacons.length - 1];
    check(state.lapsConfig.beacons.length === beforeInsert + 1 && !!added,
      "the insert-crossing button did not add a beacon");
    check(httpCalls.slice(beforeInsertCall).some((call) => call.method === "PUT"
      && call.body && call.body.indexOf('"time":123.456') >= 0),
      "the inserted crossing was not sent with its time");
    check(added && added.time === 123.456 && added.lat === undefined && added.lon === undefined,
      "an inserted crossing must carry a time and no position");
    api.renderLapControls();
    check(beaconHost._html.indexOf("t = 123.456 s") >= 0,
      "the beacon list does not show the time of a hand-inserted crossing");
    check(beaconHost._html.indexOf("manual") >= 0,
      "a hand-inserted crossing is not drawn differently from a placed beacon");

    const dels = beaconHost.querySelectorAll("a[data-beacon]");
    check(dels.length === 3, "every beacon needs a delete link, got " + dels.length);
    if (dels.length === 3) {
      dels[2].dispatch("click", { preventDefault() {} });
      check(state.lapsConfig.beacons.length === beforeInsert,
        "the ✕ did not remove the hand-inserted crossing");
      check(state.lapsConfig.beacons.every((b) => b.time !== 123.456),
        "the ✕ removed the wrong beacon");
    }

    // On the distance axis the cursor is metres, so it has to be converted.
    state.cursor = 300;
    if (api.data.meta && api.data.meta.has_distance) {
      state.mode = "distance";
      const seconds = api.cursorTime();
      check(seconds !== null && seconds > 0 && seconds < api.data.meta.duration,
        "on the distance axis the cursor must still map to a time (got " + seconds + ")");
      state.mode = "overlay";
      check(api.cursorTime() === null,
        "overlay mode has no single time for a distance, so it must refuse");
    }
    state.mode = "time";
    state.cursor = null;
    state.lapsConfig = { mode: "auto", beacons: [], trusted: {} };
    api.renderLapControls();
    api.data.api = apiBase;

    // The screen has to follow the *server's* answer - names it trimmed and
    // de-duplicated, lap rows it recomputed. Headless fetch never resolves, so
    // that half is driven straight through the response handler.
    const rowsBefore = (api.data.laps || []).slice();
    const sample = rowsBefore[0] || { lap: "1", lap_time: 1.0, start_time: 0.0,
                                      end_time: 1.0, distance: 1.0,
                                      delta_to_best: 0.0, complete: true };
    api.applyLapsResponse({
      config: { mode: "auto", beacons: [{ name: "左环A", lat: 34.1, lon: 113.6 }],
                trusted: { "左环A 1": false } },
      laps: rowsBefore.concat([Object.assign({}, sample, { lap: "左环A 9" })]),
      notice: "这次穿越没有切出新圈",
    });
    check((api.data.laps || []).length === rowsBefore.length + 1,
      "the lap table did not take the rows the server returned");
    check(String(registry.get("lapTable")._html).indexOf("左环A 9") >= 0,
      "the lap table does not show the lap the server returned");
    check(beaconHost._html.indexOf('value="左环A"') >= 0,
      "the beacon list did not take the names the server returned");
    check(String(registry.get("toast").textContent).indexOf("没有切出新圈") >= 0,
      "a notice from the server was not shown to the user");
    api.applyLapsResponse({ config: { mode: "auto", beacons: [], trusted: {} },
                            laps: rowsBefore });
  }

  // 22. maths channels: scope badge, error text, delete payload, channel index
  const mathsHost = registry.get("mathsList");
  check(!!mathsHost, "the maths channel list element is missing");
  check(!!registry.get("mathsNew"), "the new-maths-channel button is missing");
  check(!!registry.get("mathsCommit"), "the maths save button is missing");
  check(!!registry.get("mathsFuncs"), "the function-table button is missing");
  if (mathsHost) {
    const apiBase = api.data.api;
    api.data.api = "/api";
    api.applyMathsResponse({
      definitions: [
        { name: "滑移率", expr: "('车轮速度' - '车速') / max('车速', 1)",
          unit: "", scope: "local" },
        { name: "总G", expr: "sqrt('G Force Lat'^2 + 'G Force Long'^2)",
          unit: "g", scope: "global" },
      ],
      shadowed: ["总G"],
      errors: [{ name: "滑移率", expr: "x",
                 error: "表达式里用到通道 `车速`，本场次没有这个通道。" }],
      functions: [{ name: "sqrt", min_args: 1, max_args: 1, doc: "平方根" }],
    });
    check(mathsHost._html.indexOf("滑移率") >= 0 && mathsHost._html.indexOf("总G") >= 0,
      "the maths list does not show the definitions the server returned");
    // 作用域必须一眼看得出来：本地只影响本场，全局影响以后每一场
    check(mathsHost._html.indexOf(">本地<") >= 0 && mathsHost._html.indexOf(">全局<") >= 0,
      "the maths list does not say which scope each definition is in");
    check(mathsHost._html.indexOf("本场次没有这个通道") >= 0,
      "a definition that cannot be evaluated is not shown as an error");
    check(String(registry.get("mathsNote").textContent).indexOf("总G") >= 0,
      "a local definition shadowing a global one is not reported");
    check(mathsHost.querySelectorAll("[data-maths-row]").length === 2,
      "expected one row per maths definition, got "
      + mathsHost.querySelectorAll("[data-maths-row]").length);
    check(String(registry.get("mathsFuncList")._html).indexOf("sqrt") >= 0,
      "the function table is empty");

    // 点一行 → 编辑器里出现那条定义，作用域也跟着走
    mathsHost.querySelectorAll("[data-maths-row]")[1].dispatch("click", {});
    const opened = api.mathsDraft();
    check(opened.name === "总G" && opened.scope === "global",
      "clicking a definition does not load it into the editor: " + JSON.stringify(opened));
    check(registry.get("mathsEdit").hidden === false,
      "clicking a definition does not open the editor");

    // 保存时只能带同一个作用域的定义：本地文件里塞进全局定义，
    // 会让那条全局定义在每一场都被本地版覆盖一次
    state.mathsEdit = { index: 1 };
    const globalOnly = api.mathsSaveList({ name: "总G", expr: "1", unit: "", scope: "global" });
    check(globalOnly.length === 1 && globalOnly[0].name === "总G",
      "saving a global definition must send only the global ones: "
      + JSON.stringify(globalOnly));
    state.mathsEdit = { index: 0 };
    const renamed = api.mathsSaveList({ name: "滑移率2", expr: "1", unit: "", scope: "local" });
    check(renamed.length === 1 && renamed[0].name === "滑移率2",
      "renaming a local definition left the old name behind: " + JSON.stringify(renamed));

    // 名字或表达式为空时不该发请求
    api.closeMathsEditor();
    const beforeEmpty = httpCalls.length;
    api.mathsCommit();
    check(httpCalls.length === beforeEmpty,
      "saving an empty definition sent a request anyway");

    // 删除一条全局定义：只写全局文件，且不能顺手把本地定义搬进去
    const dels = mathsHost.querySelectorAll("[data-maths-del]");
    check(dels.length === 2, "every maths definition needs a delete link, got " + dels.length);
    const beforeDelete = httpCalls.length;
    dels[1].dispatch("click", { preventDefault() {} });
    const puts = httpCalls.slice(beforeDelete).filter(
      (call) => call.method === "PUT" && call.url.indexOf("/maths") >= 0);
    check(puts.length === 1, "deleting a definition must save through one PUT, got " + puts.length);
    if (puts.length === 1) {
      check(puts[0].url.indexOf("scope=global") >= 0,
        "deleting a global definition must address the global file: " + puts[0].url);
      const sent = JSON.parse(puts[0].body);
      check(sent.definitions.length === 0,
        "deleting the only global definition must send an empty list: " + puts[0].body);
    }

    // 存完以后的通道索引：新算出来的列必须出现在通道列表里
    const beforeChannels = api.data.channels.length;
    api.reindexChannels({
      channels: api.data.channels.concat([
        { name: "Σ力", unit: "g", rate: 100, samples: 10, derived: true },
      ]),
      groups: api.data.groups,
      status: api.data.status || [],
    });
    check(api.data.channels.length === beforeChannels + 1
          && api.data.channels.some((c) => c.name === "Σ力" && c.derived === true),
      "the channel index did not take the derived column the server returned");

    // 名字是用户输入：不能从标记里跑出去
    api.applyMathsResponse({
      definitions: [{ name: 'a<b>"c"', expr: "1", unit: "", scope: "local" }],
      shadowed: [], errors: [], functions: [],
    });
    check(mathsHost._html.indexOf("&lt;b&gt;") >= 0 && mathsHost._html.indexOf("<b>") < 0,
      "a maths name with angle brackets was not escaped");

    api.applyMathsResponse({ definitions: [], shadowed: [], errors: [], functions: [] });
    check(mathsHost._html.indexOf("还没有数学通道") >= 0,
      "an empty definition list does not explain how to add one");

    // 试算结果必须把**认到的通道**念出来。用户打的 `FSD13Distance1` 会被服务端
    // 认成 `FSD13 Distance1`（少一个空格算同一条通道）；不显示认到的名字，他就
    // 不知道自己那串算成了谁——这正是他报"输入通道识别不了"时缺的那句话。
    check(typeof api.formatMathsTrial === "function", "the viewer does not expose the trial text");
    if (typeof api.formatMathsTrial === "function") {
      const good = api.formatMathsTrial({
        ok: true, samples: 143800, finite: 143800, min: 266, max: 266, mean: 266,
        channels: ["FSD13 Distance1"], functions: [], notes: [],
      });
      check(good.indexOf("FSD13 Distance1") >= 0,
        "the trial result does not say which channel the name was matched to: " + good);
      check(good.indexOf("143800") >= 0 && good.indexOf("266") >= 0,
        "the trial result lost the sample count or the statistics: " + good);
      const bad = api.formatMathsTrial({
        ok: false, error: "表达式里用到通道 `FSD`，本场次没有这个通道。", channels: ["FSD"],
      });
      check(bad.indexOf("本场次没有这个通道") >= 0 && bad.indexOf("算不出来") === 0,
        "a failed trial does not show the reason the server gave: " + bad);
    }

    // The expression box must not require typing a name that cannot be typed:
    // `Vx KF` looks like two operands, `Distance (2)` looks like a call and
    // `FSD-Distance1` looks like a subtraction. The picker inserts the quoted
    // name for any channel, so no one has to know the rule.
    const pick = registry.get("mathsPickChannel");
    check(!!pick, "the maths editor has no insert-channel picker");
    if (pick) {
      check(String(pick._html).indexOf("插入通道") >= 0,
        "the insert-channel picker has no placeholder option");
      const spaced = (api.data.channels || []).filter((c) => String(c.name).indexOf(" ") >= 0);
      check(spaced.length > 0, "this snapshot has no channel name with a space to test with");
      if (spaced.length) {
        const wanted = String(spaced[0].name);
        check(String(pick._html).indexOf(wanted) >= 0,
          "the picker does not offer the channel " + wanted);
        api.openMathsEditor(null);
        registry.get("mathsExpr").value = "";
        pick.value = wanted;
        pick.dispatch("change", { target: pick });
        check(registry.get("mathsExpr").value === "'" + wanted + "'",
          "picking " + wanted + " did not insert the quoted name (got "
          + registry.get("mathsExpr").value + ")");
        check(pick.value === "",
          "the picker should fall back to its placeholder after inserting");
        // ...and it appends to what is already there instead of replacing it
        registry.get("mathsExpr").value = "1 + ";
        pick.value = wanted;
        pick.dispatch("change", { target: pick });
        check(registry.get("mathsExpr").value === "1 + '" + wanted + "'",
          "inserting into a non-empty expression lost what was already typed (got "
          + registry.get("mathsExpr").value + ")");

        // 通道下拉按单位分组：一场 400+ 条通道，平铺一列没法找
        check(String(pick._html).indexOf("<optgroup") >= 0,
          "the channel picker does not group channels by unit");

        // 函数也不该手打：插的是"函数("，光标留在括号里，参数接着从通道下拉挑
        const funcs = registry.get("mathsPickFunc");
        check(!!funcs, "the maths editor has no insert-function picker");
        if (funcs) {
          // 快照的载荷里没有函数表（编辑本来就被禁用），所以这里先喂一份目录，
          // 验的是"目录 → 下拉 → 插入"这条接线。
          api.applyMathsResponse({
            definitions: [], shadowed: [], errors: [],
            functions: [{ name: "smooth", min_args: 1, max_args: 3, doc: "平滑滤波" }],
          });
          check(String(funcs._html).indexOf("smooth") >= 0,
            "the function picker does not list the catalogue");
          check(String(funcs._html).indexOf("1~3") >= 0,
            "the function picker does not show how many arguments the function takes");
          registry.get("mathsExpr").value = "";
          funcs.value = "smooth";
          funcs.dispatch("change", { target: funcs });
          check(registry.get("mathsExpr").value === "smooth(",
            "picking a function did not insert its template (got "
            + registry.get("mathsExpr").value + ")");
          check(funcs.value === "",
            "the function picker should fall back to its placeholder after inserting");
          pick.value = wanted;
          pick.dispatch("change", { target: pick });
          check(registry.get("mathsExpr").value === "smooth('" + wanted + "'",
            "a channel cannot be picked into a function call (got "
            + registry.get("mathsExpr").value + ")");
        }

        // 运算符键盘：整个表达式要能用鼠标点出来。用户卡住的从来不是数学，
        // 是"通道名里的空格 / 短横线什么时候要加单引号"——所以通道、函数、
        // 运算符三样都能点，表达式就不必手写。
        const ops = registry.get("mathsOps");
        check(!!ops, "the maths editor has no operator keypad");
        check(html.indexOf('data-ins="*"') >= 0 && html.indexOf('data-ins="/"') >= 0
              && html.indexOf('data-ins="("') >= 0 && html.indexOf('data-ins=")"') >= 0,
          "the operator keypad markup is missing some of + - * / ( )");
        if (ops) {
          const expr = registry.get("mathsExpr");
          const press = (sym) => {
            const btn = new Element("button");
            btn.dataset.ins = sym;
            ops.dispatch("click", { target: btn });
          };
          expr.value = "";
          press("*");
          check(expr.value === "* ",
            "pressing × on an empty expression should insert just the symbol (got " + expr.value + ")");
          expr.value = "'" + wanted + "'";
          press("*");
          check(expr.value === "'" + wanted + "' * ",
            "× after a picked channel should pad both sides (got " + expr.value + ")");
          // 键盘和通道下拉接着用：点 ×、挑通道，拼出来得是一条能算的表达式
          pick.value = wanted;
          pick.dispatch("change", { target: pick });
          check(expr.value === "'" + wanted + "' * '" + wanted + "'",
            "the keypad and the channel picker do not compose (got " + expr.value + ")");
          press("+");
          check(expr.value === "'" + wanted + "' * '" + wanted + "' + ",
            "the keypad does not append at the caret (got " + expr.value + ")");
          const back = new Element("button");
          back.id = "mathsBack";
          ops.dispatch("click", { target: back });
          check(expr.value === "'" + wanted + "' * '" + wanted + "' +",
            "⌫ should drop one character (got " + expr.value + ")");
          const wipe = new Element("button");
          wipe.id = "mathsExprClear";
          ops.dispatch("click", { target: wipe });
          check(expr.value === "", "清空 should empty the expression (got " + expr.value + ")");
        }

        // 「筛选通道」：按单位分组之后 400 多条还是要滚很久，打几个字就缩到几条
        const find = registry.get("mathsFindChannel");
        check(!!find, "the maths editor has no channel filter box");
        if (find) {
          const needle = String(wanted).slice(0, 3).toLowerCase();
          const expected = (api.data.channels || [])
            .filter((c) => String(c.name).toLowerCase().indexOf(needle) >= 0).length;
          check(expected < (api.data.channels || []).length,
            "the filter test needs a needle that actually narrows the list: " + needle);
          find.value = needle;
          find.dispatch("input", { target: find });
          check(String(pick._html).indexOf("匹配 " + expected + " 条") >= 0,
            "the filter does not report how many channels matched " + needle + ": "
            + String(pick._html).slice(0, 80));
          find.value = "zzz没有这条通道zzz";
          find.dispatch("input", { target: find });
          check(String(pick._html).indexOf("没有名字含") >= 0,
            "an empty filter result does not say so: " + String(pick._html).slice(0, 80));
          find.value = "";
          find.dispatch("input", { target: find });
          check(String(pick._html).indexOf("<optgroup") >= 0,
            "clearing the filter should bring the grouped list back");
        }
        api.closeMathsEditor();
      }
    }
    api.data.api = apiBase;
  }

  // 23. track sections: the list, the edits that reach the sidecar, and the
  // bands on the time axis.
  {
    // 快照里就带着区段：面板要有内容，而不是等 GET 回来才画
    const info = api.sectionsState();
    check(!!info && !!info.config, "the snapshot carries no track sections");
    const rows = (info && info.bands) || [];
    check(rows.length >= 2, "the section list is empty: " + rows.length);
    check(rows.every((row) => row.name && row.kind_label),
      "a section row is missing its name or kind label: " + JSON.stringify(rows[0]));
    const lengths = rows.reduce((sum, row) => sum + row.length_m, 0);
    check(Math.abs(lengths - info.length_m) < 0.5,
      "the sections do not tile the lap: " + lengths + " vs " + info.length_m);
    check(((info.config.kinds || []).length === info.config.boundaries.length - 1),
      "kinds and boundaries disagree on how many spans there are");
    const hostHtml = String(registry.get("sectionsList")._html);
    check(hostHtml.indexOf("data-section-name=\"0\"") >= 0
          && hostHtml.indexOf("skind") >= 0,
      "the section panel did not render its rows: " + hostHtml.slice(0, 120));
    check(hostHtml.indexOf("disabled") >= 0,
      "the first section's start distance must be locked at 0");
    check(String(registry.get("sectionsNote").textContent).indexOf("参考圈") >= 0,
      "the section note does not say which lap it was cut on");

    // 改名字 / 改边界 / 换种类：每条都只发一次 PUT，且带走完整定义
    const apiBase = api.data.api;
    api.data.api = apiBase || "http://serve";   // 假装在 serve 模式下（快照不许改区段）
    const beforeEdit = httpCalls.length;
    api.setSectionName(1, "T1 出弯");
    const edits = httpCalls.slice(beforeEdit)
      .filter((call) => call.method === "PUT" && call.url.indexOf("/sections") >= 0);
    check(edits.length === 1, "renaming a section must send one PUT, got " + edits.length);
    if (edits.length === 1) {
      const sent = JSON.parse(edits[0].body);
      check(sent.names && sent.names[1] === "T1 出弯",
        "the rename did not reach the request body: " + edits[0].body);
      check(sent.boundaries && sent.boundaries.length === info.config.boundaries.length,
        "a manual edit must carry the whole boundary list: " + edits[0].body);
      check(!sent.auto, "a manual edit must not ask for a re-split: " + edits[0].body);
    }
    const beforeEdge = httpCalls.length;
    api.setSectionEdge(1, 140);
    const edgePuts = httpCalls.slice(beforeEdge)
      .filter((call) => call.method === "PUT" && call.url.indexOf("/sections") >= 0);
    check(edgePuts.length === 1
          && JSON.parse(edgePuts[0].body).boundaries[1] === 140,
      "moving a boundary did not reach the request body: "
      + (edgePuts[0] ? edgePuts[0].body : "(no request)"));
    const beforeKind = httpCalls.length;
    api.toggleSectionKind(2);
    const kindPuts = httpCalls.slice(beforeKind)
      .filter((call) => call.method === "PUT" && call.url.indexOf("/sections") >= 0);
    check(kindPuts.length === 1
          && JSON.parse(kindPuts[0].body).kinds[2] !== info.config.kinds[2],
      "flipping a section kind did not reach the request body: "
      + (kindPuts[0] ? kindPuts[0].body : "(no request)"));

    // 重切：第一次按判据+灵敏度，被"手工改过"挡住之后再点一次才带 force
    api.state.sectionsForce = false;
    const beforeAuto = httpCalls.length;
    api.sectionsAuto();
    const autos = httpCalls.slice(beforeAuto)
      .filter((call) => call.method === "PUT" && call.url.indexOf("/sections") >= 0);
    check(autos.length === 1, "re-splitting must send one PUT, got " + autos.length);
    if (autos.length === 1) {
      const sent = JSON.parse(autos[0].body);
      check(sent.auto === true && sent.force !== true,
        "the first re-split must not overwrite manual edits: " + autos[0].body);
      check(typeof sent.sensitivity === "number" && !!sent.basis,
        "the re-split request is missing the basis / sensitivity: " + autos[0].body);
    }
    api.state.sectionsForce = true;      // 服务端已经用 needs_force 拦过一次
    const beforeForced = httpCalls.length;
    api.sectionsAuto();
    const forced = httpCalls.slice(beforeForced)
      .filter((call) => call.method === "PUT" && call.url.indexOf("/sections") >= 0);
    check(forced.length === 1 && JSON.parse(forced[0].body).force === true,
      "the second re-split must carry force: "
      + (forced[0] ? forced[0].body : "(no request)"));
    api.state.sectionsForce = false;

    // 带子只画在时间轴上，且跟着开关走
    const recorder = { fills: 0, strokes: 0, fillRect() { this.fills += 1; },
                       beginPath() {}, moveTo() {}, lineTo() {}, stroke() { this.strokes += 1; } };
    const pad = { l: 0, r: 0, t: 0, b: 0 };
    const mode = api.state.mode;
    api.state.mode = "time";
    api.state.showSections = true;
    const drawn = api.drawSectionBands(recorder, pad, 400, 100, info.laps[0].times[0],
                                       info.laps[0].times[info.laps[0].times.length - 1]);
    check(drawn >= 2, "no section band was drawn in time mode: " + drawn);
    // 每段两笔填充：铺满绘图区的淡色 + 顶端那条可双击的实心色条
    check(recorder.fills === drawn * 2 && recorder.strokes === drawn,
      "each band needs its wash plus the clickable strip: " + JSON.stringify(recorder));
    const withoutStrip = { fills: 0, strokes: 0, fillRect() { this.fills += 1; },
                           beginPath() {}, moveTo() {}, lineTo() {}, stroke() { this.strokes += 1; } };
    const bare = api.drawSectionBands(withoutStrip, pad, 400, 100,
                                      info.laps[0].times[0],
                                      info.laps[0].times[info.laps[0].times.length - 1], false);
    check(withoutStrip.fills === bare && bare === drawn,
      "without the strip each band is exactly one fill: " + JSON.stringify(withoutStrip));
    api.state.showSections = false;
    check(api.drawSectionBands(recorder, pad, 400, 100, 0, 1e9) === 0,
      "hiding the sections still drew bands");
    api.state.showSections = true;
    api.state.mode = "distance";
    check(api.drawSectionBands(recorder, pad, 400, 100, 0, 1e9) === 0,
      "section bands were drawn on the distance axis (each lap has its own track length)");
    api.state.mode = mode;

    const toggle = registry.get("toggleSections");
    check(!!toggle, "there is no button to hide the section bands");
    if (toggle) {
      check(api.state.showSections === true, "the section bands should start visible");
      toggle.dispatch("click", { target: toggle });
      check(!api.state.showSections && !toggle.classList.contains("on"),
        "clicking the section toggle did not hide the bands");
      toggle.dispatch("click", { target: toggle });
      check(api.state.showSections, "clicking the section toggle again did not bring them back");
    }

    // 双击区段放大（ticket #8）：带子上双击 = 缩到那一段，空档上双击 = 老行为。
    // 区段按距离定义、每条圈速度不同，所以"这一点属于哪一段"必须按各圈自己的
    // 边界时刻找——和 Python 里的 sections.band_at_time() 是同一套规则。
    const marks = info.laps || [];
    check(marks.length >= 2, "the snapshot must carry per-lap section marks: " + marks.length);
    if (marks.length >= 2) {
      const startOf = api.sectionAtTime(marks[0].times[0]);
      check(!!startOf && String(startOf.lap) === String(marks[0].label) && startOf.index === 0,
        "the first instant of a lap must land in that lap's first section: " + JSON.stringify(startOf));
      const onEdge = api.sectionAtTime(marks[0].times[1]);
      check(!!onEdge && onEdge.index === 1,
        "a section boundary belongs to the section that starts there: " + JSON.stringify(onEdge));
      const tail = marks[0].times[marks[0].times.length - 1];
      if (marks[1].times[0] > tail) {
        check(api.sectionAtTime((tail + marks[1].times[0]) / 2) === null,
          "the gap between two laps must not be inside any section");
      }
      const row = (info.bands || [])[1];
      const win = api.sectionWindow(1, null);
      check(!!win && win.start === row.start_time && win.end === row.end_time,
        "the section table must hand back the reference lap's own row: " + JSON.stringify(win));
      check(api.sectionWindow((info.bands || []).length, null) === null,
        "an out-of-range section index must give nothing rather than a guess");

      api.state.mode = "time";
      api.state.view = null;
      const applied = api.zoomToSection(win);
      check(Array.isArray(applied) && Math.abs(applied[0] - win.start) < 1e-6
            && Math.abs(applied[1] - win.end) < 1e-6,
        "zooming to a section must set the view to that section: " + JSON.stringify(applied));
      check(Math.abs(api.state.cursor - win.start) < 1e-6,
        "the cursor should follow into the section you just zoomed to");
      const said = String(registry.get("toast").textContent);
      check(said.indexOf(win.label) >= 0 && said.indexOf("–") >= 0,
        "the user was not told which section they zoomed to: " + said);

      // 双击左边表里的一行 = 同一件事；双击边界输入框是"选词"，不该跳视图
      api.state.view = null;
      const rowEl = new Element("div");
      rowEl.dataset.sectionRow = "1";
      registry.get("sectionsList").dispatch("dblclick", { target: rowEl });
      check(api.state.view && Math.abs(api.state.view[0] - win.start) < 1e-6
            && Math.abs(api.state.view[1] - win.end) < 1e-6,
        "double-clicking a section row did not zoom to it: " + JSON.stringify(api.state.view));
      const inputEl = new Element("input");
      inputEl.dataset.sectionEdge = "1";
      check(api.sectionRowIndexOf(inputEl) === null,
        "double-clicking a boundary input must not zoom the view");

      // 行中间是名字输入框（flex:1），所以"双击一行"在真实命中测试下几乎点不到。
      // 行尾那个 ⤢ 才是点得到的入口——它必须画出来，而且点了要缩到这一段。
      const sectionsHtml = String(registry.get("sectionsList")._html);
      check(sectionsHtml.indexOf('data-section-zoom="1"') >= 0,
        "the section rows have no clickable zoom entry: " + sectionsHtml.slice(0, 160));
      api.state.view = null;
      const zoomEl = new Element("a");
      zoomEl.dataset.sectionZoom = "1";
      registry.get("sectionsList").dispatch("click", {
        target: zoomEl, preventDefault() {},
      });
      check(api.state.view && Math.abs(api.state.view[0] - win.start) < 1e-6
            && Math.abs(api.state.view[1] - win.end) < 1e-6,
        "clicking the section zoom entry did not zoom to it: "
        + JSON.stringify(api.state.view));

      // "全出"必须真的回到全场：serve 模式里 state.traces 只有当前这一段，
      // 若按它算横轴上限，缩过之后按"全出"就卡在刚加载的那一段（A33 实测抓到）。
      const fullBefore = api.fullRange();
      const savedTraces = api.state.traces;
      const windowed = {};
      Object.keys(savedTraces).forEach((name) => {
        const trace = savedTraces[name];
        const xs = trace && (trace.time || trace.distance);
        if (!xs || !xs.length) { windowed[name] = trace; return; }
        const mid = Math.floor(xs.length / 2);
        const copy = Object.assign({}, trace);
        if (trace.time) copy.time = trace.time.slice(mid, mid + 5);
        if (trace.distance) copy.distance = trace.distance.slice(mid, mid + 5);
        if (trace.value) copy.value = trace.value.slice(mid, mid + 5);
        windowed[name] = copy;
      });
      api.state.traces = windowed;
      const shrunk = api.fullRange();
      check(Math.abs(shrunk[0] - fullBefore[0]) < 1e-6
            && Math.abs(shrunk[1] - fullBefore[1]) < 1e-6,
        "fullRange must not shrink to whatever window is loaded: "
        + JSON.stringify(shrunk) + " vs " + JSON.stringify(fullBefore));
      api.state.traces = savedTraces;

      // 距离轴上带子不画，所以先切回时间轴再缩——但必须把"切了轴"说出来
      api.state.mode = "distance";
      api.state.view = [0, 100];
      api.zoomToSection(win);
      check(api.state.mode === "time" && api.state.view
            && Math.abs(api.state.view[0] - win.start) < 1e-6,
        "zooming to a section from the distance axis must switch back to time: " + api.state.mode);
      check(String(registry.get("toast").textContent).indexOf("切回时间轴") >= 0,
        "switching the axis silently is exactly what confuses people");

      // 带子的淡色铺满整个绘图区（那是背景），所以"双击区段"只认顶端那条实心色条：
      // 否则原来的"双击原地放大 2 倍"就再也点不到了（这条是跑耐久快照才发现的）。
      api.state.mode = "time";
      api.state.view = null;
      let stripX = null;
      for (let x = 80; x <= 880 && stripX === null; x += 5) {
        if (api.sectionStripAt({ clientX: x, clientY: 8 }, chartCanvas)) stripX = x;
      }
      check(stripX !== null, "no pixel along the strip maps into a section band");
      check(api.SECTION_STRIP_PX > 0, "the clickable strip has no height");
      if (stripX !== null) {
        check(api.sectionStripAt({ clientX: stripX, clientY: 60 }, chartCanvas) === null,
          "only the strip may count as a section double-click, not the whole plot");
      }
      const beforeStrip = api.lane().slice();
      charts.dispatch("mousedown", { detail: 2, clientX: stripX, clientY: 8,
        altKey: false, ctrlKey: false, preventDefault() {}, target: chartCanvas });
      window.dispatch("mouseup", { clientX: stripX + 1, clientY: 9 });
      const afterStrip = api.lane();
      check(afterStrip[1] - afterStrip[0] < (beforeStrip[1] - beforeStrip[0]) * 0.95,
        "double-clicking the section strip did not zoom in: " + JSON.stringify(afterStrip));
      check(api.sectionAtTime((afterStrip[0] + afterStrip[1]) / 2) !== null,
        "the strip double-click must land on the section it points at");
      // 同一列、但落在绘图区里：仍然是原来的"原地放大 2 倍"
      api.state.view = null;
      const beforePlot = api.lane().slice();
      charts.dispatch("mousedown", { detail: 2, clientX: stripX, clientY: 60,
        altKey: false, ctrlKey: false, preventDefault() {}, target: chartCanvas });
      window.dispatch("mouseup", { clientX: stripX + 1, clientY: 61 });
      const afterPlot = api.lane();
      check(Math.abs((afterPlot[1] - afterPlot[0]) - (beforePlot[1] - beforePlot[0]) * 0.5) < 1e-6,
        "double-clicking the plot area must still be the plain 2x zoom: " + JSON.stringify(afterPlot));

      // 没有时间范围的一段：说清楚，并且一个像素都不动
      const kept = api.state.view ? api.state.view.slice() : null;
      check(api.zoomToSection({ label: "坏段", start: 5, end: 5 }) === null,
        "a zero-width section must not be applied");
      check(String(api.state.view) === String(kept),
        "a zero-width section must leave the view alone: " + JSON.stringify(api.state.view)
        + " vs " + JSON.stringify(kept));

      api.state.mode = mode;
      api.state.view = null;
      api.renderAll();
    }
    api.data.api = apiBase;
  }

  // 24. undo: one step back, and a button that is grey rather than silent
  const undoBtn = registry.get("undoLaps");
  check(!!undoBtn, "the undo button is missing");
  if (undoBtn) {
    const apiBase = api.data.api;
    // A snapshot has no server, so there is no "previous version" to go back to:
    // the button must be grey, and clicking it anyway (keyboard, script) must say
    // why instead of swallowing the click.
    if (!apiBase) {
      api.renderLapControls();
      check(undoBtn.disabled === true,
        "in snapshot mode the undo button must be disabled, not clickable");
      check(String(undoBtn.title).indexOf("serve") >= 0,
        "the disabled undo button does not say why it is disabled");
      const quiet = httpCalls.length;
      undoBtn.click();
      check(httpCalls.length === quiet, "a snapshot must not send an undo request");
      check(String(registry.get("toast").textContent).indexOf("serve") >= 0,
        "in snapshot mode undo must tell the user to run serve mode");
    }

    api.data.api = "/api";
    state.canUndo = false;
    api.renderLapControls();
    check(undoBtn.disabled === true,
      "with nothing to undo the button must be disabled, not silently useless");
    check(String(undoBtn.title).indexOf("先改一次信标") >= 0,
      "the disabled undo button does not say what to do next");
    // A grey button is still reachable from the keyboard / a script; that click
    // has to say what happened rather than do nothing at all.
    let beforeUndo = httpCalls.length;
    undoBtn.click();
    check(httpCalls.length === beforeUndo, "with nothing to undo no request may be sent");
    check(String(registry.get("toast").textContent).indexOf("没有可撤销的一步") >= 0,
      "clicking a disabled undo button failed silently");

    // The server's can_undo is the only judge of whether the button is live -
    // the front end must not guess from "I edited something a moment ago".
    api.applyLapsResponse({
      config: { mode: "auto", beacons: [{ name: "左环A", lat: 34.1, lon: 113.6 }],
                trusted: { "左环A 1": false } },
      laps: (api.data.laps || []).slice(),
      can_undo: true,
    });
    check(state.canUndo === true, "the UI ignored can_undo from the server");
    check(undoBtn.disabled === false, "after an edit the undo button must be live");

    beforeUndo = httpCalls.length;
    undoBtn.click();
    const undos = httpCalls.slice(beforeUndo).filter(
      (call) => call.method === "PUT" && call.url.indexOf("/laps") >= 0);
    check(undos.length === 1, "undo must save through exactly one PUT, got " + undos.length);
    if (undos.length === 1) {
      check(undos[0].body === '{"undo":true}',
        "undo must not resend a whole config: " + undos[0].body);
    }

    // One level only: the server answers can_undo=false, so the button goes grey.
    api.applyLapsResponse({
      config: { mode: "auto", beacons: [{ name: "左环", lat: 34.1, lon: 113.6 }],
                trusted: { "左环 1": false } },
      laps: (api.data.laps || []).slice(),
      can_undo: false,
      notice: "已撤销上一步信标编辑",
    });
    check(state.canUndo === false && undoBtn.disabled === true,
      "after undoing the one step the button must go grey again");
    check(String(registry.get("toast").textContent).indexOf("已撤销") >= 0,
      "the user was not told that the undo happened");
    api.data.api = apiBase;
  }

  // 26. 报表：时间报告（区段 × 圈 + 理论最快圈）与通道报告
  const embedded = api.data.report;
  check(!!embedded && !!embedded.time,
    "快照载荷里没有报表：导出快照时必须带上 time / channels_lap / channels_section");
  if (embedded && embedded.time) {
    api.applyPreset("报表");
    const kinds = state.components.map((c) => c.type);
    check(kinds.indexOf("report") >= 0 && kinds.indexOf("chreport") >= 0,
      "「报表」预设没有摆出时间报告与通道报告: " + kinds.join(","));
    api.renderAll();

    const timeComp = state.components.find((c) => c.type === "report");
    const timeEl = SHEETEl(worksheet, timeComp.id);
    const timeWrap = insideOf(timeEl, "rwrap")[0];
    const timeNote = insideOf(timeEl, "rnote")[0];
    check(!!timeWrap && String(timeWrap._html).indexOf("<table class=\"rtable\">") >= 0,
      "时间报告没有渲染出表格");
    if (timeWrap) {
      const rows = (String(timeWrap._html).match(/<tr>/g) || []).length;
      check(rows === embedded.time.rows.length + 1,
        "时间报告的行数不对：表里 " + rows + " 行，数据 " + embedded.time.rows.length + " 段");
      check(String(timeWrap._html).indexOf("理论最快圈") < 0,
        "理论最快圈属于表下面那句话，不该塞进表格里");
    }
    check(!!timeNote
      && String(timeNote.innerHTML).indexOf("理论最快圈") >= 0
      && String(timeNote.innerHTML).indexOf(
        embedded.time.summary.theoretical.toFixed(3)) >= 0,
      "表下面那句话没有报出理论最快圈: " + (timeNote ? timeNote.innerHTML : "(缺元素)"));
    check(!!timeNote && String(timeNote.innerHTML).indexOf("连续最快圈") >= 0,
      "表下面那句话没有报出连续最快圈");

    // 只看弯道：行数必须掉到弯道的行数，且理论最快圈跟着只剩弯道
    const cornerRows = embedded.time.row_kinds.filter((k) => k === "corner").length;
    const straightRows = embedded.time.row_kinds.filter((k) => k === "straight").length;
    check(cornerRows > 0 && straightRows > 0, "金标准的区段里应该有弯也有直");
    timeComp.config.filter = "corner";
    api.renderAll();
    const cornerTable = insideOf(timeEl, "rwrap")[0];
    const cornerCount = (String(cornerTable._html).match(/<tr>/g) || []).length;
    check(cornerCount === cornerRows + 1,
      "只看弯道之后行数没变对： " + cornerCount + " vs " + (cornerRows + 1));

    // CSV：列序就是表头，行数与当前过滤后的行数一致
    const csv = api.reportCSVText(timeComp, embedded.time);
    const csvLines = csv.replace(/\n$/, "").split("\n");
    check(csvLines[0] === embedded.time.columns.map((c) => c.label).join(","),
      "CSV 表头和表格列标签不一致: " + csvLines[0]);
    check(csvLines.length === cornerRows + 1,
      "CSV 行数没有跟着「只看弯道」走: " + csvLines.length);
    check(csvLines.every((line) => line.split(",").length === embedded.time.columns.length
      || line.indexOf('"') >= 0),
      "CSV 的列数有的地方对不上");

    // 数字与引号的处理必须和 Python 的 report.to_csv 一样
    const probe = api.reportCSVText(timeComp, {
      columns: [{ key: "a", label: "名称", type: "text" },
                { key: "b", label: "用时", type: "time", decimals: 3 }],
      rows: [["T1, 入弯", 12.3456], ["带\"引号\"", 1]],
      row_kinds: ["corner", "corner"],
    });
    check(probe === "名称,用时\n\"T1, 入弯\",12.346\n\"带\"\"引号\"\"\",1.000\n",
      "前端 CSV 的转义/小数位和 Python 不一致: " + JSON.stringify(probe));

    // 导出的兜底路径：这个环境没有 Blob/URL，必须退到剪贴板而不是抛出去
    const exported = api.exportReportCSV(timeComp);
    check(typeof exported === "string" && exported.length > 0,
      "导出 CSV 在拿不到 Blob 时没有返回文本");
    check(String(registry.get("toast").textContent).indexOf("剪贴板") >= 0,
      "导出兜底时没有告诉用户 CSV 去哪了");

    // 通道报告：按圈 → 行数 = 圈数 × 通道数；按区段 → 行数 = 区段数 × 通道数
    const chComp = state.components.find((c) => c.type === "chreport");
    check((chComp.config.channels || []).length > 0,
      "通道报告没有默认通道（应该在加到工作表时从图上取几条）");
    api.renderAll();
    const chEl = SHEETEl(worksheet, chComp.id);
    const chWrap = insideOf(chEl, "rwrap")[0];
    const chRows = (String(chWrap._html).match(/<tr>/g) || []).length;
    const laps = (api.data.laps || []).length;
    check(chRows === laps * chComp.config.channels.length + 1,
      "按圈分组的通道报告行数不对: " + chRows + " vs "
      + (laps * chComp.config.channels.length + 1)
      + " [" + chComp.config.channels.join(" / ") + "]");
    check(String(chWrap._html).indexOf("标准差") >= 0 && String(chWrap._html).indexOf("绝对最大") >= 0,
      "通道报告缺少绝对最大 / 标准差这两列");

    // 按圈分组时"区段过滤"没有意义，下拉框要灰掉并说清怎么打开
    const selectsAt = (comp) => insideOf(SHEETEl(worksheet, comp.id), "scattercfg")[0]._children
      .filter((child) => child.tagName === "SELECT");
    const lapSelects = selectsAt(chComp);
    check(lapSelects.length === 3, "通道报告的配置栏应该有 3 个下拉框，实际 "
      + lapSelects.length);
    check(lapSelects[0].disabled === true
      && String(lapSelects[0].title).indexOf("按区段") >= 0,
      "按圈分组时区段过滤应该是灰的，并告诉用户切成「按区段」");
    check(lapSelects[2].disabled === true, "按圈分组时选圈下拉框应该也是灰的");

    chComp.config.by = "section";
    api.buildWorksheet();
    api.renderAll();
    const sectionSelects = selectsAt(chComp);
    check(sectionSelects[2].disabled === false, "切成按区段之后应该能选圈");
    check(String(sectionSelects[2]._html).indexOf("参考圈") >= 0,
      "选圈下拉框里没有「参考圈」这一项");
    const sectionWrap = insideOf(SHEETEl(worksheet, chComp.id), "rwrap")[0];
    const sectionRows = (String(sectionWrap._html).match(/<tr>/g) || []).length;
    const sectionCount = (embedded.channels_section.rows.length)
      / Math.max(1, embedded.channels_section.channels.length);
    check(sectionRows === sectionCount * chComp.config.channels.length + 1,
      "按区段分组的通道报告行数不对: " + sectionRows);

    // 快照模式下不许偷偷去问服务端要报表
    check(!httpCalls.some((call) => call.url.indexOf("/report") >= 0),
      "快照模式下去请求了 /report：快照必须离线可用");
    chComp.config.by = "lap";

    // serve 模式：报表由 /report 提供，参数必须带全（后端只认这几个）
    const savedApi = api.data.api;
    api.data.api = "/api";
    const chBundle = api.bundleOf(chComp);
    chBundle.dataKey = "";
    const beforeReport = httpCalls.length;
    api.refreshComponentData(chComp, true).then(() => {}, () => {});
    const reportCalls = httpCalls.slice(beforeReport)
      .filter((call) => call.url.indexOf("/report") >= 0);
    check(reportCalls.length === 1,
      "serve 模式下没有向 /report 要表（拿到 " + reportCalls.length + " 个请求）");
    if (reportCalls.length === 1) {
      const url = decodeURIComponent(reportCalls[0].url);
      check(url.indexOf("table=channels") >= 0 && url.indexOf("by=lap") >= 0,
        "报表请求少了 table / by 参数: " + url);
      check(url.indexOf("channels=") >= 0,
        "报表请求没带要统计的通道: " + url);
    }
    api.data.api = savedApi;

    // 改了区段 / 圈 / 数学通道之后，同一张表必须重算（dataVersion 进键）
    timeComp.config.filter = "all";
    api.renderAll();
    const keyBefore = api.reportFetchKey(timeComp);
    api.applySectionsResponse(Object.assign({}, api.sectionsState() || {}, { notice: "（测试）" }));
    check(api.reportFetchKey(timeComp) !== keyBefore,
      "区段改了之后报表没有失效：dataVersion 没进刷新键");

    // 布局要靠 URL 带走：过滤与分组都得回来
    timeComp.config.filter = "straight";
    chComp.config.channels = chComp.config.channels.slice(0, 2);
    const encoded = api.encodeLayout(state.components);
    const decoded = api.decodeLayout(encoded);
    const backTime = decoded.find((c) => c.type === "report");
    const backCh = decoded.find((c) => c.type === "chreport");
    check(backTime && backTime.config.filter === "straight",
      "分享链接丢了时间报告的区段过滤");
    check(backCh && (backCh.config.channels || []).join(",")
      === chComp.config.channels.join(","),
      "分享链接丢了通道报告的通道清单");
    timeComp.config.filter = "all";
  }
}

/* 27. 直方图（#9）：分布 / 格数 / 窗口 / 门槛 / 着色 / 分享链接
 *
 * 数分布是服务端的事（数的是原始样本），快照里只有导出时算好的那几份。所以这里
 * 分两半验：快照模式必须**离线可用**（一次 /histogram 都不许发），serve 模式必须
 * 把通道、格数、窗口、门槛、着色参数**一个不少**地带上。
 */
const embeddedHist = api.data.histograms;
// 这一段在报表那一段的块作用域之外，自己取一次句柄（同一个 state 对象）。
const state = api.state;
const worksheet = registry.get("worksheet");
// 27.0 组件 id 不许撞：SHEET 拿 id 当键，两份组件撞了 id 就共用一个 bundle，
// 缓存/刷新键全串味——实测的表现是直方图无限重新请求（见 A34）。
{
  const ids = state.components.map((c) => c.id);
  check(new Set(ids).size === ids.length,
    "工作表里有重复的组件 id：" + ids.join(","));
  check(api.sheetSize() === state.components.length,
    "SHEET 里的 bundle 数与组件数对不上：" + api.sheetSize()
    + " vs " + state.components.length);
  // 模拟"恢复了一份带着上次会话 id 的布局"，再加一个组件：编号必须接着最大的那个数
  api.adoptComponentIds([{ id: "histogram-9999" }]);
  const before = state.components.length;
  api.addComponentOfType("histogram");
  const after = state.components.map((c) => c.id);
  check(new Set(after).size === after.length && state.components.length === before + 1,
    "恢复布局后再加组件会撞 id：" + after.join(","));
  const fresh = state.components[state.components.length - 1];
  check(/-(\d+)$/.test(fresh.id) && parseInt(/-(\d+)$/.exec(fresh.id)[1], 10) > 9999,
    "新组件的编号没有接在已有最大号之后：" + fresh.id);
  api.componentAction(fresh, "close");
  // 旧版本存下来的布局里可能真有两条一样的 id：恢复时要顺手修掉，
  // 否则一开页就共用一个 bundle（这正是直方图那次无限请求的根因）。
  const poisoned = state.components.map((c) => ({ id: c.id, type: c.type }));
  poisoned[1].id = poisoned[0].id;
  api.adoptComponentIds(poisoned);
  check(new Set(poisoned.map((c) => c.id)).size === poisoned.length,
    "恢复一份带重复 id 的旧布局时没有修好：" + poisoned.map((c) => c.id).join(","));
}
// 这一段里**只有**进入 serve 分支之后才允许发 /histogram；前面报表那一段会临时
// 把 DATA.api 打开，那时发出去的请求不算快照的账。
const histCallsAtStart = httpCalls.length;
check(!!embeddedHist && !!embeddedHist.series
  && Object.keys(embeddedHist.series).length > 0,
  "快照载荷里没有直方图：导出快照时要带上 histograms（整场 + 每条完整圈）");
if (embeddedHist && embeddedHist.series) {
  api.applyPreset("分析");
  const histComp = state.components.find((c) => c.type === "histogram");
  check(!!histComp,
    "「分析」预设里没有直方图组件：" + state.components.map((c) => c.type).join(","));
  if (histComp) {
    api.syncHistogramSelectors();
    api.renderAll();
    check(!!histComp.config.channel,
      "直方图没有默认通道（加进工作表时应该从勾选的通道里挑一条）");
    const histEl = SHEETEl(worksheet, histComp.id);
    const histSelects = insideOf(histEl, "scattercfg")[0]._children
      .filter((child) => child.tagName === "SELECT");
    check(histSelects.length === 4,
      "直方图的配置栏应该有 4 个下拉（通道 / 画法 / 色 / 窗口），实际 "
      + histSelects.length);
    check(histSelects.length > 0 && String(histSelects[0]._html).indexOf("<optgroup") >= 0,
      "通道下拉没有按单位分组：一场 400 多条通道平铺没法找");

    // 快照：整场那份能画出来，表头把通道、点数、中位写出来
    const snap = api.snapshotHistogramFor(histComp);
    check(!!snap && snap.bins.length > 0,
      "快照里取不到这条通道的分布（导出时应该把勾选的通道都算一份）");
    if (snap) {
      const sum = snap.bins.reduce((total, box) => total + box.count, 0);
      check(sum === snap.count,
        "直方图的柱子加起来不等于样本数：" + sum + " vs " + snap.count);
      const head = String(insideOf(histEl, "graphhead")[0].textContent);
      check(head.indexOf(histComp.config.channel) >= 0 && head.indexOf("点") >= 0
        && head.indexOf("中位") >= 0,
        "直方图表头没写清它在统计什么: " + head);
      check(!httpCalls.slice(histCallsAtStart)
        .some((call) => call.url.indexOf("/histogram") >= 0),
        "快照模式下去请求了 /histogram：快照必须离线可用");

      // 格数：快照里只能往粗里并，但并出来的计数必须分毫不差
      const have = snap.bins.length;
      const merged = api.mergeCounts(snap.bins.map((box) => box.count),
                                    Math.max(1, Math.floor(have / 2)));
      check(merged.length < have
        && merged.reduce((a, b) => a + b, 0) === snap.count,
        "并格之后计数变了：" + merged.reduce((a, b) => a + b, 0) + " vs " + snap.count);
      histComp.config.bins = Math.max(4, Math.floor(have / 2));
      api.renderAll();
      const fewer = api.snapshotHistogramFor(histComp);
      check(fewer && fewer.bins.length <= Math.max(4, Math.floor(have / 2)),
        "改成更少的格数之后还是原来那么多格子");

      // 窗口：快照带的是整场 + 每条完整圈，切换要能换出另一份计数
      const lapKey = (embeddedHist.windows || []).find((w) => w.key !== "all");
      check(!!lapKey, "快照里的直方图没有按圈算过的窗口（应该带上每条完整圈）");
      if (lapKey) {
        histComp.config.window = lapKey.key;
        api.renderAll();
        const lapSnap = api.snapshotHistogramFor(histComp);
        check(!!lapSnap && lapSnap.count < snap.count,
          "切到某一条圈之后，样本数没有变成那一条圈的样本数");
      }
      histComp.config.window = "all";
      histComp.config.bins = 40;
      api.renderAll();

      // 快照里没带色值：要说清楚"色只能 serve 模式看"，而不是默默不画
      const colourName = (api.data.channels || []).map((ch) => ch.name)
        .find((name) => name !== histComp.config.channel);
      histComp.config.colour = colourName || null;
      api.renderAll();
      const colourHead = String(insideOf(histEl, "graphhead")[0].textContent);
      if (histComp.config.colour) {
        check(colourHead.indexOf("色=") >= 0 && colourHead.indexOf("serve") >= 0,
          "选了着色通道却没告诉用户快照里看不到: " + colourHead);
      }
      histComp.config.colour = null;

      // 换一条快照没带的通道：要给一句能照做的话，而不是一片空白
      const missing = (api.data.channels || []).map((ch) => ch.name)
        .find((name) => !embeddedHist.series[name]);
      if (missing) {
        histComp.config.channel = missing;
        api.renderAll();
        const swapped = api.snapshotHistogramFor(histComp);
        check(!!swapped && swapped.substituted === true && swapped.requested === missing
          && swapped.channel !== missing,
          "快照里没带这条通道时，要换一条真带了的，并记住原来挑的是谁: "
          + JSON.stringify(swapped && { channel: swapped.channel,
                                        requested: swapped.requested,
                                        substituted: swapped.substituted }) + " 挑的是 " + missing);
        const hintText = String(insideOf(histEl, "graphhead")[0].textContent);
        check(hintText.indexOf("serve") >= 0 && hintText.indexOf("没带") >= 0,
          "缺数据时没给出能照做的提示: " + JSON.stringify(hintText));
      }
      histComp.config.channel = snap.channel;
      api.renderAll();
    }

    // serve 模式：参数一个都不能少，而且缩放变了要重新问
    const savedHistApi = api.data.api;
    api.data.api = "/api";
    histComp.config.gate = "Vx KF";
    histComp.config.colour = "G Force Lat";
    histComp.config.window = "zoom";
    api.bundleOf(histComp).dataKey = "";
    const beforeHist = httpCalls.length;
    api.refreshComponentData(histComp, true).then(() => {}, () => {});
    const histCalls = httpCalls.slice(beforeHist)
      .filter((call) => call.url.indexOf("/histogram") >= 0);
    check(histCalls.length === 1,
      "serve 模式下没有向 /histogram 要分布（拿到 " + histCalls.length + " 个请求）");
    if (histCalls.length === 1) {
      const url = decodeURIComponent(histCalls[0].url);
      check(url.indexOf("channel=") >= 0 && url.indexOf("bins=") >= 0
        && url.indexOf("from=") >= 0 && url.indexOf("to=") >= 0,
        "直方图请求少了 channel / bins / from / to: " + url);
      check(url.indexOf("gate=Vx KF") >= 0 && url.indexOf("colour=G Force Lat") >= 0,
        "直方图请求没带门槛 / 着色通道: " + url);
    }
    // 缩放之后是另一个问题，必须重新问一次（不能拿旧窗口的分布糊弄）
    const histKeyBefore = api.histogramKey(histComp);
    const savedView = state.view;
    state.view = [10, 20];
    check(api.histogramKey(histComp) !== histKeyBefore,
      "缩放之后直方图的刷新键没变：会拿旧窗口的分布糊弄");
    state.view = savedView;
    api.data.api = savedHistApi;
    histComp.config.gate = "";
    histComp.config.colour = null;

    // 布局要靠 URL 带走：通道 / 格数 / 画法 / 色 / 门槛 / 窗口一个不落
    histComp.config.channel = histComp.config.channel || "Vx KF";
    histComp.config.style = "line";
    histComp.config.gate = "Vx KF";
    histComp.config.colour = "G Force Lat";
    const histEncoded = api.encodeLayout(state.components);
    const histDecoded = api.decodeLayout(histEncoded);
    const back = histDecoded.find((comp) => comp.type === "histogram");
    check(!!back && back.config.channel === histComp.config.channel
      && back.config.style === "line" && back.config.gate === "Vx KF"
      && back.config.colour === "G Force Lat"
      && back.config.bins === histComp.config.bins,
      "分享链接丢了直方图的配置: " + JSON.stringify(back && back.config));
    histComp.config.style = "bars";
    histComp.config.gate = "";
    histComp.config.colour = null;
    api.renderAll();
  }
}

/* 28. 频谱（#10）：快照离线可用 / serve 参数一个不少 / 布局能带走
 *
 * 频谱和直方图一个规矩：算在服务端（按通道**自己的采样率**）。所以这里同样分两半验：
 * 快照必须离线可用，而且要**说清**只有整场那默认一份；serve 必须把通道、点数、窗、
 * 重叠、平滑、窗口一个不少地带上。
 */
const embeddedSpec = api.data.spectra;
const specCallsAtStart = httpCalls.length;
check(!!embeddedSpec && !!embeddedSpec.series
  && Object.keys(embeddedSpec.series).length > 0,
  "快照载荷里没有频谱：导出快照时要带上 spectra（整场那一份）");
if (embeddedSpec && embeddedSpec.series) {
  api.applyPreset("底盘");
  const specComp = state.components.find((c) => c.type === "spectrum");
  check(!!specComp,
    "「底盘」预设里没有频谱组件：" + state.components.map((c) => c.type).join(","));
  if (specComp) {
    api.syncSpectrumSelectors();
    api.renderAll();
    check(!!specComp.config.channel,
      "频谱没有默认通道（加进工作表时应该挑一条悬架 / 加速度通道）");
    const specEl = SHEETEl(worksheet, specComp.id);
    const specSelects = insideOf(specEl, "scattercfg")[0]._children
      .filter((child) => child.tagName === "SELECT");
    check(specSelects.length === 8,
      "频谱的配置栏应该有 8 个下拉（通道 / 对比 / 点数 / 窗 / 重叠 / 纵轴 / 窗口 …），实际 "
      + specSelects.length);
    check(specSelects.length > 0 && String(specSelects[0]._html).indexOf("<optgroup") >= 0,
      "频谱的通道下拉没有按单位分组：一场 400 多条通道平铺没法找");

    // 快照：只有整场那一份，参数控件必须灰掉（而不是让用户改完没反应）
    const snapSpec = api.snapshotSpectrumFor(specComp);
    check(!!snapSpec && snapSpec.power.length > 0,
      "快照里取不到这条通道的频谱（导出时应该把勾选的通道都算一份）");
    if (snapSpec) {
      const fields = insideOf(specEl, "scattercfg")[0]._children;
      const pointsBox = fields.filter((child) => child.dataset
        && child.dataset.spec === "points")[0];
      const winBox = fields.filter((child) => child.dataset
        && child.dataset.spec === "win")[0];
      check(!!pointsBox && pointsBox.disabled === true,
        "快照模式下「点数」应该是灰的：只有 serve 模式能换点数");
      check(!!winBox && winBox.disabled === true,
        "快照模式下「窗函数」应该是灰的：只有 serve 模式能换窗");
      const head = String(insideOf(specEl, "graphhead")[0].textContent);
      check(head.indexOf(specComp.config.channel) >= 0 && head.indexOf("Hz") >= 0
        && head.indexOf("段") >= 0 && head.indexOf("主频") >= 0,
        "频谱表头没写清它算了什么: " + head);
      check(head.indexOf("serve") >= 0,
        "快照里只有一份频谱，表头必须说清「换参数要 serve 模式」: " + head);
      check(!httpCalls.slice(specCallsAtStart)
        .some((call) => call.url.indexOf("/spectrum") >= 0),
        "快照模式下去请求了 /spectrum：快照必须离线可用");

      // 峰值必须落在内嵌曲线自己的最大值上——不然表头报的主频是编的
      let peakAt = 0;
      snapSpec.power.forEach((value, i) => {
        if (value > snapSpec.power[peakAt]) peakAt = i;
      });
      check(Math.abs(snapSpec.frequencies[peakAt] - snapSpec.peak_frequency) < 1e-9,
        "内嵌频谱的峰值频率和曲线对不上：" + snapSpec.peak_frequency
        + " vs " + snapSpec.frequencies[peakAt]);

      // 有效值换算：√(Σ A²) 必须等于 √(Σ P·Δf)——同一份数据的两种写法，
      // 换算写错会让"有效值"这个轴整体偏 √2 倍。
      const df = snapSpec.resolution;
      const rmsFromPsd = Math.sqrt(snapSpec.power.reduce((sum, p) => sum + p * df, 0));
      const rmsFromAmp = Math.sqrt(api.spectrumY(snapSpec, { scale: "amplitude" }, -200)
        .reduce((sum, a) => sum + a * a, 0));
      check(Math.abs(rmsFromPsd - rmsFromAmp) < rmsFromPsd * 1e-9 + 1e-12,
        "有效值换算和功率谱对不上：" + rmsFromPsd + " vs " + rmsFromAmp);

      // 换一条快照没带的通道：要给一句能照做的话，而不是一片空白
      const missingSpec = (api.data.channels || []).map((ch) => ch.name)
        .find((name) => !embeddedSpec.series[name]);
      if (missingSpec) {
        specComp.config.channel = missingSpec;
        api.renderAll();
        const swappedSpec = api.snapshotSpectrumFor(specComp);
        check(!!swappedSpec && swappedSpec.substituted === true
          && swappedSpec.requested === missingSpec && swappedSpec.channel !== missingSpec,
          "快照里没带这条通道时，要换一条真带了的，并记住原来挑的是谁: "
          + JSON.stringify(swappedSpec && { channel: swappedSpec.channel,
                                            requested: swappedSpec.requested,
                                            substituted: swappedSpec.substituted })
          + " 挑的是 " + missingSpec);
        const hintText = String(insideOf(specEl, "graphhead")[0].textContent);
        check(hintText.indexOf("serve") >= 0 && hintText.indexOf("没带") >= 0,
          "缺数据时没给出能照做的提示: " + JSON.stringify(hintText));
      }
      specComp.config.channel = snapSpec.channel;
      api.renderAll();
    }

    // serve 模式：参数一个不少，而且缩放变了要重新问
    const savedSpecApi = api.data.api;
    api.data.api = "/api";
    specComp.config.span = "zoom";
    specComp.config.points = 2048;
    specComp.config.win = "blackman";
    specComp.config.overlap = 0.75;
    specComp.config.smooth = 3;
    specComp.config.against = null;
    // 纵轴只是显示方式，服务端一律回功率谱密度——请求里必须是 scale=psd
    specComp.config.scale = "amplitude";
    api.bundleOf(specComp).dataKey = "";
    const beforeSpec = httpCalls.length;
    api.refreshComponentData(specComp, true).then(() => {}, () => {});
    const specCalls = httpCalls.slice(beforeSpec)
      .filter((call) => call.url.indexOf("/spectrum") >= 0);
    check(specCalls.length === 1,
      "serve 模式下没有向 /spectrum 要频谱（拿到 " + specCalls.length + " 个请求）");
    if (specCalls.length === 1) {
      const url = decodeURIComponent(specCalls[0].url);
      check(url.indexOf("channel=") >= 0 && url.indexOf("points=") >= 0
        && url.indexOf("window=") >= 0 && url.indexOf("overlap=") >= 0
        && url.indexOf("smooth=") >= 0 && url.indexOf("from=") >= 0
        && url.indexOf("to=") >= 0,
        "频谱请求少了 channel / points / window / overlap / smooth / from / to: " + url);
      check(url.indexOf("points=2048") >= 0 && url.indexOf("window=blackman") >= 0
        && url.indexOf("overlap=0.75") >= 0 && url.indexOf("smooth=3") >= 0,
        "频谱请求没带上用户选的参数: " + url);
      check(url.indexOf("scale=psd") >= 0,
        "频谱请求应该一律要功率谱密度（纵轴是显示换算）: " + url);
    }
    const specKeyBefore = api.spectrumKey(specComp);
    const savedSpecView = state.view;
    state.view = [10, 20];
    check(api.spectrumKey(specComp) !== specKeyBefore,
      "缩放之后频谱的刷新键没变：会拿旧窗口的频谱糊弄");
    // 换窗函数同样要重新问（点数/重叠/平滑也一样，这里验一个代表）
    state.view = savedSpecView;
    const keyBeforeWin = api.spectrumKey(specComp);
    specComp.config.win = "hamming";
    check(api.spectrumKey(specComp) !== keyBeforeWin,
      "换窗函数之后频谱的刷新键没变：会拿旧窗的频谱糊弄");
    api.data.api = savedSpecApi;
    specComp.config.points = embeddedSpec.points || 1024;
    specComp.config.win = embeddedSpec.window || "hann";
    specComp.config.overlap = embeddedSpec.overlap === undefined ? 0.5 : embeddedSpec.overlap;
    specComp.config.smooth = 1;
    specComp.config.scale = "psd";
    specComp.config.span = "all";

    // 布局要靠 URL 带走：通道、对比通道、点数、窗、重叠、平滑、纵轴、窗口一个不落
    specComp.config.channel = snapSpec ? snapSpec.channel : specComp.config.channel;
    specComp.config.against = "G Force Lat";
    specComp.config.points = 4096;
    specComp.config.win = "flattop";
    specComp.config.overlap = 0.75;
    specComp.config.smooth = 5;
    specComp.config.scale = "amplitude";
    specComp.config.axis = "linear";
    const specEncoded = api.encodeLayout(state.components);
    const specDecoded = api.decodeLayout(specEncoded);
    const specBack = specDecoded.find((comp) => comp.type === "spectrum");
    check(!!specBack && specBack.config.channel === specComp.config.channel
      && specBack.config.against === "G Force Lat"
      && specBack.config.points === 4096
      && specBack.config.win === "flattop"
      && specBack.config.overlap === 0.75
      && specBack.config.smooth === 5
      && specBack.config.scale === "amplitude"
      && specBack.config.axis === "linear",
      "分享链接丢了频谱的配置: " + JSON.stringify(specBack && specBack.config));
    specComp.config.against = null;
    specComp.config.win = embeddedSpec.window || "hann";
    specComp.config.scale = "psd";
    specComp.config.axis = "log";
    specComp.config.smooth = 1;
    specComp.config.overlap = embeddedSpec.overlap === undefined ? 0.5 : embeddedSpec.overlap;
    api.renderAll();
  }

  // 29. 横轴随缩放换档：刻度步长取 1/2/5×10ⁿ，标签落在整齐的数上
  //     （原先横轴永远四等分，放大到 0.5 s 也只是把同一个区间再切四刀）
  {
    const ladder = api.niceTicks(231.7, 232.2, 8, true);
    check(Math.abs(ladder.step - 0.1) < 1e-12,
      "zoomed-in window did not pick a 0.1 s step, got " + ladder.step);
    check(ladder.values.length >= 4 && ladder.values.every(
      (v) => v >= 231.7 - 1e-9 && v <= 232.2 + 1e-9),
      "ticks escaped the visible window: " + JSON.stringify(ladder.values));
    check(ladder.values.every((v) => Math.abs(v / ladder.step - Math.round(v / ladder.step)) < 1e-6),
      "ticks are not on round multiples of the step: " + JSON.stringify(ladder.values));

    // 时间轴上的"整齐"是整分整秒：500 s 的窗口该给 2 分钟一档，而不是 50 秒
    const whole = api.niceTicks(0, 500, 8, true);
    check(Math.abs(whole.step - 120) < 1e-9,
      "a 500 s window should use 120 s steps, got " + whole.step);
    check(whole.values.length === 5, "expected 0..480 in five ticks, got " + whole.values.length);
    const half = api.niceTicks(0, 300, 8, true);
    check(Math.abs(half.step - 60) < 1e-9, "a 300 s window should use 1 min steps, got " + half.step);
    // 距离轴没有"整分钟"这回事，仍旧用 1/2/5×10ⁿ
    check(Math.abs(api.niceTicks(0, 500, 8, false).step - 100) < 1e-9,
      "the distance axis should keep the 1/2/5 ladder");

    // "随缩放自适应"的核心断言：窗口缩小 → 步长必须跟着变小（单调不增）
    const spans = [600, 300, 120, 60, 30, 10, 5, 2, 1, 0.5, 0.2, 0.1];
    const steps = spans.map((span) => api.niceTicks(100, 100 + span, 8, true).step);
    for (let i = 1; i < steps.length; i++) {
      check(steps[i] <= steps[i - 1] + 1e-12,
        "tick step grew while zooming in: " + spans[i - 1] + "s → " + steps[i - 1]
        + ", " + spans[i] + "s → " + steps[i]);
    }
    check(steps[0] > steps[steps.length - 1],
      "the whole-session step must be coarser than the zoomed-in one");
    check(steps.every((step) => [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
      1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
      .some((nice) => Math.abs(step - nice) < 1e-12)),
      "a time step is not on the clock ladder: " + JSON.stringify(steps));

    // 标签跟着档位换：整场读成 mm:ss，放大到亚秒才给小数
    check(api.axisTickLabel(125, 5, 0, true) === "2:05",
      "a 5 s step should print mm:ss, got " + api.axisTickLabel(125, 5, 0, true));
    check(api.axisTickLabel(3661, 60, 0, true) === "61:01",
      "minutes are not allowed to wrap into hours silently: "
      + api.axisTickLabel(3661, 60, 0, true));
    check(api.axisTickLabel(231.8, 0.1, 1, true) === "231.80",
      "a sub-second step needs decimals: " + api.axisTickLabel(231.8, 0.1, 1, true));
    check(api.axisTickLabel(1234.5, 0.1, 1, false) === "1234.5",
      "the distance axis must not print clock labels: "
      + api.axisTickLabel(1234.5, 0.1, 1, false));

    // 画布上真的换了：缩到 0.5 s 之后，横向标签数量与内容都跟整场不一样
    const labelsFor = (from, to) => {
      api.zoomTo(from, to);
      const before = calls.fillText;
      api.renderAll();
      return calls.fillText - before;
    };
    const wholeLabels = labelsFor(0, 463.99);
    const zoomLabels = labelsFor(231.7, 232.2);
    check(wholeLabels > 0 && zoomLabels > 0, "no axis labels were drawn at all");
    api.zoomTo(0, 463.99);
  }

  // 30. 注释（#15）：它有自己的侧车，图上是虚线，列表里能就地改，快照只读。
  //     位置由 Python 算好（distance / x / y），这里验的是"画得出来、改得进去、
  //     而且不碰圈速表"——真鼠标那几种状态归 tools/verify_clicks.py。
  {
    const notesHost = registry.get("notesList");
    const lapRowsBefore = (registry.get("lapTable")._rows || []).length;

    const comp = api.state.components.find((c) => c.type === "graph")
      || api.state.components[0];
    const ctx = api.bundleOf(comp).canvas.getContext("2d");
    const pad = { l: 64, r: 18, t: 6, b: 18 };

    // 快照里没有 DATA.notes 时：列表给一句能照做的提示，按钮是禁的
    api.state.notes = [];
    api.renderNotes();
    check(notesHost.querySelectorAll("input[data-note-text]").length === 0,
      "空注释表不该有输入框");
    check(registry.get("addNote").disabled === !api.data.api,
      "快照模式下「＋ 注释」该是禁用的");
    check(String(notesHost.innerHTML).indexOf("还没") >= 0
      || String(notesHost.innerHTML).indexOf("没有注释") >= 0,
      "空注释表要给一句提示，现在写的是: " + notesHost.innerHTML);

    // 一条注释：列表、计数、图上的虚线都该出来
    api.state.notes = [{ time: 231.74, text: "这里换了刹车点", distance: 987.6,
                         x: 12.3, y: 4.5 }];
    api.renderNotes();
    const boxes = notesHost.querySelectorAll("input[data-note-text]");
    check(boxes.length === 1 && boxes[0].value === "这里换了刹车点",
      "注释没进列表: " + JSON.stringify(boxes.map((b) => b.value)));
    check(String(registry.get("notesCount").textContent).indexOf("1") >= 0,
      "注释条数没显示: " + registry.get("notesCount").textContent);

    api.state.mode = "time";
    let seen = { stroke: calls.stroke, fillText: calls.fillText, fillRect: calls.fillRect };
    api.drawNotes(ctx, pad, 800, 200, 200, 260, true);
    check(calls.stroke > seen.stroke && calls.fillText > seen.fillText
      && calls.fillRect > seen.fillRect,
      "时间轴上的注释没画出来（虚线 / 文字 / 底色至少要各画一次）");
    // 窗口之外不画：范围是 200–260 s，注释在 231.74 s 之外时一条线都不该有
    seen = { stroke: calls.stroke, fillText: calls.fillText };
    api.drawNotes(ctx, pad, 800, 200, 0, 100, true);
    check(calls.stroke === seen.stroke && calls.fillText === seen.fillText,
      "窗口外的注释不该画出来");
    // 「显示」关掉之后，画布上一个像素都不该多
    api.state.showNotes = false;
    seen = { stroke: calls.stroke, fillText: calls.fillText, fillRect: calls.fillRect };
    api.drawNotes(ctx, pad, 800, 200, 200, 260, true);
    check(calls.stroke === seen.stroke && calls.fillText === seen.fillText
      && calls.fillRect === seen.fillRect,
      "关掉「显示」之后注释还在画");
    api.state.showNotes = true;

    // 距离轴：用 Python 算好的 distance 落位；没算出来（null）就不画，不猜
    api.state.mode = "distance";
    seen = { stroke: calls.stroke };
    api.drawNotes(ctx, pad, 800, 200, 900, 1100, false);
    check(calls.stroke > seen.stroke, "距离轴上的注释没按 distance 落位");
    api.state.notes = [{ time: 231.74, text: "没有距离", distance: null }];
    seen = { stroke: calls.stroke };
    api.drawNotes(ctx, pad, 800, 200, 900, 1100, false);
    check(calls.stroke === seen.stroke, "没有距离就不该猜一个位置画出来");

    // 注释不是信标：加了注释，圈速表一行都不能变
    api.state.mode = "time";
    api.renderAll();
    check((registry.get("lapTable")._rows || []).length === lapRowsBefore,
      "注释不该动圈速表：加之前 " + lapRowsBefore + " 行，加之后 "
      + (registry.get("lapTable")._rows || []).length + " 行");

    // 就地改文字：回车提交走 saveNotes（快照模式没有服务，所以只是本地回写）
    const box = notesHost.querySelectorAll("input[data-note-text]")[0];
    const kept = box.value;
    box.value = "改过的字";
    box.dispatch("input", {});
    box.dispatch("keydown", { key: "Enter", preventDefault() {} });
    check(box.value === "改过的字", "改注释文字的输入框状态不对: " + box.value);
    box.dispatch("keydown", { key: "Escape", preventDefault() {} });
    check(box.value === kept, "Esc 之后输入框该弹回原文字，现在是 " + box.value);

    api.state.notes = (api.data.notes || []).slice();
    api.renderNotes();
    api.renderAll();
  }

  // 31. GPS 校正（#14）：坏定位**永远**标注（那是数据质量的事实），开关只决定
  //     要不要按时移 / 插值去修正；轨迹图上不许跨着空档或跳点连线。
  {
    const gpsNote = registry.get("gpsNote");
    check(!!gpsNote, "GPS 校正面板没建出来");

    api.state.gps = {
      config: { enabled: false, offset_s: 0, offset_ratio: 0, resample: false,
                spike_kmh: 200, gap_s: 1, scope_track: true, scope_laps: true,
                scope_distance: false },
      stored: false, error: null, applied: null, notice: null,
      summary: { samples: 38860, no_fix: 638, low_sats: 0, jumps: 1, worst_jump_m: 214.48,
                 holes: 2, longest_hole_s: 279.4, segments: 3, spike_kmh: 200,
                 gap_s: 1, rate: 20 },
    };
    api.renderGps();
    const off = String(gpsNote.textContent);
    check(off.indexOf("跳点 1 处") >= 0 && off.indexOf("214.5") >= 0,
      "面板没念出跳点: " + off);
    check(off.indexOf("空定位 638 点") >= 0, "面板没念出空定位: " + off);
    check(off.indexOf("空档 2 段") >= 0 && off.indexOf("279.4") >= 0,
      "面板没念出空档: " + off);
    check(off.indexOf("启用校正") >= 0, "没开的时候要说清怎么开: " + off);
    check(registry.get("gpsEnabled").checked === false, "缺省就不是开启");
    check(Number(registry.get("gpsSpike").value) === 200, "阈值没填进输入框");

    api.state.gps = {
      config: { enabled: true, offset_s: 0.25, offset_ratio: 0, resample: true,
                spike_kmh: 800, gap_s: 1, scope_track: true, scope_laps: true,
                scope_distance: false },
      stored: true, error: null, notice: null,
      summary: { samples: 38860, no_fix: 638, low_sats: 0, jumps: 1, worst_jump_m: 214.48,
                 holes: 2, longest_hole_s: 279.4, segments: 3, spike_kmh: 800,
                 gap_s: 1, rate: 20 },
      applied: { enabled: true, offset_s: 0.25, resampled: true, samples_before: 38860,
                 samples: 194292, segments: 4, breaks: 4, scope_track: true, scope_laps: true,
                 scope_distance: false },
    };
    api.renderGps();
    const on = String(gpsNote.textContent);
    check(on.indexOf("已应用") >= 0 && on.indexOf("0.25") >= 0,
      "开了之后要念出改了什么: " + on);
    check(on.indexOf("194292") >= 0 && on.indexOf("断开 4 处") >= 0,
      "插值点数与断开段数都要念出来: " + on);
    check(registry.get("gpsEnabled").checked === true
      && registry.get("gpsResample").checked === true,
      "配置没回填到复选框");
    check(registry.get("gpsScopeDistance").checked === false,
      "距离轴缺省不跟着变（圈速/区段/报表都建在速度积分的距离轴上）");
    check(registry.get("gpsApply").disabled === !api.data.api,
      "快照模式下「应用」该是禁用的");

    const trackComp = api.state.components.find((c) => c.type === "track");
    check(!!trackComp, "这张工作表里没有轨迹组件，断线那条没验到");
    if (trackComp) {
      const b = api.bundleOf(trackComp);
      const xs = [], ys = [], sp = [], tm = [];
      for (let i = 0; i < 21; i++) {
        xs.push(i * 5); ys.push((i % 5) * 3); sp.push(40 + i); tm.push(i * 0.1);
      }
      trackComp.config.window = "all";
      api.state.mode = "time";
      api.state.track = { x: xs, y: ys, speed: sp, time: tm, breaks: [], jump_times: [],
                          holes: [], speed_channel: "Vx KF", origin: [22.6, 114.0] };
      let seen = calls.lineTo;
      api.renderTrackComponent(trackComp, b);
      const whole = calls.lineTo - seen;
      check(whole > 0, "整条轨迹一段线都没画");

      api.state.track.breaks = [7, 13];
      seen = calls.lineTo;
      api.renderTrackComponent(trackComp, b);
      check(calls.lineTo - seen === whole - 2,
        "断开两处就该少画两段线：整条 " + whole + "，断开后 " + (calls.lineTo - seen));
      check(String(b.head.textContent).indexOf("断开 2 处") >= 0,
        "抬头要说清断了几处: " + b.head.textContent);

      api.state.track.jump_times = [0.6];
      const arcs = calls.arc;
      api.renderTrackComponent(trackComp, b);
      check(calls.arc > arcs, "跳点要在轨迹图上画成红点");

      // 没有 breaks 字段的旧载荷不能崩：老快照 / 老接口照旧画得出来
      api.state.track = { x: xs, y: ys, speed: sp, time: tm, speed_channel: "Vx KF",
                          origin: [22.6, 114.0] };
      seen = calls.lineTo;
      api.renderTrackComponent(trackComp, b);
      check(calls.lineTo - seen === whole, "没有 breaks 字段时该按整条画");
    }
    api.state.track = (api.data && api.data.track) || null;
    api.state.gps = (api.data && api.data.gps) || null;
    api.renderGps();
  }
}

  // 32. 组件类型注册表（#17）：每种显示形式只在一处声明自己，工作表不再认类型。
  //     这一组要证明两件事：三类已迁移的形式**靠声明**就能被认出来；以及
  //     "加一种显示形式"真的只要一条声明——`caption` 只存在于注册表里。
  check(!!(api.componentTypes && api.decodeLayout && api.componentSpec),
    "调试句柄没有导出组件注册表（#17）");
  if (api.componentTypes && api.decodeLayout) {
    const state = api.state;
    const worksheet = registry.get("worksheet");
    const types = api.componentTypes;
    check(!!(types.delta && types.delta.render && types.delta.label),
      "Δ 的声明不完整（至少要 label 与 render）");
    // 取数那件事认 `needs` 或 `data` 两种写法：#21 会把它们收成一个 seam，
    // 那时候该改的是它，不该让这条断言红。
    check(!!(types.status && types.status.render
      && (types.status.needs || types.status.data) && types.status.hotkey),
      "状态与故障的声明不完整（render / 取数 / hotkey）");
    check(!!(types.track && types.track.defaults && types.track.controls && types.track.hooks
      && (types.track.data || types.track.refreshWindow) && types.track.encode
      && types.track.decode && types.track.render),
      "赛道轨迹的声明不完整（默认配置 / 控件 / 事件 / 取数 / 分享链接 / 画）");
    check(api.componentSpec({ type: "没这个类型" }) === null,
      "没声明过的类型该给 null，而不是猜一个");
    check(api.componentTypeWithHotkey("e") === "status",
      "E 键该由注册表里声明了 hotkey 的那个形式接管");

    // 三类形式的配置要经得起分享链接往返（#17 验收条目之一）。
    const probes = [
      { id: "probe1", type: "track", x: 0, y: 1, w: 4, h: 13,
        config: { channel: "GPS Speed", window: "zoom" } },
      { id: "probe2", type: "status", x: 4, y: 1, w: 12, h: 4, config: {} },
      { id: "probe3", type: "delta", x: 0, y: 5, w: 12, h: 8, config: {} },
    ];
    const back = api.decodeLayout(api.encodeLayout(probes));
    check(!!back && back.length === 3, "三类形式的分享链接往返丢了组件");
    if (back && back.length === 3) {
      check(back.map((c) => c.type).join(",") === "track,status,delta",
        "三类形式的分享链接往返换了类型或次序");
      check(back[0].config.channel === "GPS Speed" && back[0].config.window === "zoom",
        "轨迹的分享链接往返没保住 config（通道 " + back[0].config.channel
        + " / 窗口 " + back[0].config.window + "）");
      check(back[0].x === 0 && back[0].y === 1 && back[0].w === 4 && back[0].h === 13,
        "三类形式的分享链接往返没保住位置与尺寸");
    }
    // 老链接里的轨迹载荷只有一个通道名（没有 "|窗口"），得按"整场"解出来。
    const legacyLink = Buffer.from(JSON.stringify([["track", 0, 0, 4, 13, "Vx KF"]]))
      .toString("base64");
    const legacy = api.decodeLayout(legacyLink);
    check(!!legacy && legacy.length === 1 && legacy[0].config.channel === "Vx KF"
      && (legacy[0].config.window || "all") === "all",
      "老分享链接（轨迹载荷里没有窗口）该照旧解成整场轨迹");

    // 一条声明就够：把"只有标题"形式声明进注册表之后，添加下拉、标题、默认配置、
    // 通用渲染、分享链接全都认识它——工作表里没有一处为它写过分支。
    const addSel = registry.get("addType");
    check(addSel && String(addSel._html).indexOf('value="caption"') >= 0,
      "注册表里的显示形式没有出现在「＋ 添加组件」的下拉里");
    const beforeAdd = state.components.length;
    const focusBefore = state.focusId;
    api.addComponentOfType("caption");
    const caption = state.components[state.components.length - 1];
    check(state.components.length === beforeAdd + 1 && caption && caption.type === "caption",
      "加一个只声明过的显示形式失败了");
    check(caption && caption.config.text === "自检",
      "新形式的默认配置没有从声明里来");
    check(caption && api.componentTitle(caption).indexOf("只有标题（自检）") === 0,
      "新形式的标题没有从声明里来");
    check(!!(caption && SHEETEl(worksheet, caption.id)),
      "新形式没有被工作表画出来");
    const captionBack = api.decodeLayout(api.encodeLayout([caption]));
    check(!!captionBack && captionBack.length === 1 && captionBack[0].config.text === "自检",
      "只声明过的显示形式进不了分享链接");
    // 收尾：把它拿掉并重建工作表，后面的断言看到的工作表与加它之前一样。
    if (caption) state.components.splice(state.components.indexOf(caption), 1);
    state.focusId = focusBefore;
    api.buildWorksheet();
    api.renderAll();

    // 旧布局（localStorage 里存着上个版本的类型清单）不能把 restore 弄崩：
    // 没声明过的类型在解码时被丢掉，剩下的照旧进来。
    const mixed = Buffer.from(JSON.stringify([
      ["没这个类型", 0, 0, 4, 13, ""], ["delta", 0, 0, 12, 8, ""],
    ])).toString("base64");
    const kept = api.decodeLayout(mixed);
      check(!!kept && kept.length === 1 && kept[0].type === "delta",
        "分享链接里没声明过的类型该被丢掉，而不是整条链接解不出来");
  }

  // 33. 图表类迁进注册表（#19）+ 取数收成一条路径（#21）+ 表格与仪表类收口（#20）。
  //     要证明三件事：五类图表形式的声明是完整的；两张同类同屏不会互相顶掉；
  //     脚本里没有第二条取数的路。
  check(!!(api.apiUrl && api.apiGet && api.refreshComponentData && api.componentTypes),
    "调试句柄没有导出取数入口（#21）");
  if (api.componentTypes && api.apiUrl) {
    const types2 = api.componentTypes;
    const charts = ["graph", "scatter", "histogram", "spectrum", "track"];

    // 收口：每一种显示形式都得声明 label 与 render —— 表是「有哪些显示形式」
    // 的唯答案；表格类还必须声明 table（服务端就是按它分两套列的）。
    for (const t of Object.keys(types2)) {
      const spec = types2[t];
      check(typeof spec.label === "string" && spec.label.length > 0
        && typeof spec.render === "function",
        t + " 的声明缺 label 或 render（工作表就认不出它）");
      if (spec.tabular) {
        check(spec.table === "time" || spec.table === "channels",
          t + " 是表格却没声明 table");
      }
    }
    // 五类图表形式：各自那几件事都要在表里（不再散在 buildWorksheet / render 里）。
    for (const t of charts) {
      const spec = types2[t];
      check(!!(spec && spec.defaults && spec.encode && spec.decode && spec.render),
        "图表类 " + t + " 的声明不完整（默认配置 / 分享链接 / 画）");
    }
    for (const t of ["scatter", "histogram", "spectrum", "track"]) {
      const d = types2[t] && types2[t].data;
      check(!!(d && d.key && d.load && d.error),
        t + " 没声明取数（键 / 去哪里要 / 出错说什么）");
    }
    check(!!(types2.scatter.controls && types2.histogram.controls
      && types2.spectrum.controls && types2.gauge.controls
      && types2.report.controls && types2.chreport.controls),
      "控件条还有没迁进注册表的类型");
    check(!!(types2.scatter.sync && types2.histogram.sync
      && types2.spectrum.sync && types2.gauge.sync),
      "下拉填值还有没迁进注册表的类型");
    check(!!(types2.graph.sidebar && types2.chreport.sidebar),
      "侧边栏通道清单的归属没有声明");

    // 取数只有一条路：地址由 apiUrl 拼、请求由 apiGet 发。
    const urlApiSaved = api.data.api;
    api.data.api = "/api";                     // 拼地址要处在 serve 模式才有前缀
    const pointsUrl = api.apiUrl("/points", { channels: "a,b", from: 1.5, to: 2, max: 20000 });
    check(pointsUrl === "/api/session/" + encodeURIComponent(api.data.session)
        + "/points?channels=a%2Cb&from=1.5&to=2&max=20000",
      "apiUrl 拼出来的地址不对: " + pointsUrl);
    const thinUrl = api.apiUrl("/points", { channels: "a", from: undefined, to: null, max: "" });
    check(thinUrl.indexOf("?channels=a") >= 0 && thinUrl.indexOf("from=") < 0
      && thinUrl.indexOf("max=") < 0,
      "apiUrl 把空参数也写进地址了: " + thinUrl);
    api.data.api = urlApiSaved;
    // 六个组件数据端点谁都不许自己发请求（脚本里搜得到就是绕过了这条路）。
    const stray = script.split("\n").filter(
      (line) => /fetch\([^\n]*"(points|histogram|spectrum|track|report|trace)"/.test(line));
    check(stray.length === 0,
      "还有组件取数绕过了 apiGet（#21）:\n    " + stray.join("\n    "));
    // 工作表里的类型分派：收口之后只剩「哪个组件是图」（9 处）与「哪一列是
    // 文字列」（1 处，报表排版）这两类语义判断，合计实测 10 处。
    const dispatch = (script.match(/\.type === "/g) || []).length
      + (script.match(/\.type !== "/g) || []).length;
    check(dispatch <= 10,
      "工作表里还有 " + dispatch + " 处按类型分派（#20 收口前是 80 处）："
      + "新的分派要么改成声明，要么把这条门限与理由一起写进验收条目");

    // 五类图表形式的配置要经得起分享链接往返（#19 验收条目之一）。
    const chartProbes = [
      { id: "p1", type: "graph", x: 0, y: 0, w: 12, h: 8,
        config: { channels: ["Vx KF", "GPS Speed"], mode: "overlapped" } },
      { id: "p2", type: "scatter", x: 0, y: 8, w: 4, h: 13,
        config: { x: "Vx KF", y: ["G Force Lat"], colour: "TH", style: "trend" } },
      { id: "p3", type: "histogram", x: 4, y: 8, w: 6, h: 13,
        config: { channel: "Vx KF", bins: 77, style: "line", colour: "TH",
                  gate: "Vx KF", window: "all" } },
      { id: "p4", type: "spectrum", x: 0, y: 21, w: 6, h: 13,
        config: { channel: "Susp Pos FL", against: "Susp Pos FR", points: 2048,
                  win: "blackman", overlap: 0.6, smooth: 3, scale: "psd",
                  axis: "linear", span: "all" } },
      { id: "p5", type: "track", x: 6, y: 21, w: 4, h: 13,
        config: { channel: "GPS Speed", window: "zoom" } },
    ];
    const chartBack = api.decodeLayout(api.encodeLayout(chartProbes));
    check(!!chartBack && chartBack.length === 5, "五类图表形式的分享链接往返丢了组件");
    if (chartBack && chartBack.length === 5) {
      check(chartBack.map((c) => c.type).join(",") === "graph,scatter,histogram,spectrum,track",
        "五类图表形式的分享链接往返换了类型或次序");
      check(chartBack[0].config.channels.join("|") === "Vx KF|GPS Speed"
        && chartBack[0].config.mode === "overlapped", "图的往返没保住通道或分栏方式");
      check(chartBack[1].config.x === "Vx KF" && chartBack[1].config.y.join("|") === "G Force Lat"
        && chartBack[1].config.colour === "TH" && chartBack[1].config.style === "trend",
        "散点的往返没保住 X / Y / 色 / 画法");
      check(chartBack[2].config.bins === 77 && chartBack[2].config.style === "line"
        && chartBack[2].config.colour === "TH" && chartBack[2].config.gate === "Vx KF"
        && chartBack[2].config.window === "all",
        "直方图的往返没保住格数 / 画法 / 色 / 门槛 / 窗口");
      check(chartBack[3].config.points === 2048 && chartBack[3].config.win === "blackman"
        && chartBack[3].config.overlap === 0.6 && chartBack[3].config.smooth === 3
        && chartBack[3].config.axis === "linear" && chartBack[3].config.span === "all"
        && chartBack[3].config.against === "Susp Pos FR",
        "频谱的往返没保住点数 / 窗 / 重叠 / 平滑 / 纵轴 / 窗口 / 对比通道");
      check(chartBack[4].config.channel === "GPS Speed" && chartBack[4].config.window === "zoom",
        "轨迹的往返没保住通道或窗口");
    }

    // 两张散点同屏：各拿自己那一份数据（#21 的核心 —— 原来它们挤在
    // 全表共用的 state.points 里，谁后问谁把对方顶掉）。
    const sheetState = api.state;
    const savedApiForScatter = api.data.api;
    api.data.api = null;                       // 快照模式：不问服务端，直接塞数据
    api.addComponentOfType("scatter");
    api.addComponentOfType("scatter");
    const scats = sheetState.components.filter((c) => c.type === "scatter").slice(-2);
    const [scatA, scatB] = scats;
    if (scatA && scatB) {
      scatA.config.x = "甲X"; scatA.config.y = ["甲Y"];
      scatB.config.x = "乙X"; scatB.config.y = ["乙Y"];
      const dataA = { time: [0, 1, 2], values: { "甲X": [0, 1, 2], "甲Y": [0, 1, 2] } };
      const dataB = { time: [0, 1, 2, 3, 4], values: { "乙X": [0, 1, 2, 3, 4], "乙Y": [0, 1, 2, 3, 4] } };
      for (const [comp, payload] of [[scatA, dataA], [scatB, dataB]]) {
        const b = api.bundleOf(comp);
        b.data = payload;
        b.dataKey = api.componentSpec(comp).data.key(comp);   // 键吻合 → 重画时不再去要
        b.cacheKey = "";
      }
      api.renderAll();
      check(api.bundleOf(scatA).data === dataA && api.bundleOf(scatB).data === dataB,
        "两张散点共用了一个数据槽（互相顶掉的旧毛病）");
      check(String(api.bundleOf(scatA).head.textContent).indexOf("3 点") === 0,
        "第一张散点画的不是自己那份数据: " + api.bundleOf(scatA).head.textContent);
      check(String(api.bundleOf(scatB).head.textContent).indexOf("5 点") === 0,
        "第二张散点画的不是自己那份数据: " + api.bundleOf(scatB).head.textContent);
      check(api.componentSpec(scatA).data.key(scatA) !== api.componentSpec(scatB).data.key(scatB),
        "两张散点的缓存键一样：换一张的配置会顶掉另一张");
      const ids = [scatA.id, scatB.id];
      sheetState.components = sheetState.components.filter(
        (c) => ids.indexOf(c.id) < 0);
      api.buildWorksheet();
      check(ids.every((id) => {
        const gone = { id: id };
        return api.bundleOf(gone) === undefined;
      }), "删掉散点之后它的数据槽还留着（缓存残留）");
    } else {
      check(false, "加不出两张散点组件");
    }
    api.data.api = savedApiForScatter;
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
