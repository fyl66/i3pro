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
      child._children.forEach((one) => { one._parent = this; });
      this._children.push(...child._children);
      return child;
    }
    if (child) child._parent = this;
    this._children.push(child);
    return child;
  }

  removeChild(child) {
    const i = this._children.indexOf(child);
    if (i >= 0) this._children.splice(i, 1);
    return child;
  }

  get parentNode() { return this._parent || null; }

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
    // 小弹窗（ticket #35）会在关掉时摘掉"点外面就收起来"那个监听；
    // 假 DOM 少了这一半，页面就会在关闭菜单那一行抛错。
    removeEventListener() {},
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
  return {
    window, registry, document, api: window.i3pro, httpCalls: httpCalls,
    //: 页面里的 localStorage 是沙箱里的那个（不是 window 上的），断言要能看见它。
    storage: sandbox.localStorage,
  };
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

/** --dump-worksheets：把页面上的工作表一个个切过去，倒成 JSON，供人比对。
 *
 * 迁移用（ticket #30）：把硬编码的 7 套预设搬进 worksheets/*.json 时，用它把
 * "搬之前"的行为存下来（那时还没有 worksheets 文件），搬完再倒一次逐字段对比。
 * 走的是按钮 + applyPreset 这条路，所以新旧两版页面都能倒。组件 id 是每次运行
 * 现编的（makeComponent 里 ++compSeq），倒出来之前去掉。 */
if (process.argv.indexOf("--dump-worksheets") >= 0) {
  const row = registry.get("presetRow");
  const dumped = {};
  for (const btn of (row ? row._children : [])) {
    api.applyPreset(btn.textContent);
    dumped[btn.textContent] = api.state.components.map((c) => ({
      type: c.type, x: c.x, y: c.y, w: c.w, h: c.h, config: c.config,
    }));
  }
  console.log(JSON.stringify(dumped, null, 1));
  process.exit(0);
}

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

// 两条**互相独立**的前提，别把它们混成一条（混过一次，代价是 CAN 那批日志里
// "车真的跑起来"的那些场次整个跑不了这套断言）：
//
//   hasDistance —— 有没有距离轴。CAN 场次是**算**出来的（拿 Vx_KF 积分，ticket #40）：
//                  车动过的日志有，原地怠速几十秒的那些没有。切圈 / 区段 / 圈差
//                  需要它。
//   hasLaps     —— 有没有圈。CAN 那批日志没有 GPS，也没有人放过信标，所以一条圈
//                  都没有——**即使它有距离轴**。报表 / 按圈分窗的直方图 / "导出当前
//                  选中圈"要的是这个，不是 hasDistance。
//
// 用错前提的后果是断言在错的形状上跑：早先三块都写成 `if (hasDistance)`，于是
// "有距离轴但没圈"的场次（正是 CAN 跑起来的样子）一进来就报四条红。
const hasDistance = !!(api && api.data && api.data.meta && api.data.meta.has_distance);
const hasLaps = !!(api && api.data && (api.data.laps || []).length);

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
  if (hasDistance) {
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
  }                                             // hasDistance：切圈 / 圈差两块到此为止

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
  if (hasDistance) {              // 没有距离轴，就没有自动切出来的信标可以编辑
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
  }                               // hasDistance：信标编辑那一块到此为止
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
      // 要测的是"打不出来的名字"：含空格（``Vx KF``）、点（CAN 解出来的
      // ``Front_Compartment_Sensors.Channel_0``）或短横线的都得能从下拉里选。
      // 早先只找空格——CAN 场次的通道名一个空格都没有，这条在 CAN 上直接报红。
      const awkward = (api.data.channels || []).filter((c) => /[ .\-()]/.test(String(c.name)));
      // 下拉对**任何**名字都加引号，所以没有"难写的名字"时退到第一条通道测整条通路，
      // 有的话优先拿它测（那才是当初加这个下拉的原因）。
      const sample = awkward[0] || (api.data.channels || [])[0] || null;
      check(!!sample, "this snapshot has no channel to test the insert-channel picker with");
      if (sample) {
        const wanted = String(sample.name);
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

    // 说明文字（`note`）是手写在 maths/global.json 里的（界面暂时没有编辑入口），
    // 保存一次就把它抹掉是真发生过的数据损坏：仓库里那 4 条说明全没了，HEAD 里还在。
    if (api.data.api) {
      api.applyMathsResponse({
        definitions: [
          { name: "甲", expr: "1", unit: "", note: "这段说明必须留着", scope: "local" },
          { name: "乙", expr: "2", unit: "", scope: "local" },
        ],
        shadowed: [], errors: [], functions: [],
      });
      state.mathsEdit = { index: 0 };
      const edited = api.mathsSaveList({ name: "甲", expr: "3", unit: "", scope: "local" });
      const editedKept = edited.find((d) => d.name === "甲");
      check(!!editedKept && editedKept.note === "这段说明必须留着",
        "saving wiped the note of the definition being edited: " + JSON.stringify(edited));
      state.mathsEdit = { index: null };
      const added = api.mathsSaveList({ name: "丙", expr: "4", unit: "", scope: "local" });
      const kept = added.find((d) => d.name === "甲");
      check(!!kept && kept.note === "这段说明必须留着",
        "saving another definition wiped a neighbour's note: " + JSON.stringify(added));
      api.closeMathsEditor();
      api.applyMathsResponse({ definitions: [], shadowed: [], errors: [], functions: [] });
    }
    api.data.api = apiBase;
  }

  // 23. track sections: the list, the edits that reach the sidecar, and the
  // bands on the time axis.
  if (hasDistance && !((api.sectionsState() || {}).config)) {
    // 有距离轴但一个圈都没有（CAN 日志：有车速、没有 GPS / 信标）——没有可切的
    // 区段，所以下面那些断言跳过；但"没有圈"这件事本身要成立。
    check(!((api.data.laps || []).length),
      "有距离轴却没有参考圈，圈表却不是空的");
  }
  if (hasDistance && ((api.sectionsState() || {}).config)) {
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
  // 报表是按圈 / 区段算的，所以要的前提是**有圈**（hasLaps），不是"有距离轴"：
  // CAN 场次跑起来之后有距离轴但仍没有圈（没有 GPS、没人放信标）。
  const embedded = api.data.report;
  if (hasLaps) {
    check(!!embedded && !!embedded.time,
      "快照载荷里没有报表：导出快照时必须带上 time / channels_lap / channels_section");
  } else {
    // 没圈时不许给一张空表（空表看起来像"算出来就是零"），要给下一条指令
    const empty = api.data.report || {};
    check(!!empty.error && empty.error.indexOf("信标") >= 0 && empty.time === null,
      "没有圈时报表既没有表也没有下一条指令：" + JSON.stringify(empty.error));
    check(empty.error.indexOf("报表") >= 0,
      "没有圈时的提示用的是区段模块的说法，用户在报表上会以为点错了地方："
      + empty.error);
  }
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
}                                 // 26 报表两块到此为止（也是 if (api) 的收尾）

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
      // 没有圈的场次（CAN：没有 GPS、没放信标）没有按圈分的窗口——
      // 要的是 hasLaps，不是 hasDistance（CAN 跑起来之后有距离轴但仍没有圈）。
      check(!hasLaps || !!lapKey,
        "快照里的直方图没有按圈算过的窗口（应该带上每条完整圈）");
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
      check(canMeta.frames > 0 && canMeta.ids > 0, "加不出两张散点组件");
    }

    // 两张直方图（不同分箱数）同屏：同样各拿自己那一份（#21 验收条目点名的另一对）。
    api.addComponentOfType("histogram");
    api.addComponentOfType("histogram");
    const hists = sheetState.components.filter((c) => c.type === "histogram").slice(-2);
    const [histA, histB] = hists;
    if (histA && histB) {
      const mkHist = (name, boxes) => ({
        channel: name, unit: "", count: boxes, embedded: false, window_label: "整场",
        range: [0, boxes], stats: {},
        bins: Array.from({ length: boxes }, (_, i) => ({ count: i + 1, x0: i, x1: i + 1 })),
      });
      const hDataA = mkHist("甲", 11);
      const hDataB = mkHist("乙", 33);
      for (const [comp, payload, name, bins] of [
        [histA, hDataA, "甲", 11], [histB, hDataB, "乙", 33],
      ]) {
        comp.config.channel = name;
        comp.config.bins = bins;
        comp.config.window = "all";
        const b = api.bundleOf(comp);
        b.data = payload;
        b.dataKey = api.componentSpec(comp).data.key(comp);
        b.cacheKey = "";
      }
      api.renderAll();
      check(api.bundleOf(histA).data === hDataA && api.bundleOf(histB).data === hDataB,
        "两张直方图共用了一个数据槽（互相顶掉的旧毛病）");
      check(String(api.bundleOf(histA).head.textContent).indexOf("11") >= 0
        && String(api.bundleOf(histA).head.textContent).indexOf("甲") >= 0,
        "第一张直方图画的不是自己那份分布: " + api.bundleOf(histA).head.textContent);
      check(String(api.bundleOf(histB).head.textContent).indexOf("33") >= 0
        && String(api.bundleOf(histB).head.textContent).indexOf("乙") >= 0,
        "第二张直方图画的不是自己那份分布: " + api.bundleOf(histB).head.textContent);
      const ids = [histA.id, histB.id];
      sheetState.components = sheetState.components.filter((c) => ids.indexOf(c.id) < 0);
      api.buildWorksheet();
      check(ids.every((id) => {
        const gone = { id: id };
        return api.bundleOf(gone) === undefined;
      }), "删掉直方图之后它的数据槽还留着（缓存残留）");
    } else {
      check(false, "加不出两张直方图组件");
    }
    api.data.api = savedApiForScatter;
  }


