// Complete-worker Native tests reuse the existing browser/page API boundary.
const vm = require('node:vm');
const { assert, event, page, worker } = require('./dialog_scope_harness.cjs');

const flush = () => new Promise(resolve => setImmediate(resolve));
const config = transport => ({ transport, nativeHost: 'io.browsertap.host', protocol: 1 });

async function nativeWorker(options = {}) {
  const ports = [];
  const sockets = [];
  const events = [];
  const fetches = [];
  const main = page();
  const tabs = options.tabs || [{ id: 73, url: 'https://synthetic.example/', title: 'Synthetic' }];
  const stored = { btap_client_id: 'chrome_synthetic', ...(options.stored || {}) };
  let nativeCalls = 0;

  class NativePort {
    onMessage = event();
    onDisconnect = event();
    sent = [];
    closed = false;
    disconnectCalls = 0;
    postMessage(message) {
      assert.equal(this.closed, false, 'send on closed native port');
      this.sent.push(JSON.parse(JSON.stringify(message)));
      events.push(['native-send', message.type]);
      if (this.failSend) throw new Error('synthetic private payload must not escape');
    }
    async receive(message) { await this.onMessage.emit(message); await flush(); }
    disconnect() {
      if (this.closed) return;
      this.disconnectCalls += 1;
      this.closed = true;
      events.push(['native-close']);
      void this.onDisconnect.emit();
    }
    async drop() { this.disconnect(); await flush(); }
  }

  class Socket {
    static CONNECTING = 0;
    static OPEN = 1;
    readyState = 0;
    sent = [];
    constructor(url) {
      assert.ok(ports.every(port => port.closed), 'Native must close before WebSocket starts');
      assert.ok(sockets.every(socket => socket.readyState > 1), 'only one WebSocket may connect');
      this.url = url;
      sockets.push(this);
      events.push(['websocket-create', url]);
    }
    async open() { this.readyState = 1; await this.onopen(); await flush(); }
    send(message) {
      assert.equal(this.readyState, 1, 'send on closed WebSocket');
      this.sent.push(JSON.parse(message));
    }
    close() {
      if (this.readyState === 3) return;
      this.readyState = 3;
      this.onclose?.();
    }
    async receive(message) { await this.onmessage({ data: JSON.stringify(message) }); }
  }

  const runtime = await worker([main], {
    ...options,
    WebSocket: Socket,
    async fetch(url, request) {
      const index = fetches.length;
      fetches.push({ url, request });
      if (options.fetch) return await options.fetch(url, request, index);
      const value = options.config ? await options.config(url, index) : config('native');
      if (value === null) throw new Error('synthetic bridge unavailable');
      return { ok: true, json: async () => value };
    },
    configureChrome(chrome) {
      chrome.runtime.connectNative = name => {
        nativeCalls += 1;
        assert.equal(name, 'io.browsertap.host');
        if (options.nativeThrows) throw new Error('synthetic native host not installed');
        const port = new NativePort();
        ports.push(port);
        events.push(['native-create']);
        return port;
      };
      chrome.storage.local.get = async keys => Object.fromEntries(
        (Array.isArray(keys) ? keys : [keys]).map(key => [key, stored[key]]),
      );
      chrome.storage.local.set = async value => Object.assign(stored, value);
      chrome.tabs.query = async () => structuredClone(tabs);
      if (options.configureChrome) options.configureChrome(chrome);
    },
  });
  return {
    ...runtime, main, ports, sockets, events, fetches, stored, tabs,
    nativeCalls: () => nativeCalls,
    run: source => vm.runInContext(source, runtime.context),
    async ready(port = ports.at(-1), bridgePort = 18765) {
      await port.receive({ type: 'host_ready', protocol: 1, bridgePort });
    },
  };
}

function chunks(message, size = 256 * 1024) {
  // Protocol fixture: byte-equivalent to an ASCII JSON wire message, including
  // surrogate-pair escapes. This sends real frames through the loaded adapter.
  const serialized = JSON.stringify(message).replace(/[\u007f-\uffff]/g,
    character => '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'));
  const total = Math.ceil(serialized.length / size);
  return Array.from({ length: total }, (_, index) => ({
    type: 'native_chunk', id: 'synthetic-chunk', index, total,
    data: serialized.slice(index * size, (index + 1) * size),
  }));
}

module.exports = { nativeWorker, config, chunks, flush };
