"use strict";
// A hand-written DOM stand-in for running the plan dashboard's page script under node, with no
// npm package. It models only what the page uses: elements and text, classList, dataset,
// attributes, event dispatch with bubbling, focus, a CSS-rule list parsed from the page's own
// <style> (custom properties, @media prefers-color-scheme, descendant selectors) behind
// getComputedStyle, a fake fetch that logs every request, a fake EventSource the test drives by
// name, and a test-controlled clock in place of the timers. It lays nothing out: "in view" is
// decided from the CSS rules, never from a geometry.
//
//   const win = load({html, routes: {"/api/snapshot": {...}}, dark: false});
//   win.emit("snapshot", obj); await win.settle();
//   win.requests            every URL fetch was called with, in order
//   win.sources             every EventSource the page opened
//   win.clock.advance(ms)   runs due timers;  win.clock.live()  the timers still armed
//   win.run("selected")     evaluates an expression in the page's own scope

const vm = require("vm");

const VOID = new Set(["meta", "link", "br", "hr", "img", "input"]);
const RAW = new Set(["script", "style"]);
const FOCUSABLE = new Set(["button", "input", "select", "textarea"]);

class TextNode {
  constructor(data) { this.data = data; this.parentNode = null; this.nodeType = 3; }
  get textContent() { return this.data; }
}

class Event {
  constructor(type, init) {
    Object.assign(this, init || {});
    this.type = type;
    this.defaultPrevented = false;
    this.stopped = false;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this.stopped = true; }
}