/* ------------------------------------- 导出数据面板（ticket #23/#24 的前端一半）
 * 这一组钉三件事：面板能把选择拼成服务端认的参数、预估文案照服务端的数写、
 * 快照模式下不给"点了没反应"。真下载由 tools/verify_clicks.py 在真 Edge 里走。
 */
const exportDlg = registry.get("exportDlg");
const exportGo = registry.get("exportGo");
check(!!registry.get("dataBtn"), "工具栏里没有「导出数据」按钮");
// 假 DOM 不解析 hidden 属性，所以"默认关着"按 markup 查（真浏览器里由 #24 的真点击验收）
check(html.indexOf('<div id="exportDlg" hidden>') >= 0, "导出面板默认应当是关着的");
if (api && exportDlg) {
  const savedApi = api.data.api;
  const fields = (cfg) => { api.applyExportConfig(cfg); };
  api.openExportDialog();
  check(exportDlg.hidden === false, "点「导出数据」没有打开面板");
  check(String(registry.get("exportPlan").innerHTML).indexOf("快照") >= 0,
    "快照模式下没告诉用户导出要 serve 模式: " + registry.get("exportPlan").innerHTML);
  check(exportGo.disabled === true, "快照模式下「导出」按钮应当是灰的");

  // 采样率下拉必须是文档里那一串（Auto + 8 档 + 自定义）
  const rateOptions = String(registry.get("exportRate").innerHTML);
  const wantRates = ["auto", "1", "5", "10", "20", "50", "100", "200", "500", "custom"];
  check(wantRates.every((r) => rateOptions.indexOf('value="' + r + '"') >= 0),
    "采样率下拉少了档位: " + rateOptions.replace(/\n/g, ""));

  // 服务端算的预估照原样落到面板上（行数 / 列数 / 体积 / 分表 / 警告 / 范围）
  // 形状就是契约 §5 那一份：只有 rows / columns / bytes / sheets / warnings
  api.applyExportPlan({
    rows: 46400, columns: 446, bytes: 186000000, sheets: 2,
    warnings: ["原始采样模式下，比主时间基慢的 3 条通道大部分行是空的"],
  });
  const planText = String(registry.get("exportPlan").innerHTML);
  check(planText.indexOf("46,400") >= 0, "预估里没写行数: " + planText);
  check(planText.indexOf("446") >= 0, "预估里没写列数: " + planText);
  check(planText.indexOf("MB") >= 0 || planText.indexOf("GB") >= 0, "预估里没写体积: " + planText);
  check(planText.indexOf("2 张表") >= 0, "预估里没写分几张表: " + planText);
  check(planText.indexOf("慢的 3 条通道") >= 0, "预估里的警告没显示: " + planText);
  check(String(registry.get("exportPlan").className).indexOf("warn") >= 0,
    "有警告时预估没有走 warn 样式");

  // 12.5s / 1200m 这类写法在这里就归一化，服务端只收到数字
  check(api.exportMoment("12.5s").value === "12.5" && api.exportMoment("12.5s").absolute === false,
    "12.5s 没有被当成相对秒");
  check(api.exportMoment("1200m").value === "1200", "1200m 没有被当成米数");
  if (hasDistance) {   // 没有距离轴的场次：下面这两个选项在面板里是禁用的
  check(api.exportMoment("2026-09-14 12:34:56.789").absolute === true,
    "绝对时间没有被认出来是绝对时间");
  check(api.exportMoment("12:35:10.123").absolute === true, "裸时钟没有被当成绝对时间");

  // 面板 -> 查询参数：这一串就是 export.parse_request 认得的那几个名字
  fields({ range: "time", from: "12.5s", to: "18", channels: "all", maths: true,
           rate: "10", custom: "", resample: "linear", meta: true,
           axis: "time", format: "csv", layout: "wide" });
  const timeUrl = api.exportURL(api.exportConfig(), false).href;
  ["axis=time", "from=12.5", "to=18", "channels=all", "maths=1", "rate=10",
   "resample=linear", "format=csv", "layout=wide", "metadata=1", "bundle=1",
  ].forEach((bit) => {
    check(timeUrl.indexOf(bit) >= 0, "时间段导出的参数里少了 " + bit + "：" + timeUrl);
  });
  check(timeUrl.indexOf("estimate=") < 0, "下载请求里不该带 estimate=");
  check(api.exportURL(api.exportConfig(), true).href.indexOf("estimate=1") >= 0,
    "预估请求没带 estimate=1（契约里这个开关叫 estimate）");

  // 距离段 + 日期时间 = 明确报错（不是静默当成 0）
  fields({ range: "distance", from: "2026-09-14 12:34:56", to: "1850m", channels: "all",
           maths: true, rate: "auto", custom: "", resample: "hold", meta: false,
           axis: "distance", format: "xlsx", layout: "wide" });
  const badDistance = api.exportURL(api.exportConfig(), false);
  check(!!badDistance.error && badDistance.error.indexOf("米") >= 0,
    "距离段填了日期时间却没有报错: " + JSON.stringify(badDistance));

  // Excel 一律宽表；自定义采样率才露出数字框
  check(registry.get("exportLayout").disabled === true, "选 Excel 时布局应当锁死成宽表");
  check(registry.get("exportLayout").value === "wide", "选 Excel 时布局没有回到宽表");
  fields({ range: "all", from: "", to: "", channels: "all", maths: true, rate: "custom",
           custom: "250", resample: "nearest", meta: false, axis: "time",
           format: "csv", layout: "long" });
  check(registry.get("exportRateCustom").hidden === false, "选了自定义却没露出数字框");
  const customUrl = api.exportURL(api.exportConfig(), false).href;
  check(customUrl.indexOf("rate=250") >= 0, "自定义采样率没有进参数: " + customUrl);
  check(customUrl.indexOf("layout=long") >= 0 && customUrl.indexOf("bundle=1") < 0,
    "长表 / 不要元数据的参数不对: " + customUrl);

  // 范围自己决定轴：距离段 + 主索引还停在「时间」时，仍然必须按**米**导出——
  // 否则 1200–1850 会被当成 1200–1850 **秒**一声不响地导出去（另一段数据）。
  fields({ range: "distance", from: "1200m", to: "1850m", channels: "all", maths: true,
           rate: "auto", custom: "", resample: "linear", meta: false, axis: "time",
           format: "csv", layout: "wide" });
  const distanceUrl = api.exportURL(api.exportConfig(), false).href;
  check(distanceUrl.indexOf("axis=distance") >= 0 && distanceUrl.indexOf("index=timestamp") < 0,
    "距离段没有把主索引钉在米上: " + distanceUrl);
  check(registry.get("exportAxis").value === "distance"
    && registry.get("exportAxis").disabled === true,
    "「指定距离段」时主索引下拉没有锁到距离");
  }

  // 镜像的那一半：时间段 + 主索引停在「距离」上，也必须按秒走
  fields({ range: "time", from: "1200", to: "1250", channels: "all", maths: true,
           rate: "auto", custom: "", resample: "linear", meta: false, axis: "distance",
           format: "csv", layout: "wide" });
  const timeAgain = api.exportURL(api.exportConfig(), false).href;
  check(timeAgain.indexOf("axis=time") >= 0 && timeAgain.indexOf("index=timestamp") < 0,
    "「指定时间段」没有按秒导出: " + timeAgain);

  // 绝对时间戳（ticket #27）：同一根时间轴，换一种写法（axis=time + index=timestamp）。
  // 静态 markup 在假 DOM 里没有 innerHTML，所以档位按页面源查。
  check(html.indexOf('<option value="time">') >= 0
    && html.indexOf('<option value="timestamp">') >= 0
    && html.indexOf('<option value="distance">') >= 0,
    "主索引下拉少了档位");
  fields({ range: "all", from: "", to: "", channels: "all", maths: true, rate: "auto",
           custom: "", resample: "linear", meta: false, axis: "timestamp",
           format: "csv", layout: "long" });
  const stampUrl = api.exportURL(api.exportConfig(), false).href;
  check(stampUrl.indexOf("axis=time") >= 0 && stampUrl.indexOf("index=timestamp") >= 0,
    "绝对时间戳没有拼成 axis=time&index=timestamp: " + stampUrl);
  api.applyExportPlan({ rows: 10, columns: 4, bytes: 900, sheets: 0, index: "timestamp" });
  check(String(registry.get("exportPlan").innerHTML).indexOf("timestamp") >= 0,
    "预估里没写主索引列: " + registry.get("exportPlan").innerHTML);

  // 勾选的通道：走 selected + names
  fields({ range: "all", from: "", to: "", channels: "selected", maths: false,
           rate: "auto", custom: "", resample: "linear", meta: false, axis: "time",
           format: "csv", layout: "wide" });
  const picked = api.selectedChannels();
  const selectedUrl = api.exportURL(api.exportConfig(), false).href;
  check(picked.length > 0 && selectedUrl.indexOf("channels=selected") >= 0
    && selectedUrl.indexOf("maths=0") >= 0,
    "「只导出勾选的通道」没有拼成 selected + names: " + selectedUrl);

  // 选中圈：取那一圈的起止秒
  fields({ range: "lap", from: "", to: "", channels: "all", maths: true, rate: "auto",
           custom: "", resample: "linear", meta: false, axis: "time", format: "csv",
           layout: "wide" });
  const lapPreset = api.exportPreset(api.exportConfig());
  if (hasLaps) {   // 没有圈就没得选：要有圈，不是"有距离轴"
    check(lapPreset && typeof lapPreset.from === "number" && lapPreset.to > lapPreset.from,
      "「当前选中圈」没有给出这一圈的起止: " + JSON.stringify(lapPreset));
  } else {
    check(!!lapPreset && !!lapPreset.error && lapPreset.error.indexOf("圈") >= 0,
      "没有圈的时候「当前选中圈」既没给区间也没给下一步: " + JSON.stringify(lapPreset));
  }

  // 光标 A–B：没放基准光标时要说清下一步，放了才给区间
  api.state.datumOn = false;
  const noCursor = api.exportPreset({ range: "cursor" });
  check(!!noCursor.error && noCursor.error.indexOf("基准光标") >= 0,
    "没放基准光标时没有给出下一步: " + JSON.stringify(noCursor));
  api.state.datumOn = true;
  api.state.datum = 12.5;
  api.state.cursor = 18.25;
  const ab = api.exportPreset({ range: "cursor" });
  check(ab && ab.from === 12.5 && ab.to === 18.25, "光标 A–B 区间不对: " + JSON.stringify(ab));

  // 记住上次的配置（下次打开面板还是这一套）
  fields({ range: "time", from: "3", to: "4", channels: "all", maths: true, rate: "20",
           custom: "", resample: "linear", meta: true, axis: "time", format: "csv",
           layout: "wide" });
  api.data.api = "/api";                       // 假装是 serve 模式，让预估真的发一次请求
  api.refreshExportPlan();
  const last = httpCalls[httpCalls.length - 1];
  check(!!last && last.url.indexOf("/api/session/") >= 0 && last.url.indexOf("/export?") >= 0
    && last.url.indexOf("estimate=1") >= 0,
    "预估请求没有打到 /api/session/<场次>/export?estimate=1: " + (last && last.url));
  check(api.exportSaved().range === "time" && api.exportSaved().rate === "20",
    "上次的导出配置没有记住: " + JSON.stringify(api.exportSaved()));
  api.closeExportDialog();
  check(exportDlg.hidden === true, "关掉面板之后它还开着");

  // 下载文件名：中文名走 filename*=UTF-8''，普通写法也认
  check(api.exportFilename("attachment; filename*=UTF-8''%E4%B8%AD.csv") === "中.csv",
    "没有解出带中文的 filename*");
  check(api.exportFilename('attachment; filename="plain.csv"') === "plain.csv",
    "没有解出普通 filename=");
  api.data.api = savedApi;
}

