// Deterministic CDP peer with separate DOM realms and real remote-object identity.
// Input is independently routed through the synthetic frame geometry/focus tree.
const fs = require('fs');
const vm = require('vm');
const { webcrypto } = require('crypto');
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
globalThis.crypto = webcrypto;
vm.runInThisContext(fs.readFileSync(request.module, 'utf8'));
const mode = request.mode || 'same_process';
const calls = [];
const inputEvents = [];
const attached = [];
const detached = [];
const objects = new Map();
const worlds = new Map();
let counter = 1;
let clock = 1000;
Date.now = () => clock;
let currentLeaf;
let geometryReads = 0;
let navigated = false;
const styleDefaults = {
  display:'block', visibility:'visible', opacity:'1', transform:'none',
  paddingLeft:'0px', paddingTop:'0px', paddingRight:'0px', paddingBottom:'0px',
};
class Element {
  constructor(doc, tag, id, rect, attributes = {}) {
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.nodeType = 1;
    this.id = id;
    this.attributes = attributes;
    this.type = attributes.type || 'text';
    this.rect = { ...rect };
    this.style = { ...styleDefaults };
    this.parentElement = null;
    this.children = [];
    this.childNodes = [];
    this.textContent = attributes.text || '';
    this.className = attributes.class || '';
    this.isConnected = true;
    this.clientLeft = 0;
    this.clientTop = 0;
    this.clientWidth = rect.width;
    this.clientHeight = rect.height;
    this.value = 'old value';
    doc.elements.push(this);
  }
  getAttribute(name) { return name === 'id' ? this.id : this.attributes[name] ?? null; }
  hasAttribute(name) { return this.getAttribute(name) !== null; }
  getBoundingClientRect() { return { ...this.rect, right:this.rect.left + this.rect.width, bottom:this.rect.top + this.rect.height }; }
  getRootNode() { return this.ownerDocument; }
  checkVisibility() { return this.style.display !== 'none' && this.style.visibility !== 'hidden'; }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  matches(selector) {
    if (selector === '*') return true;
    if (selector === ':disabled') return !!this.disabled;
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.className.split(' ').includes(selector.slice(1));
    if (selector.includes('[') || selector.includes(' ')) return false;
    return selector.toUpperCase() === this.tagName;
  }
  querySelectorAll(selector) { return this.ownerDocument.querySelectorAll(selector).filter(node => node !== this && this.contains(node)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  select() { this.selected = true; }
  focus() {
    if (mode === 'focus_lost') return;
    this.ownerDocument.activeElement = this;
    let doc = this.ownerDocument;
    while (doc.owner) {
      doc.owner.ownerDocument.activeElement = doc.owner;
      doc = doc.owner.ownerDocument;
    }
  }
  scrollIntoView() { throw new Error('must not scroll a framed target'); }
}
function makeDocument(frameId, target, owner = null) {
  const doc = {
    frameId, target, owner, elements:[], nodeType:9, current:true, epoch:1,
    title:'Generic iframe', readyState:'complete', activeElement:null,
    querySelectorAll(selector) {
      if (selector === '[') throw new Error('invalid selector');
      return this.elements.filter(node => node.isConnected && selector.split(',').some(part => node.matches(part.trim())));
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    getElementById(id) { return this.querySelector(`#${id}`); },
    hasFocus() { return mode !== 'no_document_focus' && !(mode === 'root_focus_lost' && frameId === 'root' && inputEvents.length); },
    getRootNode() { return this; },
    elementFromPoint(x, y) {
      return [...this.elements].reverse().find(node => {
        const rect = node.getBoundingClientRect();
        return node.isConnected && node.checkVisibility() && x >= rect.left && y >= rect.top && x < rect.right && y < rect.bottom;
      }) || null;
    },
  };
  const location = { href:`https://${frameId}.test/`, origin:`https://${frameId}.test`, pathname:'/', hostname:`${frameId}.test` };
  const globals = { document:doc, location, innerWidth:800, innerHeight:600, getComputedStyle:node => node.style };
  globals.window = globals;
  doc.defaultView = globals;
  doc.realm = vm.createContext(globals);
  worlds.set(frameId, doc);
  return doc;
}
const root = makeDocument('root', 'tab:7');
const outer = new Element(root, 'iframe', 'outer', { left:100, top:50, width:504, height:304 });
outer.frameId = 'child';
outer.clientLeft = outer.clientTop = 2;
outer.clientWidth = 500;
outer.clientHeight = 300;
const outOfProcess = ['oopif', 'nested', 'wrong_target'].includes(mode);
const child = makeDocument('child', outOfProcess ? 'target:child' : 'tab:7', outer);
Object.defineProperty(outer, 'contentDocument', { get() { throw new Error('cross origin'); } });
currentLeaf = child;
if (mode === 'nested') {
  const inner = new Element(child, 'iframe', 'inner', { left:20, top:25, width:300, height:180 });
  inner.frameId = 'nested';
  currentLeaf = makeDocument('nested', 'target:child', inner);
}
const control = new Element(currentLeaf, request.payload.action === 'type' ? 'input' : 'button', 'control',
  { left:10, top:20, width:120, height:30 }, { 'aria-label':'Control', text:'Control' });
if (['missing', 'missing_replaced'].includes(mode)) control.isConnected = false;
if (mode.startsWith('xterm_')) control.className = 'xterm-helper-textarea';
if (mode.startsWith('challenge')) currentLeaf.title = 'Just a moment';
if (mode === 'disabled') control.disabled = true;
if (mode === 'readonly') control.readOnly = true;
if (mode === 'ambiguous') new Element(currentLeaf, control.tagName, 'control', control.rect);
if (mode === 'hidden_duplicate') {
  const hidden = new Element(currentLeaf, control.tagName, 'control', control.rect);
  hidden.style.display = 'none';
}
if (mode === 'not_a_frame') outer.tagName = 'DIV';
if (mode === 'transformed') outer.style.transform = 'matrix(2,0,0,2,0,0)';
if (mode === 'zoomed') outer.style.zoom = '1.2';
if (mode === 'offscreen') outer.rect.left = -1000;
if (mode === 'padding') {
  outer.style.paddingLeft = '5px'; outer.style.paddingTop = '7px';
}
if (mode === 'parent_overlay') new Element(root, 'aside', 'overlay', { left:0, top:0, width:800, height:600 });
if (mode === 'child_overlay') new Element(currentLeaf, 'aside', 'overlay', { left:0, top:0, width:800, height:600 });
globalThis.setTimeout = (callback, delay) => {
  clock += delay;
  if (mode === 'xterm_navigation') currentLeaf.epoch += 1;
  if (mode === 'xterm_focus') root.activeElement = null;
  callback();
};
function reference(object, doc, group) {
  const id = `object-${counter++}`;
  objects.set(id, { object, doc, epoch:doc.epoch, group });
  return { type:'object', ...(object?.nodeType ? { subtype:'node' } : {}), objectId:id };
}
function getObject(objectId, target) {
  const stored = objects.get(objectId);
  if (!stored || stored.doc.epoch !== stored.epoch) throw new Error('Could not find object with given id');
  if (stored.doc.target !== target) throw new Error('Remote object used in wrong target');
  return stored;
}
function focused() {
  let doc = root;
  let el = doc.activeElement;
  while (el?.frameId) { doc = worlds.get(el.frameId); el = doc.activeElement; }
  return el;
}
function clicked(doc, x, y) {
  const el = doc.elementFromPoint(x, y);
  if (!el?.frameId) return el;
  return clicked(worlds.get(el.frameId), x - el.rect.left - el.clientLeft - (parseFloat(el.style.paddingLeft) || 0),
    y - el.rect.top - el.clientTop - (parseFloat(el.style.paddingTop) || 0));
}
const api = {
  async attach(target) {
    const key = target.targetId ? `target:${target.targetId}` : `tab:${target.tabId}`;
    attached.push(key);
    return { key, released:false };
  },
  async detach(lease) { detached.push(lease.key); lease.released = true; },
  failureCode(error) { return error.code || 'cdp_error'; },
  async send(lease, method, params = {}, timeout, minimum, state) {
    if (lease.released) throw new Error('released lease');
    if (timeout <= 0) throw new Error('unbounded timeout');
    clock += mode === 'deadline' ? 125 : 1;
    calls.push({ target:lease.key, method, ...(method.startsWith('Input.') ? { params } : {}) });
    if (method === 'Page.getFrameTree') return { frameTree:{ frame:{ id:lease.key === 'tab:7' ? 'root' : mode === 'wrong_target' ? 'unrelated' : 'child' } } };
    if (method === 'Page.createIsolatedWorld') {
      const doc = worlds.get(params.frameId);
      if (doc?.target !== lease.key) throw new Error('No frame for given id found');
      return { executionContextId:[...worlds.keys()].indexOf(params.frameId) + 1 };
    }
    if (method === 'Runtime.evaluate') {
      const doc = [...worlds.values()][params.contextId - 1];
      if (doc.target !== lease.key) throw new Error('Cannot find context');
      return { result:reference(doc, doc, params.objectGroup) };
    }
    if (method === 'DOM.describeNode') {
      const object = getObject(params.objectId, lease.key).object;
      return { node:{ nodeName:object.tagName, frameId:object.frameId, backendNodeId:1 } };
    }
    if (method === 'Runtime.callFunctionOn') {
      const stored = getObject(params.objectId, lease.key);
      stored.doc.realm._receiver = stored.object;
      stored.doc.realm._args = (params.arguments || []).map(item => item.value);
      let result;
      try { result = vm.runInContext(`(${params.functionDeclaration}).apply(_receiver, _args)`, stored.doc.realm); }
      finally { delete stored.doc.realm._receiver; delete stored.doc.realm._args; }
      if (result === control && !navigated && ['replaced', 'navigation', 'context_reused'].includes(mode)) {
        navigated = true;
        if (mode === 'replaced') outer.isConnected = false;
        else currentLeaf.epoch += 1;
      }
      if (mode === 'missing_replaced' && result?.status === 'not_found' && stored.doc === currentLeaf)
        outer.isConnected = false;
      if (result?.found && typeof result.x === 'number' && stored.object === control) {
        geometryReads += 1;
        if (geometryReads === 1 && mode === 'moving') control.rect.left += 20;
      }
      return { result: params.returnByValue || result === null || typeof result !== 'object'
        ? { type:typeof result, value:result }
        : reference(result, stored.doc, params.objectGroup) };
    }
    if (method === 'Runtime.releaseObjectGroup') {
      for (const [id, item] of objects) if (item.group === params.objectGroup && item.doc.target === lease.key) objects.delete(id);
      return {};
    }
    if (method.startsWith('Input.')) {
      if (mode === 'input_not_sent') { const error = new Error('debugger detached'); error.code = 'debugger_detached'; throw error; }
      if (state) state.dispatched = true;
      const el = method === 'Input.dispatchMouseEvent' ? clicked(root, params.x, params.y) : focused();
      if (el !== control) throw new Error('INPUT HIT THE WRONG ELEMENT');
      if (method === 'Input.insertText') {
        el.value = el.selected ? params.text : el.value + params.text;
        el.selected = false;
      }
      inputEvents.push({ method, params, target:el.id, value:el.value });
      if (mode === 'challenge_cleared') currentLeaf.title = 'Generic iframe';
      if (mode === 'partial_navigation') currentLeaf.epoch += 1;
      if (mode === 'uncertain') { const error = new Error('reply timed out'); error.code = 'cdp_timeout'; throw error; }
      return {};
    }
    return {};
  },
};
(async () => {
  const payload = { ...request.payload, tabId:7, timeoutMs:1000, deadlineEpochMs:2000 };
  const result = await globalThis.BtapFrameLocator.run(payload, {}, api);
  process.stdout.write(JSON.stringify({ result, calls, inputEvents, attached, detached,
    objectCount:objects.size, value:control.value, elapsed:clock - 1000 }));
})().catch(error => { console.error(error); process.exit(1); });