class Target {
  constructor() { this.listeners = {}; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  dispatchEvent(event) {
    if (!(event instanceof Event)) event = new Event(event.type, event);
    event.target = event.target || this;
    for (let node = this; node && !event.stopped; node = node.parentNode || node.parent) {
      event.currentTarget = node;
      for (const fn of [...((node.listeners || {})[event.type] || [])]) fn.call(node, event);
    }
    return !event.defaultPrevented;
  }
}

class Element extends Target {
  constructor(doc, tag) {
    super();
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.localName = tag.toLowerCase();
    this.nodeType = 1;
    this.attributes = {};
    this.childNodes = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    const self = this;
    this.classList = {
      contains: (c) => self.classes().includes(c),
      add: (...cs) => self.setAttribute("class", [...new Set([...self.classes(), ...cs])].join(" ")),
      remove: (...cs) => self.setAttribute("class", self.classes().filter((c) => !cs.includes(c)).join(" ")),
      toggle: (c) => (self.classList.contains(c) ? self.classList.remove(c) : self.classList.add(c)),
    };
  }
  classes() { return (this.attributes.class || "").split(/\s+/).filter(Boolean); }
  get className() { return this.attributes.class || ""; }
  set className(v) { this.attributes.class = String(v); }
  get id() { return this.attributes.id || ""; }
  get hidden() { return "hidden" in this.attributes; }
  set hidden(v) { if (v) this.attributes.hidden = ""; else delete this.attributes.hidden; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    if (v !== "" && v !== null && v !== undefined) this.appendChild(new TextNode(String(v)));
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  hasAttribute(name) { return name in this.attributes; }
  removeAttribute(name) { delete this.attributes[name]; }
  appendChild(node) {
    if (node.parentNode) node.parentNode.childNodes = node.parentNode.childNodes.filter((n) => n !== node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }
  replaceChildren(...nodes) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    nodes.forEach((n) => this.appendChild(typeof n === "string" ? new TextNode(n) : n));
  }
  remove() { if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((n) => n !== this); this.parentNode = null; }
  get isHidden() {
    for (let node = this; node && node.nodeType === 1; node = node.parentNode) {
      if (this.ownerDocument.display(node) === "none") return true;
    }
    return false;
  }
  get focusable() {
    return (this.hasAttribute("tabindex") || FOCUSABLE.has(this.localName)) && !this.isHidden;
  }
  focus() { if (this.focusable) this.ownerDocument.active = this; }
  blur() { if (this.ownerDocument.active === this) this.ownerDocument.active = null; }
  click() { this.dispatchEvent(new Event("click", { bubbles: true })); }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => node.children.forEach((child) => { if (this.ownerDocument.matches(child, selector)) found.push(child); walk(child); });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
}

// ---- HTML ---------------------------------------------------------------------------------

function parseHtml(doc, html) {
  const root = new Element(doc, "html");
  const stack = [root];
  const top = () => stack[stack.length - 1];
  const tag = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-z][a-z0-9]*)\s*>|<([a-z][a-z0-9]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>/gi;
  let at = 0, match;
  const text = (end) => {
    const data = html.slice(at, end);
    if (data.trim() !== "") top().appendChild(new TextNode(data));
  };
  while ((match = tag.exec(html))) {
    text(match.index);
    at = tag.lastIndex;
    if (match[1]) {
      const name = match[1].toLowerCase();
      while (stack.length > 1 && top().localName !== name) stack.pop();
      if (stack.length > 1) stack.pop();
    } else if (match[2]) {
      const name = match[2].toLowerCase();
      const element = new Element(doc, name);
      const attr = /([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = attr.exec(match[3]))) element.setAttribute(a[1].toLowerCase(), a[2] ?? a[3] ?? a[4] ?? "");
      if (name === "html") { Object.assign(root.attributes, element.attributes); continue; }
      top().appendChild(element);
      if (RAW.has(name)) {
        const close = html.toLowerCase().indexOf("</" + name, at);
        element.appendChild(new TextNode(html.slice(at, close)));
        tag.lastIndex = at = html.indexOf(">", close) + 1;
      } else if (!VOID.has(name) && !match[4]) {
        stack.push(element);
      }
    }
  }
  return root;
}

// ---- CSS ----------------------------------------------------------------------------------

function parseCss(source) {
  const css = source.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = [];
  const parse = (text, media) => {
    let i = 0;
    while (i < text.length) {
      const open = text.indexOf("{", i);
      if (open < 0) break;
      let depth = 1, j = open + 1;
      while (j < text.length && depth) { depth += text[j] === "{" ? 1 : text[j] === "}" ? -1 : 0; j++; }
      const prelude = text.slice(i, open).trim(), body = text.slice(open + 1, j - 1);
      i = j;
      if (prelude.startsWith("@media")) { parse(body, prelude.slice(6).trim()); continue; }
      if (prelude.startsWith("@")) continue;
      const decls = [];
      for (const part of body.split(";")) {
        const colon = part.indexOf(":");
        if (colon < 0) continue;
        const name = part.slice(0, colon).trim().toLowerCase();
        let value = part.slice(colon + 1).trim();
        const important = /!\s*important\s*$/i.test(value);
        value = value.replace(/!\s*important\s*$/i, "").trim();
        if (name) decls.push({ name, value, important });
      }
      for (const selector of prelude.split(",")) rules.push({ selector: selector.trim(), decls, media, order: rules.length });
    }
  };
  parse(css, null);
  return rules;
}

const SIMPLE = /^(\*|[a-z][a-z0-9-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=["']?([^\]"']*)["']?)?\]|:([\w-]+)/gi;

function compound(text) {
  const c = { tag: null, id: null, classes: [], attrs: [], pseudos: [] };
  for (const m of text.matchAll(SIMPLE)) {
    if (m[1]) c.tag = m[1].toLowerCase(); else if (m[2]) c.id = m[2]; else if (m[3]) c.classes.push(m[3]);
    else if (m[4]) c.attrs.push([m[4], m[5]]); else if (m[6]) c.pseudos.push(m[6]);
  }
  return c;
}

function specificity(selector) {
  let score = 0;
  for (const part of selector.split(/\s*>\s*|\s+/)) {
    const c = compound(part);
    score += (c.id ? 100 : 0) + (c.classes.length + c.attrs.length + c.pseudos.length) * 10 + (c.tag && c.tag !== "*" ? 1 : 0);
  }
  return score;
}

// ---- document -----------------------------------------------------------------------------

class Document extends Target {
  constructor(html, rules, dark) {
    super();
    this.dark = dark;
    this.active = null;
    this.rules = rules;
    this.documentElement = parseHtml(this, html);
    this.documentElement.parentNode = null;
    this.documentElement.parent = this;
    this.nodeType = 9;
  }
  get body() { return this.documentElement.querySelector("body"); }
  get activeElement() { return this.active || this.body; }
  createElement(tag) { return new Element(this, tag); }
  createTextNode(text) { return new TextNode(String(text)); }
  getElementById(id) { return this.documentElement.querySelectorAll("#" + id)[0] || null; }
  querySelector(selector) { return this.documentElement.querySelector(selector); }
  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }

  matches(el, selector) {
    const parts = selector.trim().split(/\s*(>)\s*|\s+/).filter((p) => p !== undefined && p !== "");
    let index = parts.length - 1, node = el;
    if (!this.matchCompound(node, compound(parts[index]))) return false;
    index--;
    while (index >= 0) {
      let direct = false;
      if (parts[index] === ">") { direct = true; index--; }
      const want = compound(parts[index]);
      node = node.parentNode;
      while (node && node.nodeType === 1 && !this.matchCompound(node, want)) {
        if (direct) return false;
        node = node.parentNode;
      }
      if (!node || node.nodeType !== 1) return false;
      index--;
    }
    return true;
  }

  matchCompound(el, c) {
    if (c.tag && c.tag !== "*" && el.localName !== c.tag) return false;
    if (c.id && el.id !== c.id) return false;
    if (c.classes.some((cls) => !el.classList.contains(cls))) return false;
    if (c.attrs.some(([name, value]) => !el.hasAttribute(name) || (value !== undefined && el.getAttribute(name) !== value))) return false;
    return c.pseudos.every((p) => (p === "root" ? el === this.documentElement : p === "focus" ? this.active === el : false));
  }