/* ----------------------------------------------- 工作表（ticket #30）
 * 顶上那排按钮现在来自仓库里的 worksheets/*.json（服务端读出来塞进 DATA.worksheets，
 * 快照内嵌一份）。这一组钉三件事：按钮与文件一一对应；pick 规则挑得到通道、挑不到
 * 就留空而不是乱指；坏文件与不认得的组件类型都要**说出来**，不能静默少画。
 * 另外把"布局记忆按场次"这条也钉住——那是同一票的另一半。 */
{
  const injected = api.data.worksheets || [];
  const buttons = registry.get("presetRow")._children;
  check(injected.length >= 5, "这份页面没带工作表文件（DATA.worksheets 是空的）");
  check(buttons.length === injected.length,
    "工作表按钮数与文件数对不上：" + buttons.length + " vs " + injected.length);
  check(buttons.map((b) => b.textContent).join("|") === injected.map((s) => s.name).join("|"),
    "按钮顺序与文件顺序不一致：" + buttons.map((b) => b.textContent).join("|"));
  check(registry.get("presetRow")._children.length === injected.length
    && injected.every((s) => s.components.length > 0),
    "每份工作表都得至少有一个组件");

  // 切过去要真的有那套组件，而且类型都认得出（认不出的类型在这里会被剔掉并报出来）
  const catalogue = api.worksheetCatalogue();
  check(catalogue.problems.length === 0,
    "仓库里那几份工作表不该有问题：" + catalogue.problems.join(" / "));
  let switched = 0;
  for (const sheet of catalogue.sheets) {
    api.applyPreset(sheet.name);
    if (api.state.preset === sheet.name && api.state.components.length === sheet.components.length) {
      switched += 1;
    }
  }
  check(switched === catalogue.sheets.length,
    "有几套工作表切过去没生效：" + switched + "/" + catalogue.sheets.length);

  // pick：按 hints 找到本场次真实存在的通道（不是照抄文件里的名字）
  const graphSheet = catalogue.sheets.filter((s) => s.components.some(
    (c) => c.type === "graph" && c.pick && c.pick.channels))[0];
  check(!!graphSheet, "没有哪套工作表的图是靠 pick 挑通道的");
  const graphComp = api.componentsOfWorksheet(graphSheet).filter((c) => c.type === "graph")[0];
  check((graphComp.config.channels || []).length > 0,
    "pick 没有给图挑到通道：" + JSON.stringify(graphComp.config.channels));
  check((graphComp.config.channels || []).every((n) => api.data.channels.some(
    (c) => c.name === n)),
    "pick 挑出了本场次没有的通道：" + JSON.stringify(graphComp.config.channels));

  // pick 的语义：取第几条、越界给空、兜底链、special
  const hints = api.data.channels.slice(0, 4).map((c) => c.name);
  const all = api.resolvePickValue({ patterns: hints, limit: 4 }, hints);
  check(Array.isArray(all) && all.length === 4, "limit 没有收满：" + JSON.stringify(all));
  check(api.resolvePickValue({ patterns: hints, index: 2 }, hints) === hints[2],
    "index 没有取到第 3 条");
  check(api.resolvePickValue({ patterns: hints, index: 9 }, hints) === null,
    "越界该给 null，不能乱指一条");
  check(api.resolvePickValue([{ patterns: hints, index: 9 }, { patterns: hints, index: 1 }], hints)
    === hints[1], "兜底链没有落到后面那条规则上");
  check(Array.isArray(api.resolvePickValue({ patterns: hints })), "没写 index 就该给一整排");
  check(Array.isArray(api.resolvePickValue({ special: "report", limit: 2 }, hints))
    || api.resolvePickValue({ special: "report", limit: 2 }, hints) === null,
    "special=report 这条规则没实现");

  // 文件里写了不认得的类型：那一个组件被跳过，别的照画，而且要说出来
  const savedSheets = api.data.worksheets;
  const savedProblems = api.data.worksheet_problems;
  api.data.worksheets = [{
    id: "probe", name: "探针", order: 1, hints: [],
    components: [{ type: "graph" }, { type: "没有这种显示形式" }],
  }];
  api.data.worksheet_problems = [];
  api.resetWorksheetCache();
  const probe = api.worksheetCatalogue();
  check(probe.sheets.length === 1 && probe.sheets[0].components.length === 1,
    "不认得的组件类型没有被跳过：" + JSON.stringify(probe.sheets));
  check(probe.problems.join(" ").indexOf("没有这种显示形式") >= 0
    && probe.problems.join(" ").indexOf("升级 i3pro") >= 0,
    "跳过了一个组件却没说出来：" + probe.problems.join(" / "));
  api.renderWorksheetNote();
  check(String(registry.get("sheetNote").textContent).indexOf("升级 i3pro") >= 0,
    "工作表那一栏没有把问题显示出来");

  // 服务端报的坏文件也要出现在同一行字里
  api.data.worksheets = savedSheets;
  api.data.worksheet_problems = [{ file: "坏的.json", error: "坏的.json 不是合法的 JSON（第 1 行）。" }];
  api.resetWorksheetCache();
  api.renderWorksheetNote();
  check(String(registry.get("sheetNote").textContent).indexOf("坏的.json") >= 0,
    "服务端报出来的坏文件没有显示给用户");

  // 一份文件都没有（旧快照 / 目录被删）：给兜底工作表，并说清为什么
  api.data.worksheets = [];
  api.data.worksheet_problems = [];
  api.resetWorksheetCache();
  const empty = api.worksheetCatalogue();
  check(empty.sheets.length === 1 && empty.sheets[0].name === "默认",
    "没有工作表文件时应当退到「默认」那套：" + JSON.stringify(empty.sheets.map((s) => s.name)));
  check(empty.problems.join(" ").indexOf("重新生成") >= 0,
    "没有工作表文件时没说下一步：" + empty.problems.join(" / "));
  check(api.componentsOfWorksheet(empty.sheets[0]).length > 0,
    "兜底工作表一个组件都没有，页面会是空白的");

  // 布局记忆：键里带场次名（以前是全场共用一格，换场次也带着上一场的布局走）
  api.data.worksheets = savedSheets;
  api.data.worksheet_problems = savedProblems;
  api.resetWorksheetCache();
  api.applyPreset(catalogue.sheets[catalogue.sheets.length - 1].name);
  api.saveLayout();
  const keys = Object.keys(ctx.storage._v);
  check(keys.some((k) => k.indexOf("i3pro.worksheet.v2") === 0
    && k.indexOf(api.data.session) >= 0),
    "布局记忆没有按场次分开存：" + keys.join(", "));
  const remembered = JSON.parse(ctx.storage.getItem(
    "i3pro.worksheet.v2:" + api.data.session));
  check(remembered.preset === catalogue.sheets[catalogue.sheets.length - 1].name,
    "记下来的不是刚切过去的那套：" + JSON.stringify(remembered.preset));

  // 收尾：切回第一套，别把页面停在最后一套上（后面那几条 DOM 断言看的是当前布局）。
  // 注意最后那行画布统计是**从打开页面起累加**的，这一组切了 7 套工作表，数字会比
  // 没有这一组时大——那不是回归，"搬前搬后逐字段一致"由单测里的夹具那条钉住。
  api.resetWorksheetCache();
  api.applyPreset(catalogue.sheets[0].name);
}

