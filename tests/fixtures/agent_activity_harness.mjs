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
    style: { setProperty: () => {} },
    children: [],
    _attrs: {},
    _classes: new Set(),
    classList: {
      add: (...c) => c.forEach((x) => el._classes.add(x)),
      remove: (...c) => c.forEach((x) => el._classes.delete(x)),
      contains: (c) => el._classes.has(c),
      toggle: (c, force) => (force ? el._classes.add(c) : el._classes.delete(c)),
    },
    appendChild: (c) => el.children.push(c),
    removeChild: () => {},
    setAttribute: (k, v) => (el._attrs[k] = v),
    getAttribute: (k) => el._attrs[k],
    removeAttribute: (k) => delete el._attrs[k],
    addEventListener: (t, fn) => (el._on[t] = el._on[t] || []).push(fn),
    removeEventListener: () => {},
    fire: (t) => (el._on[t] || []).forEach((fn) => fn({})),
    querySelector: (sel) => (el._q[sel] = el._q[sel] || makeEl()),
    getBoundingClientRect: () => ({ height: 0, width: 0 }),
  };
  el._q = {};
  el._on = {};
  return el;
}

let clock = 1000;
const NOW = new Date(Date.now() - 4200).toISOString();
let nextFrameId = 1;
const frames = new Map();

const PRELUDE =
  "var window = this; var document = this.document; var performance = this.performance;" +
  "var navigator = this.navigator; var setTimeout = this.setTimeout;" +
  "var requestAnimationFrame = this.requestAnimationFrame;" +
  "var cancelAnimationFrame = this.cancelAnimationFrame;" +
  "var clearTimeout = this.clearTimeout;" +
  "var setInterval = this.setInterval; var clearInterval = this.clearInterval;\n";

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
    setInterval: () => 0,
    clearInterval: () => {},
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
const act = new (loadAgents(false).AgentActivity)(container);
const hasCaret = () => act.textEl.innerHTML.includes("ob-caret");
const cls = (c) => container._classes.has(c);

act.update({ label: "Thinking", stream: "the model is thinking about", spin: true, startedAt: NOW });
flush();
check("streaming shows the caret", hasCaret(), act.textEl.innerHTML);
check("streaming marks the label as working", cls("is-working"), "");
check("label is the phase, not the prose", act.labelEl.textContent === "Thinking", act.labelEl.textContent);
check("timer counts while working", /\ds$/.test(act.timerEl.textContent), act.timerEl.textContent);
check("no emoji reaches the label", !/[\u{1F300}-\u{1FAFF}]/u.test(act.labelEl.textContent), act.labelEl.textContent);

act.update({
  label: "Thinking",
  stream: "the model is thinking about the page it just loaded",
  spin: false,
  startedAt: NOW,
  seconds: 4.2,
});
flush();
check("settling removes the caret", !hasCaret(), act.textEl.innerHTML);
check("settled text is complete", act.textEl.innerHTML.includes("just loaded"), act.textEl.innerHTML);
check("completion summarises the duration", act.labelEl.textContent === "Thought for 4.2s", act.labelEl.textContent);
check("completion collapses", cls("is-complete") && !cls("is-expanded"), [...container._classes].join(","));
check("a collapsed thought is still on screen", cls("is-open"), [...container._classes].join(","));
check("completion drops the working shimmer", !cls("is-working"), "");

act.headEl.fire("click");
check("chevron expands a completed thought", cls("is-expanded"), [...container._classes].join(","));
check("expanded reports itself", act.headEl.getAttribute("aria-expanded") === "true", "");
act.headEl.fire("click");
check("chevron collapses again", !cls("is-expanded"), "");
check("collapsing does not hide the whole card", cls("is-open"), [...container._classes].join(","));

act.update({ label: "Running actions", spin: false, startedAt: NOW });
flush();
check("a phase with no prose empties the body", act.textEl.innerHTML === "", act.textEl.innerHTML);
check("a phase with no prose is not complete", !cls("is-complete"), "");
check("a phase with no prose offers no Copy", !cls("has-prose"), [...container._classes].join(","));
check("label follows the new phase", act.labelEl.textContent === "Running actions", act.labelEl.textContent);

act.update({ label: "Thinking", stream: "next thought", spin: true, startedAt: NOW });
flush(1);
check("caret returns on the next stream", hasCaret(), act.textEl.innerHTML);

act.trackEl.scrollHeight = 400;
act.update({ label: "Thinking", stream: "a very long thought", spin: true, startedAt: NOW });
flush();
check("overflow masks the viewport", act.viewportEl._classes.has("is-masked"), "");
check(
  "overflow slides the track up rather than scrolling",
  act.trackEl.style.transform === "translateY(-192px)",
  act.trackEl.style.transform
);

act.update({ label: "Thinking", stream: "a very long thought", spin: false, startedAt: NOW, seconds: 9 });
flush();
check("completion stops sliding the track", act.trackEl.style.transform === "", act.trackEl.style.transform);

const rm = new (loadAgents(true).AgentActivity)(makeEl());
rm.update({ label: "Thinking", stream: "reduced motion thought", spin: false, startedAt: NOW, seconds: 1 });
check(
  "reduced motion settles without a caret",
  !rm.textEl.innerHTML.includes("ob-caret"),
  rm.textEl.innerHTML
);

let failed = 0;
for (const [name, ok, detail] of results) {
  if (!ok) failed++;
  console.log(`${ok ? "ok" : "not ok"} - ${name}${ok ? "" : ` :: ${detail}`}`);
}
process.exit(failed ? 1 : 0);