  cascade(el) {
    const declared = [];
    for (const rule of this.rules) {
      if (rule.media !== null && !(this.dark && /prefers-color-scheme:\s*dark/.test(rule.media))) continue;
      if (!this.matches(el, rule.selector)) continue;
      const spec = specificity(rule.selector);
      for (const d of rule.decls) declared.push({ ...d, spec, order: rule.order });
    }
    declared.sort((a, b) => (a.important - b.important) || (a.spec - b.spec) || (a.order - b.order));
    const out = {};
    for (const d of declared) out[d.name] = d.value;
    return out;
  }

  rootVars() {
    const vars = {};
    for (const [name, value] of Object.entries(this.cascade(this.documentElement))) if (name.startsWith("--")) vars[name] = value;
    return vars;
  }

  display(el) {
    const own = this.cascade(el).display;
    if (own !== undefined) return own;
    return el.hasAttribute("hidden") ? "none" : "block";
  }

  computedStyle(el) {
    const vars = this.rootVars();
    const resolve = (value) => {
      let guard = 0;
      while (/var\(/.test(value) && guard++ < 10) {
        value = value.replace(/var\((--[\w-]+)(?:\s*,\s*([^)]*))?\)/g, (_, name, fallback) => vars[name] ?? fallback ?? "");
      }
      return value;
    };
    const style = {};
    for (const [name, value] of Object.entries(this.cascade(el))) {
      const resolved = resolve(value);
      style[name] = resolved;
      style[name.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = resolved;
    }
    if (style.display === undefined) style.display = el.hasAttribute("hidden") ? "none" : "block";
    style.getPropertyValue = (name) => style[name] ?? "";
    return style;
  }
}

// ---- timers, fetch, EventSource -----------------------------------------------------------

function makeClock() {
  let now = 0, next = 1;
  const timers = new Map();
  const add = (fn, ms, repeat) => {
    const id = next++;
    timers.set(id, { id, fn, ms: Math.max(Number(ms) || 0, 0), repeat, due: now + Math.max(Number(ms) || 0, 1) });
    return id;
  };
  return {
    setTimeout: (fn, ms) => add(fn, ms, false),
    setInterval: (fn, ms) => add(fn, ms, true),
    clearTimeout: (id) => { timers.delete(id); },
    clearInterval: (id) => { timers.delete(id); },
    now: () => now,
    live: () => [...timers.values()].map((t) => ({ id: t.id, ms: t.ms, repeat: t.repeat, due: t.due })),
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...timers.values()].filter((t) => t.due <= end).sort((a, b) => a.due - b.due || a.id - b.id)[0];
        if (!due) break;
        now = due.due;
        if (due.repeat) due.due = now + Math.max(due.ms, 1); else timers.delete(due.id);
        due.fn();
      }
      now = end;
    },
  };
}

function load({ html, routes = {}, dark = false }) {
  const doc = new Document(html, [], dark);
  const style = doc.documentElement.querySelector("style");
  doc.rules = style ? parseCss(style.textContent) : [];
  const clock = makeClock();
  const requests = [];
  const sources = [];

  const fakeFetch = async (url) => {
    requests.push(String(url));
    const path = String(url).split("?")[0];
    const route = routes[path];
    const found = route !== undefined;
    const body = typeof route === "function" ? route(path) : route;
    return { ok: found, status: found ? 200 : 404, json: async () => JSON.parse(JSON.stringify(body)), text: async () => JSON.stringify(body) };
  };

  class EventSource extends Target {
    constructor(url) { super(); this.url = url; this.onerror = null; this.closed = false; sources.push(this); }
    emit(name, data) { for (const fn of this.listeners[name] || []) fn({ type: name, data: JSON.stringify(data) }); }
    fail() { if (this.onerror) this.onerror({ type: "error" }); }
    close() { this.closed = true; }
  }

  const sandbox = {
    document: doc, fetch: fakeFetch, EventSource, console,
    setTimeout: clock.setTimeout, setInterval: clock.setInterval, clearTimeout: clock.clearTimeout, clearInterval: clock.clearInterval,
    getComputedStyle: (el) => doc.computedStyle(el),
    matchMedia: (query) => ({ matches: dark && /prefers-color-scheme:\s*dark/.test(query), media: query }),
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  const scripts = doc.documentElement.querySelectorAll("script");
  for (const script of scripts) vm.runInContext(script.textContent, context, { filename: "page-script" });

  const win = {
    document: doc, requests, sources, clock, context, rules: doc.rules,
    run: (code) => vm.runInContext(code, context),
    computed: (el) => doc.computedStyle(el),
    emit: (name, data, index = 0) => sources[index].emit(name, data),
    key(key, target) { return (target || doc.activeElement).dispatchEvent(new Event("keydown", { key, bubbles: true })); },
    async settle() { for (let i = 0; i < 12; i++) await new Promise((resolve) => setImmediate(resolve)); },
  };
  return win;
}

module.exports = { load, parseCss, Event };