/* ------------------------------------ 工作表增删改与进出（ticket #33）
 * 快照模式没有服务端可写，所以这一组钉得住的是三件：
 *   ① 写回文件的形状对不对——pick 填出来的通道名**不许**写死进文件；
 *   ② 导出给队友的那份 JSON 是文件那一份（不带本地 id）；
 *   ③ 快照里点那一排按钮要说"改用 serve"，而且**一个请求都不许发**
 *      （没挂上处理器 / 发到一半失败都会被这一条抓住）。
 * "点得到、文件真的变了"归真浏览器那关（tools/verify_clicks.py）。 */
{
  const sheet = api.worksheetCatalogue().sheets[0];
  const comp = api.componentsOfWorksheet(sheet).filter((c) => c.type === "graph")[0];
  check((comp.picked || []).length > 0, "pick 填了通道却没记下填了哪些键（保存会把通道名写死）");
  const written = api.sheetComponent(comp);
  check(written.type === "graph" && typeof written.x === "number"
    && typeof written.h === "number",
    "写回文件的组件缺了类型 / 位置 / 尺寸：" + JSON.stringify(written));
  check(!(written.config && "channels" in written.config),
    "pick 挑出来的通道名被写回文件了（换场次就空图）：" + JSON.stringify(written.config));
  check(!!written.pick && !!written.pick.channels,
    "文件里那条 pick 没有被保留：" + JSON.stringify(written));
  // 用户自己设的键（不是 pick 填的那个）必须原样写回去
  const probe = { type: "gauge", x: 1, y: 2, w: 3, h: 4,
                  config: { subtype: "bar", channel: "Vx KF" }, picked: [] };
  const kept = api.sheetComponent(probe);
  check(kept.config && kept.config.subtype === "bar" && kept.config.channel === "Vx KF",
    "用户自己设的配置在保存时被丢掉了：" + JSON.stringify(kept));

  // 导出给队友的那份：是文件那一份（没有本地 id / picked），能被 import 回来
  const dumped = JSON.parse(JSON.stringify(api.sheetExportPayload(sheet)));
  check(dumped.schema === 1 && dumped.name === sheet.name
    && dumped.components.length === sheet.components.length,
    "导出的工作表不是文件那一份：" + JSON.stringify(dumped).slice(0, 120));
  check(!("id" in dumped), "导出的 JSON 里带上了本地 id（那不是文件格式的一部分）");

  // 当前用哪一份：按名字找得到；「自定义」（URL 带来的那一屏）没有文件
  api.state.preset = sheet.name;
  check((api.currentSheet() || {}).id === sheet.id,
    "currentSheet() 没认出正在用的那一套：" + JSON.stringify(api.currentSheet()));
  api.state.preset = "自定义";
  check(api.currentSheet() === null, "「自定义」排布不该有对应的文件");
  api.state.preset = sheet.name;

  // 快照模式：7 个按钮逐个点，每个都要说"serve"，而且谁都不许发请求
  const posts = () => ctx.httpCalls.filter(
    (c) => c.url.indexOf("/worksheets") >= 0).length;
  const before = posts();
  for (const id of ["wsSave", "wsSaveAs", "wsNew", "wsRename", "wsDelete", "wsImport"]) {
    const btn = registry.get(id);
    check(!!btn, "工作表那一排少了按钮 " + id);
    btn.click();
    const said = String(registry.get("toast").textContent);
    check(said.indexOf("serve") >= 0,
      "快照里点「" + btn.textContent + "」没说改用 serve：" + said);
  }
  check(posts() === before,
    "快照模式点工作表按钮居然发了请求（会给用户「改了」的错觉）");

  // 导出在快照里走的是"现场造一个文件"那条路：没有 Blob 的沙箱会说不让下载，
  // 但**不能崩**、也不能发请求。
  registry.get("wsExport").click();
  check(String(registry.get("toast").textContent).length > 0,
    "快照里点「导出」什么也没说（用户不知道发生了什么）");
  check(posts() === before, "快照里点「导出」发了请求");
}

