// Offline execution of the complete extension worker and actual page payloads.
// API fakes model transport boundaries; no source functions are sliced/replaced.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const extension = path.resolve(__dirname, '../../src/browsertap_mcp/chrome_extension');
const manifest = JSON.parse(fs.readFileSync(path.join(extension, 'manifest.json'), 'utf8'));

function event() {
  const listeners = new Set();
  return {
    addListener: listener => listeners.add(listener),
    removeListener: listener => listeners.delete(listener),
    emit: (...args) => Promise.all([...listeners].map(listener => listener(...args))),
  };
}

function clock() {
  let now = 1000000;
  let nextId = 1;
  const timers = new Map();
  class ClockDate extends Date {
    static now() { return now; }
  }
  return {
    Date: ClockDate,
    setTimeout(callback, delay) {
      const id = nextId++;
      timers.set(id, { callback, at: now + Number(delay || 0) });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    advance(ms, fire = true) {
      now += ms;
      if (!fire) return;
      let fired = 0;
      for (;;) {
        const due = [...timers].find(([, timer]) => timer.at <= now);
        if (!due) return;
        assert.ok(fired++ < 1000, 'timer loop');
        timers.delete(due[0]);
        due[1].callback();
      }
    },
  };
}

function page(top = null) {
  const time = clock();
  const nativeCalls = [];
  const document = {
    createElement() { return { style: {}, remove() {}, textContent: '' }; },
    body: { appendChild() {} },
    documentElement: { appendChild() {} },
  };
  const sandbox = {
    document,
    console: { log() {}, error() {} },
    Date: time.Date,
    setTimeout: time.setTimeout,
    clearTimeout: time.clearTimeout,
    NodeList: function NodeList() {},
    HTMLCollection: function HTMLCollection() {},
    alert(message) { nativeCalls.push(['alert', message]); },
    confirm(message) { nativeCalls.push(['confirm', message]); return true; },
    prompt(message, value) { nativeCalls.push(['prompt', message]); return value ?? ''; },
    onbeforeunload() { return 'unsaved'; },
  };
  sandbox.window = sandbox;
  sandbox.top = top ? top.window : sandbox;
  const context = vm.createContext(sandbox);
  const names = ['alert', 'confirm', 'prompt', 'onbeforeunload'];
  const original = Object.fromEntries(names.map(name => [name, sandbox[name]]));
  const descriptors = Object.fromEntries(names.map(name => [
    name, Object.getOwnPropertyDescriptor(sandbox, name),
  ]));
  const keys = Reflect.ownKeys(sandbox);
  for (const script of manifest.content_scripts || []) {
    // MAIN-world declarations are what a normal website can observe. The
    // isolated script lives in a separate realm in Chrome, not in this one.
    if (script.world !== 'MAIN') continue;
    for (const file of script.js || []) {
      vm.runInContext(fs.readFileSync(path.join(extension, file), 'utf8'), context, {
        filename: file,
      });
    }
  }
  return {
    window: sandbox, context, time, nativeCalls, original, descriptors, keys,
    run: source => vm.runInContext(source, context, { timeout: 5000 }),
    assertRestored() {
      for (const name of names) {
        assert.deepEqual(Object.getOwnPropertyDescriptor(sandbox, name), descriptors[name], name);
      }
      assert.deepEqual(Reflect.ownKeys(sandbox).filter(key =>
        typeof key === 'string' && key.startsWith('__btap_')), []);
    },
  };
}

async function worker(frames = [], options = {}) {
  const errors = [];
  const messages = [];
  const injections = [];
  const cdpCalls = [];
  const imports = [];
  const storage = () => ({ get: async () => ({}), set: async () => {}, remove: async () => {} });
  const chrome = {
    runtime: {
      id: 'synthetic-extension',
      getManifest: () => manifest,
      onInstalled: event(), onStartup: event(), onConnect: event(), onMessage: event(),
    },
    storage: { local: storage(), session: storage(), onChanged: event() },
    alarms: { onAlarm: event(), create() {}, clear: async () => true },
    tabs: {
      onRemoved: event(), onCreated: event(), onUpdated: event(),
      onActivated: event(), onReplaced: event(),
      query: async () => [], sendMessage: async () => {},
    },
    declarativeNetRequest: {
      getSessionRules: async () => [], updateSessionRules: async () => {},
    },
    debugger: {
      onEvent: event(), onDetach: event(), getTargets: async () => [],
      attach: async () => {}, detach: async () => {},
      async sendCommand(target, method, params) {
        cdpCalls.push({ target, method, params });
        assert.equal(method, 'Runtime.evaluate', 'unexpected CDP API in dialog harness');
        assert.equal(target.tabId, 73);
        return { result: { value: await frames[0].run(params.expression) } };
      },
    },
    scripting: {
      async executeScript(request) {
        assert.deepEqual(JSON.parse(JSON.stringify(request.target)), { tabId: 73, allFrames: true });
        assert.equal(request.world, 'MAIN');
        injections.push(request);
        if (options.rejectCleanup && request.args[0] === 'release') {
          throw new Error('frame no longer scriptable');
        }
        if (options.hangCleanup && request.args[0] === 'release') {
          return new Promise(() => {});
        }
        const results = [];
        // Deliberately return a subframe first and give the top frame a nonzero
        // frame ID. Production must select its marker, not frame order or ID.
        for (const index of frames.map((_, index) => index).reverse()) {
          const frame = frames[index];
          if (options.blockEval) {
            frame.run("window.savedEval = eval; window.eval = () => { throw new EvalError('Refused to evaluate unsafe-eval due to content security policy'); };");
          }
          try {
            const result = await frame.run(`(${request.func.toString()})(...${JSON.stringify(request.args)})`);
            results.push({ frameId: index === 0 ? 91 : index, result });
          } finally {
            if (options.blockEval) frame.run('window.eval = window.savedEval; delete window.savedEval;');
          }
        }
        return results;
      },
    },
  };
  let context;
  class OfflineWebSocket {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 1;
    send(value) { messages.push(JSON.parse(value)); }
    close() { this.readyState = 3; }
  }
  let timerId = 0;
  const timers = new Map();
  context = vm.createContext({
    chrome,
    console: { log() {}, error: (...args) => errors.push(args.map(String).join(' ')) },
    navigator: { userAgent: 'Chrome/130.0.0.0' },
    WebSocket: OfflineWebSocket,
    setTimeout(callback, delay) {
      if (delay === 200) queueMicrotask(callback); // async-tab grace; no real wait
      const id = ++timerId;
      timers.set(id, {callback, delay});
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return ++timerId; }, clearInterval() {},
    fetch() { throw new Error('network access forbidden in offline dialog harness'); },
    importScripts(...files) {
      for (const file of files) {
        assert.equal(path.basename(file), file, 'unexpected worker dependency path');
        imports.push(file);
        vm.runInContext(fs.readFileSync(path.join(extension, file), 'utf8'), context, {
          filename: file,
        });
      }
    },
  });
  vm.runInContext(fs.readFileSync(path.join(extension, 'background.js'), 'utf8'), context, {
    filename: 'background.js', timeout: 5000,
  });
  // Let real asynchronous startup finish against the explicit API fakes.
  await vm.runInContext('Promise.all([bridgeConfigReady, cspStartupCleanup])', context);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(errors, [], 'worker startup logged errors');
  return {
    context, chrome, messages, injections, cdpCalls, imports,
    expireTimers(delay) {
      let count = 0;
      for (const [id, timer] of timers) {
        if (timer.delay !== delay) continue;
        timers.delete(id);
        timer.callback();
        count += 1;
      }
      return count;
    },
    build(code, scope = null, route = 'page') {
      const builder = route === 'cdp' ? 'buildCdpScript' : 'buildPageScript';
      return vm.runInContext(`${builder}(${JSON.stringify(code)}, ${JSON.stringify(scope)})`, context);
    },
    manage(frame, action, scope) {
      const source = vm.runInContext('globalThis.manageDialogScope.toString()', context);
      return frame.run(`(${source})(${JSON.stringify(action)}, ${JSON.stringify(scope)})`);
    },
    async exec(code, policy = 'dismiss') {
      let token = null;
      if (policy) {
        const result = await vm.runInContext(`handleExtMessage(${JSON.stringify({
          cmd: 'set_dialog_policy', tabId: 73, policy, timeoutMs: 3000,
        })})`, context);
        assert.equal(result.ok, true);
        token = result.data.token;
      }
      await vm.runInContext(`handleWsExec(${JSON.stringify({
        id: 'execution', tabId: 73, timeoutMs: 3000,
        code: (token ? `/*__btap_dialog_scope:${token}*/` : '') + code,
      })})`, context);
      const answer = messages.findLast(message => message.type !== 'ack');
      assert.ok(answer, 'worker did not send a command outcome');
      return answer;
    },
  };
}

module.exports = { assert, extension, manifest, page, worker };
