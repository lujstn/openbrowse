import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "../../app/dashboard/static/agents.js"), "utf8");

function makeEl() {
  const el = {
    className: "",
    innerHTML: "",
    scrollTop: 0,
    scrollHeight: 0,
    style: {},
    children: [],
    _attrs: {},
    _classes: new Set(),
    classList: {
      add: (...c) => c.forEach((x) => el._classes.add(x)),
      remove: (...c) => c.forEach((x) => el._classes.delete(x)),
      contains: (c) => el._classes.has(c),
    },
    appendChild: (c) => el.children.push(c),
    removeChild: () => {},
    setAttribute: (k, v) => (el._attrs[k] = v),
    getAttribute: (k) => el._attrs[k],
    addEventListener: () => {},
    removeEventListener: () => {},
    querySelector: (sel) => (el._q[sel] = el._q[sel] || makeEl()),
    getBoundingClientRect: () => ({ height: 0, width: 0 }),
  };
  el._q = {};
  return el;
}

let clock = 1000;
let nextFrameId = 1;
const frames = new Map();

const PRELUDE =
  "var window = this; var document = this.document; var performance = this.performance;" +
  "var navigator = this.navigator; var setTimeout = this.setTimeout;" +
  "var requestAnimationFrame = this.requestAnimationFrame;" +
  "var cancelAnimationFrame = this.cancelAnimationFrame;" +
  "var clearTimeout = this.clearTimeout;\n";

function loadAgents(reducedMotion) {
  const win = {
    matchMedia: () => ({ matches: reducedMotion }),
    requestAnimationFrame: (cb) => {
      const id = nextFrameId++;
      frames.set(id, cb);
      return id;
    },
    cancelAnimationFrame: (id) => frames.delete(id),
    setTimeout: () => 0,
    clearTimeout: () => {},
    performance: { now: () => clock },
    navigator: {},
    document: { createElement: () => makeEl(), body: makeEl() },
  };
  win.window = win;
  const ctx = vm.createContext(win);
  vm.runInContext(PRELUDE + source, ctx);
  return ctx.OpenBrowseAgents;
}

function flush(steps = 40, dt = 16) {
  for (let i = 0; i < steps; i++) {
    const queued = [...frames.entries()];
    if (!queued.length) return;
    frames.clear();
    clock += dt;
    queued.forEach(([, cb]) => cb(clock));
  }
}

const results = [];
const check = (name, cond, detail) => results.push([name, !!cond, detail]);

const container = makeEl();
const sr = new (loadAgents(false).StreamingResponse)(container);
const hasCaret = () => sr.textEl.innerHTML.includes("ob-caret");

sr.update({ stream: "the model is thinking about", spin: true });
flush();
check("caret shows while streaming", hasCaret(), sr.textEl.innerHTML);

sr.update({ stream: "the model is thinking about the page it just loaded", spin: false });
flush();
check("caret removed once settled", !hasCaret(), sr.textEl.innerHTML);
check("settled text is complete", sr.textEl.innerHTML.includes("just loaded"), sr.textEl.innerHTML);
check("settled class applied", container._classes.has("is-settled"), "");

sr.update({ stream: "next thought", spin: true });
flush(1);
check("caret returns on the next stream", hasCaret(), sr.textEl.innerHTML);

const rmSr = new (loadAgents(true).StreamingResponse)(makeEl());
rmSr.update({ stream: "reduced motion thought", spin: false });
check(
  "reduced motion settles without a caret",
  !rmSr.textEl.innerHTML.includes("ob-caret"),
  rmSr.textEl.innerHTML
);

let failed = 0;
for (const [name, ok, detail] of results) {
  if (!ok) failed++;
  console.log(`${ok ? "ok" : "not ok"} - ${name}${ok ? "" : ` :: ${detail}`}`);
}
process.exit(failed ? 1 : 0);