/* ---------------------- 缺失通道的三态与出口（ticket #34） --------------------
 * 一票修 bug 的事：以前换场次之后，工作表里那条本场次没有的通道是**静默消失**的
 * （服务端跳过 + 前端 `.filter` 再滤一遍 + 零文案），用户看到的只有"图少了一条线"。
 *
 * 这一组钉四件事：
 *   ① 三态判定只有一处（`channelState`），而且三种名字各归各的状态；
 *   ② 缺的通道**列在通道列表里、灰着、带"本场次没有"**，而且**没被从配置里删掉**；
 *   ③ 组件标题与抬头写着"缺 N 条"；
 *   ④ 出口写明：导出请求带 `skip_missing=1`、面板上列了名字。
 * "真点得到、真灰着"归真浏览器那关（tools/verify_clicks.py）。 */
{
  const api = window.i3pro;
  const real = "Vx KF";
  const bogus = "本场次没有的通道（无烟煤）";
  check(api.channelState(real) === "present",
    "本场次存在的通道被判成了别的状态：" + api.channelState(real));
  check(api.channelState(bogus) === "missing",
    "本场次没有的通道没被判成 missing：" + api.channelState(bogus));

  // 三态里的"empty"（通道在、整段没有有效样本）在快照里没有天然的样本：
  // 临时把一条真实通道的 has_data 翻成 false，验的是**判定与文案分得开**。
  const probe = api.channels.get(real);
  const wasHasData = probe.has_data;
  probe.has_data = false;
  check(api.channelState(real) === "empty",
    "整段没有有效样本的通道被和「本场次没有」混成一种了：" + api.channelState(real));
  check(api.workbenchMissing().indexOf(real) < 0,
    "「整段没数据」的通道被算进了「本场次没有」那份名单");
  probe.has_data = wasHasData;
  check(api.channelState(real) === "present", "探针没还原回去");

  // 往当前那张图上挂一条本场次没有的通道——就是"换场次之后常见的状态"。
  const comp = api.state.components.filter((c) => c.type === "graph")[0];
  const before = comp.config.channels.slice();
  comp.config.channels.push(bogus);
  api.state.focusId = comp.id;
  api.renderAll();
  api.renderChannelList();
  api.renderMissingNotice();

  check(api.workbenchMissing().indexOf(bogus) >= 0,
    "工作表引用了本场次没有的通道，workbenchMissing() 却没认出来：" + api.workbenchMissing());
  check(api.selectedChannels().indexOf(bogus) >= 0,
    "缺的通道被**自动剔除**出配置了（换回原场次时勾选就没了）");
  check(api.componentTitle(comp).indexOf("缺 1 条") >= 0,
    "组件标题没写「缺 N 条」：" + api.componentTitle(comp));

  // 通道列表那一行：假 DOM 里 className 是普通属性、行内容是 _html，
  // "真的灰了、真的点得到"归真浏览器那关（tools/verify_clicks.py）。
  const list = registry.get("channelList");
  const rows = (list && list._children ? list._children : [])
    .filter((c) => String(c.className).indexOf("missing") >= 0);
  const row = rows.filter((r) => String(r._html).indexOf(bogus) >= 0)[0];
  check(!!row, "通道列表里没有那条灰掉的缺通道");
  if (row) {
    check(row._html.indexOf("本场次没有") >= 0,
      "灰是灰了，但没说清是哪一种「没有」：" + row._html);
    check(row._html.indexOf("checked") >= 0,
      "缺的通道在列表里没保持勾选（看起来像被剔除了）");
  }
  // 页面抬头那条（快照文案）：它是挂在 #fileInfo 里的一个 span，假 DOM 拿得到。
  const info = registry.get("fileInfo");
  const notice = (info && info._children ? info._children : [])
    .filter((c) => c.id === "missingNotice")[0];
  check(!!notice && String(notice.textContent).indexOf(bogus) >= 0,
    "页面抬头没写缺了哪几条：" + (notice ? notice.textContent : "(没有这个元素)"));

  // 出口：导出面板把这条通道当"跳过并写进元数据"，而不是当成会把整单打回的错。
  const cfg = api.exportConfig();
  cfg.channels = "selected";
  const gone = api.exportMissingChannels(cfg);
  check(gone.indexOf(bogus) >= 0, "导出面板没认出这次会跳过哪几条：" + gone);
  const url = api.exportURL(cfg);
  check(!url.error, "导出被整单打回了（缺通道不该让导出做不成）：" + url.error);
  if (url.href) {
    check(url.href.indexOf("skip_missing=1") >= 0,
      "导出请求没带 skip_missing=1，服务端会当成名字打错：" + url.href);
    check(decodeURIComponent(url.href).indexOf(bogus) >= 0,
      "缺的那条不在导出名单里（元数据就写不出 excluded_missing）");
  }

  // 还原：把探针那条撤掉，状态要跟着回到"一条都不缺"。
  comp.config.channels = before;
  api.renderAll();
  api.renderChannelList();
  api.renderMissingNotice();
  check(api.workbenchMissing().length === 0,
    "撤掉之后还有残留的缺通道：" + api.workbenchMissing());
  check((registry.get("channelList")._children || [])
          .filter((c) => String(c.className).indexOf("missing") >= 0).length === 0,
    "通道列表里还留着灰掉的缺通道");
  check(String(notice.textContent) === "",
    "撤掉之后页面抬头还写着缺通道：" + notice.textContent);
}

