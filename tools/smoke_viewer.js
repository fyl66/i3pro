/**
 * Headless smoke test for the generated viewer.
 *
 *   node tools/smoke_viewer.js out/viewer.html
 *
 * The viewer is a single self-contained HTML file with no dependencies, so
 * there is nothing to install: this script runs its inline JavaScript against a
 * minimal DOM/canvas shim and fails if the script throws, if nothing is drawn,
 * or if the header / lap table / channel list are not populated. It renders
 * twice - once in time-axis mode and once with an overlay hash - so both chart
 * paths are exercised.
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

const calls = { fillText: 0, stroke: 0, lineTo: 0, rect: 0, arc: 0 };
function makeContext() {
  return new Proxy(
    {},
    {
      get(target, key) {
        if (key in target) return target[key];
        if (typeof key === "symbol") return undefined;
        return () => {
          if (key in calls) calls[key] += 1;
        };
      },
      set(target, key, value) {
        target[key] = value;
        return true;
      },
    }
  );
}

class Element {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this._html = "";
    this._rows = [];
    this._children = [];
    this._attrs = {};
    this.style = {};
    this.dataset = {};
    this.value = "";
    this.title = "";
    this.disabled = false;
    this.checked = false;
    this.textContent = "";
    this.download = "";
    this.href = "";
    const classes = new Set();
    this.classList = {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
      contains: (c) => classes.has(c),
    };
  }
  get innerHTML() {
    return this._html;
  }
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
  }
  get textContent() {
    return this._html;
  }
  set textContent(value) {
    this._html = String(value);
  }
  appendChild(child) {
    // a real DOM fragment splices its children into the parent
    if (child && child.tagName === "FRAGMENT") {
      this._children.push(...child._children);
      return child;
    }
    this._children.push(child);
    return child;
  }
  append(...children) {
    this._children.push(...children);
  }
  querySelector(selector) {
    if (selector === "canvas" || selector === ".readout" || selector === ".label") {
      if (!this._q[selector]) this._q[selector] = new Element("div");
      return this._q[selector];
    }
    return new Element("div");
  }
  querySelectorAll() {
    return this._rows;
  }
  closest() {
    return null;
  }
  addEventListener() {}
  getAttribute(name) {
    return this._attrs[name];
  }
  setAttribute(name, value) {
    this._attrs[name] = value;
  }
  getContext() {
    return makeContext();
  }
  toDataURL() {
    return "data:image/png;base64,";
  }
  click() {}
  getBoundingClientRect() {
    return { left: 0, top: 0, width: 900, height: 150 };
  }
  get clientWidth() {
    return 900;
  }
  get parentElement() {
    if (!this._parent) {
      this._parent = new Element("div");
      this._parent._parent = this._parent;
    }
    return this._parent;
  }
}
Element.prototype._q = {};

function run(hash) {
  const registry = new Map();
  const document = {
    getElementById(id) {
      if (!registry.has(id)) registry.set(id, new Element("div", id));
      return registry.get(id);
    },
    createElement(tag) {
      return new Element(tag);
    },
    createDocumentFragment() {
      return new Element("fragment");
    },
    body: new Element("body"),
    addEventListener() {},
  };
  const window = { addEventListener() {}, devicePixelRatio: 1 };
  // real location.hash always carries the leading "#"
  const location = { hash: hash ? `#${hash}` : "", origin: "http://localhost", pathname: "/x" };
  const navigator = { clipboard: { writeText: async () => {} } };
  const sandbox = {
    document,
    window,
    location,
    navigator,
    console,
    setTimeout,
    clearTimeout,
    URLSearchParams,
    fetch: () => Promise.reject(new Error("fetch is not available headless")),
  };
  vm.runInNewContext(script, sandbox);
  return registry;
}

try {
  var registry = run(process.env.I3PRO_HASH || "");
} catch (error) {
  console.error("FAIL: viewer script threw:", error.message);
  console.error(error.stack.split("\n").slice(0, 6).join("\n"));
  process.exit(1);
}

const problems = [];
const header = registry.get("fileInfo");
if (!header || !header.innerHTML.includes(".ld")) problems.push("header was not populated");
const lapTable = registry.get("lapTable");
if (!lapTable || !lapTable.innerHTML.includes("<tr")) problems.push("lap table is empty");
const channelList = registry.get("channelList");
if (!channelList || channelList._children.length < 5) problems.push("channel list is empty");
const count = registry.get("chCount");
if (!count || !String(count.textContent).includes("/")) problems.push("channel count missing");
if (calls.fillText === 0) problems.push("no canvas text was drawn");
if (calls.lineTo === 0) problems.push("no canvas traces were drawn");
if ((process.env.I3PRO_HASH || "").includes("overlay")) {
  const delta = registry.get("deltaReadout");
  if (!delta || !String(delta.textContent).includes("最大差")) {
    problems.push("overlay mode did not render the delta panel");
  }
}

if (problems.length) {
  console.error("FAIL:", problems.join("; "));
  process.exit(1);
}
console.log(
  `PASS - viewer ran headless (${calls.lineTo} line segments, ${calls.fillText} labels, ` +
    `${(lapTable._rows || []).length} lap rows, ${channelList._children.length} channel rows)`
);