/* ------------------- 配色与抬头（ticket #35） -------------------------------
 * 判据分两半：**可辨性是服务端算的**（CIE76 色差 / WCAG 对比度，见
 * tests/test_i3pro.py 的 TestPalettes），这里钉的是"界面真的用了它"：
 * 三套调色板都在、换调色板真的换色、手选色优先、手选色会被写回工作表、
 * 抬头字段按组件藏得住。"点得到菜单、选得中颜色"归真浏览器那关。 */
{
  const api = window.i3pro;
  const dom = ctx.document;
  const keys = Object.keys(api.palettes || {});
  check(keys.length === 3, "调色板不是三套：" + keys.join(","));
  let shortPalette = "";
  for (const key of keys) {
    const entry = api.palettes[key] || {};
    if ((entry.colors || []).length < 8 || !entry.label) shortPalette = key;
  }
  check(!shortPalette, "有一套调色板没有八色的余量或没有名字：" + shortPalette);
  check(keys.indexOf("default") === 0, "第一套不是 default（老工作表要退回它）");
  check(!keys.some((k) => (api.palettes[k].colors || [])
    .some((c) => String(c).toLowerCase() === String(api.missingColor).toLowerCase())),
    "「本场次没有」的灰混进了调色板（它是语义色，不是第 N 条通道）");

  const comp = api.state.components.filter((c) => c.type === "graph")[0];
  const name = (comp.config.channels || [])[0];
  check(!!name, "这张图没有通道，配色用例没法验");
  const auto = api.channelColor(name, comp);
  check(api.channelColorIsManual(name, comp) === false,
    "这条通道一上来就被当成手选色了");
  check(api.palettes.default.colors.indexOf(auto) >= 0,
    "默认调色板下这条通道的颜色不在默认那一套里：" + auto);

  // 换整套调色板：颜色跟着换，而且是那一套里的颜色。
  api.setComponentPalette(comp, "colorblind");
  check(comp.config.palette === "colorblind", "换调色板没写进组件配置");
  const swapped = api.channelColor(name, comp);
  check(api.palettes.colorblind.colors.indexOf(swapped) >= 0,
    "换到色盲友好之后颜色不是那一套的：" + swapped);
  check(swapped !== auto, "换了一整套调色板颜色却一点没变：" + swapped);

  // 手选色优先，而且它必须**跟着工作表写回文件**。
  api.setChannelColor(comp, name, "#ff00aa");
  check(api.channelColorIsManual(name, comp), "手选色没被记下来");
  check(api.channelColor(name, comp) === "#ff00aa", "手选色没生效");
  api.setComponentPalette(comp, "contrast");
  check(api.channelColor(name, comp) === "#ff00aa",
    "换了调色板把手选色冲掉了（手选的意义就是不跟着调色板走）");
  const written = JSON.parse(JSON.stringify(api.sheetComponent(comp)));
  check((written.config || {}).colors
    && written.config.colors[name] === "#ff00aa",
    "手选色没有写进工作表的组件配置：" + JSON.stringify(written.config));
  check((written.config || {}).palette === "contrast",
    "调色板选择没有写进工作表：" + JSON.stringify(written.config));

  // 抬头字段按组件开关，而且写进配置。
  api.setHeadField(comp, "measure", false);
  check(api.headFields(comp).measure === false, "关掉 Min/Max/Avg 没生效");
  check(((JSON.parse(JSON.stringify(api.sheetComponent(comp)))).config || {})
    .show.measure === false, "抬头开关没有写进工作表");
  check(api.headFields(comp).cursor === true, "只关了一项，别的项跟着被关了");
  api.renderAll();
  const nodes = (registry.get("worksheet")._children || []);
  const node = nodes.filter((c) => c.dataset && String(c.dataset.id) === String(comp.id))[0];
  const body = (node && node._children ? node._children : [])
    .filter((c) => String(c.className).indexOf("compbody") >= 0)[0];
  const head = (body && body._children ? body._children : [])
    .filter((c) => String(c.className).indexOf("graphhead") >= 0)[0];
  const legend = (head && head._children ? head._children : [])
    .filter((c) => String(c.className).indexOf("glegend") >= 0)[0];
  const row = (legend && legend._children ? legend._children : [])
    .filter((c) => String(c.className).indexOf("lrow") >= 0)[0];
  const spans = (row && row._children ? row._children : [])
    .filter((c) => String(c.className).indexOf("lmm") >= 0);
  check(spans.length === 3 && spans.every((s) => s.style.display === "none"),
    "关掉 Min/Max/Avg 之后抬头里那三格还露着：" + spans.length);
  const curs = (row && row._children ? row._children : [])
    .filter((c) => String(c.className).indexOf("lcur") >= 0);
  check(curs.length === 1 && curs[0].style.display !== "none",
    "只关了测量，光标值也跟着没了");

  // 色块点出来的那个菜单：真的有可选的颜色，点了真的换。
  const anchor = (row && row._children ? row._children : [])
    .filter((c) => String(c.className).indexOf("swatch") >= 0)[0];
  check(!!anchor, "抬头里没有色块（点不了颜色菜单）");
  if (anchor) {
    api.openColorMenu(comp, name, anchor);
    const menu = (dom.body._children || [])
      .filter((c) => String(c.className).indexOf("popmenu") >= 0)[0];
    check(!!menu, "点色块没有弹出颜色菜单");
    if (menu) {
      const shelf = (menu._children || [])
        .filter((c) => String(c.className).indexOf("popshelf") >= 0)[0];
      const spots = (shelf && shelf._children ? shelf._children : [])
        .filter((c) => String(c.className).indexOf("popswatch") >= 0);
      check(spots.length >= 8, "颜色菜单里的可选颜色太少：" + spots.length);
      const target = spots[spots.length - 1];
      const wanted = String(target.style.background);
      target.click();
      check(api.channelColor(name, comp) === wanted,
        "点了菜单里的颜色却没换：" + api.channelColor(name, comp) + " != " + wanted);
      api.closePopMenu();
      check(dom.body._children.filter(
        (c) => String(c.className).indexOf("popmenu") >= 0).length === 0,
        "菜单点了之后没关掉");
    }
  }

  // 还原：把这一件组件恢复成没动过的样子。
  delete comp.config.colors;
  delete comp.config.palette;
  delete comp.config.show;
  api.renderAll();
  check(api.headFields(comp).measure === true, "还原之后抬头开关没回到默认");
}

/* -------------------- 通道别名（ticket #36） --------------------------------
 * 规矩只有一条：**有序候选，取第一条在本场次存在的**。它实现一次（Python 的
 * `aliases.landing`），随页面载荷贴成一张落点表；前端只**查表**，不重写规则。
 * 这一组钉的是"前端确实只查表"：
 *   ① `@引用` 落到了就替换成真通道名、落不到就留着（ticket #34 判它 missing）；
 *   ② 保存时没动过的槽写回**引用**（写回真名的话，换场次又空了）；
 *   ③ 编辑（增删候选 / 调顺序 / 增删别名）只改工作副本，保存才落盘。
 * "真点得到、真存进文件"归真浏览器那关（tools/verify_clicks.py）。 */
{
  const api = window.i3pro;
  const comp = api.state.components.filter((c) => c.type === "graph")[0];
  const real = "Vx KF";
  const landing = { "@车速": real, "@落不到的": null };
  const before = comp.config.channels.slice();
  // 仓库里那七份工作表的图是 `pick` 规则挑出来的（`picked` 里记着"别写回文件"），
  // 别名那一路是**文件里写死的引用**——这里把 picked 清掉才是在验别名那条路。
  const pickedBefore = comp.picked;
  comp.picked = [];
  comp.config.channels = ["@车速", "@落不到的", before[0]];
  api.applyAliases(comp, landing);
  check(comp.config.channels[0] === real,
    "落到的引用没被替换成真通道名：" + comp.config.channels[0]);
  check(comp.config.channels[1] === "@落不到的",
    "落不到的引用被丢掉了（应该留着，交给 ticket #34 判缺失）：" + comp.config.channels[1]);
  check(comp.config.channels[2] === before[0], "普通通道名被别名替换动到了");
  check(comp.aliasSource && comp.aliasSource.channels
    && comp.aliasSource.channels[0] === "@车速",
    "没记下这一格原来是引用：" + JSON.stringify(comp.aliasSource));

  const written = JSON.parse(JSON.stringify(api.sheetComponent(comp)));
  check(written.config.channels[0] === "@车速",
    "保存时把别名写成了真通道名（换场次又空了）：" + written.config.channels[0]);
  check(written.config.channels[2] === before[0], "普通通道名保存时被写坏了");

  // 用户真改过这一格：那就按他写的存（不再写回引用）。
  comp.config.channels = [real, before[0]];
  const edited = JSON.parse(JSON.stringify(api.sheetComponent(comp)));
  check(edited.config.channels[0] === real,
    "用户改过的通道槽还是被写回了引用：" + edited.config.channels[0]);

  // 三态：落到的算 present，落不到的算 missing——与"缺通道"是同一条缝。
  api.state.aliasLanding = landing;
  check(api.channelState("@车速") === "present", "落到的别名没被判成 present");
  check(api.channelState("@落不到的") === "missing",
    "落不到的别名没被判成 missing：" + api.channelState("@落不到的"));
  check(api.isAliasReference("@车速") && !api.isAliasReference("车速"),
    "别名引用的写法认错了");
  api.state.aliasLanding = {};

  // 编辑：增删候选、调顺序、增删别名——都只改工作副本。
  const aliasesBefore = JSON.parse(JSON.stringify(api.state.aliases || []));
  api.aliasAddAlias("测试别名");
  api.aliasAddCandidate("测试别名", "GPS Speed");
  api.aliasAddCandidate("测试别名", real);
  const made = (api.state.aliases || []).filter((one) => one.name === "测试别名")[0];
  check(!!made && made.candidates.length === 2,
    "加别名 / 加候选没落到工作副本上：" + JSON.stringify(made));
  api.aliasMoveCandidate("测试别名", 1, -1);
  check(made.candidates[0] === real, "候选往前挪一位没生效：" + made.candidates);
  api.aliasRemoveCandidate("测试别名", 0);
  check(made.candidates.length === 1 && made.candidates[0] === "GPS Speed",
    "删候选没生效：" + JSON.stringify(made.candidates));
  check(api.state.aliasesDirty === true, "改过之后没有标成「待保存」");
  api.renderAliasList();
  const list = registry.get("aliasList");
  const rows = (list && list._children) || [];
  check(rows.length === 1 && String(rows[0]._children[0]._html).indexOf("测试别名") >= 0
    || rows.length === 1, "别名列表没画出这一条：" + rows.length);
  api.aliasRemoveAlias("测试别名");
  check(!(api.state.aliases || []).some((one) => one.name === "测试别名"),
    "删别名没生效");

  // 一条都没落地的别名也要说清（i2 Pro 的 Channel Status 存在的理由）：
  // 刚建好只有名字、还没加候选时，落点是 undefined → 界面写"待保存"，不是空白。
  api.aliasAddAlias("还没候选");
  api.state.aliasLanding = { "@还没候选": null };
  api.state.aliasesDirty = false;
  api.renderAliasList();
  // 假 DOM 里文字挂在子节点上（行本身是 makeEl 造的空壳），所以递归收一遍。
  const textOf = (el) => {
    let out = String(el.textContent || "") + String(el._html || "");
    for (const kid of el._children || []) out += " " + textOf(kid);
    return out;
  };
  const barren = (registry.get("aliasList")._children || [])
    .filter((one) => textOf(one).indexOf("还没候选") >= 0)[0];
  const barrenText = barren ? textOf(barren) : "";
  check(barrenText.indexOf("本场次一条都没落地") >= 0,
    "一条都没落地的别名没写明原因：" + barrenText);
  api.aliasRemoveAlias("还没候选");
  api.state.aliasLanding = {};

  api.state.aliases = aliasesBefore;
  api.state.aliasesDirty = false;
  api.renderAliasList();

  // 交叉引用：坏输入要被挡住，而且说清下一步。
  api.aliasAddAlias("@带圈的");
  check(!(api.state.aliases || []).some((one) => one.name === "@带圈的"),
    "名字带 @ 的别名被收下了");
  api.aliasAddAlias("空的候选");
  api.aliasAddCandidate("空的候选", "  ");
  const empty = (api.state.aliases || []).filter((one) => one.name === "空的候选")[0];
  check(empty && empty.candidates.length === 0, "空候选被收下了");
  api.aliasRemoveAlias("空的候选");
  api.state.aliases = aliasesBefore;
  api.state.aliasesDirty = false;
  api.renderAliasList();

  // 还原：组件恢复成没动过的样子。
  comp.config.channels = before;
  comp.picked = pickedBefore;
  delete comp.aliasSource;
  delete comp.aliasResolved;
  api.renderAll();
}

/* --------------------------------------------------------------- DOM checks */
/* ------------------------- 34. 原始 CAN 帧表（#38 / #39 / #40） --------------
 * 断言导入报告里该有的东西：帧数 / 覆盖率、**每份 DBC 的贡献与哈希**、
 * **每条通道来自哪份 DBC**、读不懂的 ID 那张表、以及"有没有距离轴"的说法。
 * 距离轴是**算出来**的：跑起来的日志里有 Vx_KF 就有距离轴，只有几十秒的原地
 * 日志没有——两种情况都要认（ticket #40）。
 */
const canMeta = (api && api.data && api.data.meta && api.data.meta.can) || null;
if (canMeta) {
  check(canMeta.frames > 0 && canMeta.ids > 0,
    "CAN 导入报告里没有帧数 / ID 数：" + JSON.stringify(canMeta.frames));
  check(canMeta.covered_frames > 0 && canMeta.coverage > 0 && canMeta.coverage < 1,
    "覆盖率不合常理：" + canMeta.coverage);
  check((canMeta.undecoded || []).length > 0, "报告里没有列出读不懂的 ID");
  check((canMeta.undecoded || []).every((row) => row.id && row.frames > 0
    && typeof row.rate === "number" && row.sample),
    "未定义 ID 的行缺 ID / 帧数 / 帧率 / 样例字节");
  check((canMeta.channels || []).length > 0, "报告里没有解码出来的通道");
  check((canMeta.channels || []).every((row) => row.name && row.message
    && row.update_rate > 0 && row.dbc),
    "通道行没有名字 / 报文名 / 真实更新率 / 来自哪份 DBC");

  // 概览条（页面顶上那条全程波形）画的是"速度"。CAN 场次的速度叫 ``Vx_KF``，
  // 而候选表里写的是 ``Vx KF``（空格）——只按原样找就会一条都找不到，
  // 于是**跑起来的 CAN 场次连概览条都没有**（ticket #40 实测：serve 模式
  // /overview 返回 null；同一份数据导成快照却有，因为那边退到了第一条通道）。
  // 有距离轴就等于"有一条真的在动的速度"，那就必须有概览条。
  if (hasDistance) {
    check(!!api.state.overview && (api.state.overview.time || []).length > 0,
      "CAN 场次有速度却没有概览条（速度候选只认带空格的 Vx KF）");
  }
  // 而且画的必须**是速度那条**，不能是"找不到速度就退到第一条通道"的兜底：
  // 兜底也会给出非空概览条，所以只看"有没有"抓不到这个 bug（变异测试实测过）。
  // 比较的是去掉空格/下划线/大小写之后的名字，也就是
  // `src/i3pro/render.py` 里 SPEED_FOR_COLORING 认名字的那条规则；下面这份
  // 候选表是它的镜像（改名要两处一起改——所以只镜像名字，不镜像顺序）。
  if (api.state.overview) {
    const squash = (text) => String(text).toLowerCase().replace(/[^a-z0-9]/g, "");
    const SPEEDY = ["vxkf", "groundspeed", "gpsspeed", "speedfr", "speedfl"];
    check(SPEEDY.indexOf(squash(api.state.overview.name)) >= 0,
      "概览条画的不是速度通道，而是兜底的第一条通道："
      + api.state.overview.name);
  }

  // 多份 DBC 取并集：每份的贡献、哈希、归属都要能复核
  const dbcFiles = ((canMeta.dbc || {}).files) || [];
  check(dbcFiles.length >= 2,
    "报告里没有列出每份 DBC 的贡献（ticket #40）：" + dbcFiles.length);
  check(dbcFiles.every((row) => row.file && row.sha256 && row.covered_frames >= 0
    && row.channels >= 0),
    "DBC 贡献行缺文件名 / 哈希 / 覆盖帧数 / 贡献通道数");
  const dbcOwners = new Set(dbcFiles.map((row) => row.file));
  check((canMeta.channels || []).every((row) => dbcOwners.has(row.dbc)),
    "有通道写着来自一份不在贡献表里的 DBC");
  const bestSingle = Math.max.apply(null, dbcFiles.map((row) => row.channels || 0));
  check((canMeta.channels || []).length > bestSingle,
    "通道数没有超过任何单份 DBC（" + (canMeta.channels || []).length + " vs 最高 "
    + bestSingle + "）——那就是没取并集");
  check((canMeta.dbc || {}).method === "union" || (canMeta.dbc || {}).method === "file",
    "报告没说清是并集还是固定一份：" + JSON.stringify((canMeta.dbc || {}).method));
  const canNotes = canMeta.notes || [];
  check(canNotes.some((text) => text.indexOf("DBC") >= 0),
    "报告没有说明用了几份 DBC：" + JSON.stringify(canNotes));
  if (hasDistance) {
    check(canNotes.some((text) => text.indexOf("距离轴来源") >= 0),
      "有距离轴却没写来源：" + JSON.stringify(canNotes));
  } else {
    check(canNotes.some((text) => text.indexOf("距离轴") >= 0),
      "没有说明这批日志没有距离轴：" + JSON.stringify(canNotes));
    check(String(registry.get("statusLine").innerHTML).indexOf("距离轴") >= 0,
      "状态行没有说距离轴不可用");
  }

  // 抬头那个入口点得开，表里的行数与报告里的条数**一致**（同一处实现，不许各算一遍）
  const canLink = registry.get("canLink");
  check(!!canLink, "抬头里没有 CAN 导入报告的入口");
  if (canLink) {
    canLink.dispatch("click", { preventDefault() {} });
    check(registry.get("canDlg").hidden === false, "点了入口窗口没开");
    const table = String(registry.get("canTableWrap")._html);
    check(table.indexOf("来自哪份 DBC") >= 0,
      "报告里没有「每条通道来自哪份 DBC」那张表");
    check((canMeta.channels || []).every((row) => table.indexOf(row.name) >= 0),
      "有通道没出现在报告表里");
    // 两张表：通道表在前、读不懂的 ID 表在后；行数只数最后那张
    const lastTable = table.split("<table>").pop();
    const rowCount = (lastTable.match(/<tr/g) || []).length - 1;   // 去掉表头那一行
    check(rowCount === (canMeta.undecoded || []).length,
      "表里的行数与报告不一致：" + rowCount + " vs " + (canMeta.undecoded || []).length);
    check(table.indexOf(canMeta.undecoded[0].id) >= 0
      && table.indexOf(canMeta.undecoded[0].sample.replace(/ /g, " ")) >= 0,
      "表里没有第一个未定义 ID 的 ID / 样例字节");
    const diag = (canMeta.undecoded || []).filter((row) => row.diagnostic);
    check(!diag.length || table.indexOf("诊断流量") >= 0,
      "诊断流量没有被单独标出来");
    registry.get("canClose").dispatch("click", {});
    check(registry.get("canDlg").hidden === true, "关闭按钮没关上窗口");
  }
}

const header = registry.get("fileInfo");
check(header && (header.innerHTML.indexOf(".ld") >= 0
  || header.innerHTML.indexOf(".csv") >= 0
  || header.innerHTML.indexOf(".xlsx") >= 0
  || header.innerHTML.indexOf(".txt") >= 0
  || header.innerHTML.indexOf(".tsv") >= 0), "header was not populated");
// 分隔文本（.txt / 分隔符不是逗号的 .csv）要把"怎么读的"画在抬头那一行上
// （ticket #32）。跑 .ld 快照时这一段没有 parse_note，跳过——它由验收里那份
// 文本快照负责跑。
const parseNote = (api && api.data && api.data.meta && api.data.meta.parse_note) || "";
if (parseNote) {
  check(header.innerHTML.indexOf(parseNote) >= 0,
    "读法没有写进抬头：" + parseNote);
}
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
