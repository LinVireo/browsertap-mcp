// Native Messaging transport for the existing bridge command dispatcher.
/* exported NativeBridgeSocket, NATIVE_HOST_NAME, NATIVE_PROTOCOL */

const NATIVE_HOST_NAME = 'io.browsertap.host';
const NATIVE_PROTOCOL = 1;
const NATIVE_MAX_FRAME_BYTES = 1024 * 1024 - 1;
const NATIVE_MAX_MESSAGE_BYTES = 64 * 1024 * 1024;
// The host writes 256 KiB pieces. Bound the piece count as well as their bytes
// so tiny malicious pieces cannot allocate an unbounded array of strings.
const NATIVE_MAX_CHUNKS = 256;

class NativeBridgeSocket {
  constructor(acceptHost = async () => true) {
    this.transport = 'native';
    this.readyState = 0; // WebSocket.CONNECTING; OPEN=1, CLOSED=3
    this.bridgePort = null;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    this.port = null;
    this.chunks = null;
    this.hostReadyReceived = false;
    this.acceptHost = acceptHost;
    this.receive = message => this.receiveMessage(message);
    this.disconnected = () => {
      // Reading lastError consumes Chrome's diagnostic. Never log its raw
      // text, native frames, command bodies, or browser results.
      void chrome.runtime.lastError;
      this.finish('native_disconnected');
    };
    try {
      this.port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
      this.port.onMessage.addListener(this.receive);
      this.port.onDisconnect.addListener(this.disconnected);
    } catch (_) {
      // Match WebSocket's asynchronous close: background.js must first get a
      // chance to attach handlers and establish this attempt's ownership.
      void Promise.resolve().then(() => this.finish('native_unavailable'));
    }
  }

  finish(reason) {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.chunks = null;
    const port = this.port;
    this.port = null;
    if (port) {
      try { port.onMessage.removeListener(this.receive); } catch (_) {}
      try { port.onDisconnect.removeListener(this.disconnected); } catch (_) {}
      try { port.disconnect(); } catch (_) {}
    }
    this.onclose?.({ type: 'close', reason, wasClean: reason === 'closed' });
  }

  close() {
    this.finish('closed');
  }

  send(value) {
    if (this.readyState !== 1 || !this.port) throw new Error('Native transport is not open');
    let message;
    try {
      if (typeof value !== 'string' || new TextEncoder().encode(value).length > NATIVE_MAX_MESSAGE_BYTES) {
        throw new Error('invalid frame');
      }
      message = JSON.parse(value);
      if (!message || typeof message !== 'object' || Array.isArray(message)) throw new Error('invalid frame');
    } catch (_) {
      this.finish('invalid_outgoing_message');
    }
    // Only fixed errors escape this boundary. Native API errors can carry
    // browser data, so do not retain them as a logged Error.cause either.
    if (this.readyState !== 1) throw new Error('Invalid Native transport message');
    try {
      this.port.postMessage(message);
      return;
    } catch (_) {
      // Delivery is uncertain once postMessage was called. Close, never replay.
      this.finish('native_send_failed');
    }
    throw new Error('Native transport send failed');
  }

  receiveMessage(message) {
    if (this.readyState === 3) return;
    try {
      if (!message || typeof message !== 'object' || Array.isArray(message)) throw new Error('invalid frame');
      const serialized = JSON.stringify(message);
      if (new TextEncoder().encode(serialized).length > NATIVE_MAX_FRAME_BYTES) throw new Error('oversized frame');
      if (!this.hostReadyReceived) {
        if (message.type !== 'host_ready' || message.protocol !== NATIVE_PROTOCOL ||
            !Number.isInteger(message.bridgePort) || message.bridgePort < 1024 || message.bridgePort > 65533) {
          throw new Error('invalid host readiness');
        }
        this.hostReadyReceived = true;
        this.bridgePort = message.bridgePort;
        // The host may have started a bridge on a configured non-default port.
        // Let the connection owner re-read that bridge's preference before any
        // ext_ready, keepalive, tab snapshot, or browser command can be sent.
        void Promise.resolve().then(() => this.readyState === 0 && this.acceptHost(this)).then(accepted => {
          if (this.readyState !== 0) return;
          if (!accepted) { this.finish('transport_not_selected'); return; }
          this.readyState = 1;
          this.onopen?.({ type: 'open' });
        }).catch(() => this.finish('host_configuration_failed'));
        return;
      }
      if (this.readyState !== 1 || message.type === 'host_ready') throw new Error('unexpected frame');
      if (message.type === 'native_chunk') {
        this.receiveChunk(message);
        return;
      }
      if (this.chunks) throw new Error('interleaved frame');
      this.onmessage?.({ data: serialized });
    } catch (_) {
      this.finish('invalid_native_frame');
    }
  }

  receiveChunk(message) {
    const { id, index, total, data } = message;
    if (typeof id !== 'string' || !id || id.length > 128 ||
        !Number.isInteger(index) || !Number.isInteger(total) ||
        total < 1 || total > NATIVE_MAX_CHUNKS || index < 0 || index >= total ||
        typeof data !== 'string' || !data || /[^\x20-\x7e]/.test(data)) {
      throw new Error('invalid chunk');
    }
    if (!this.chunks) {
      if (index !== 0) throw new Error('missing first chunk');
      this.chunks = { id, total, next: 0, bytes: 0, pieces: [] };
    }
    const chunks = this.chunks;
    if (id !== chunks.id || total !== chunks.total || index !== chunks.next ||
        chunks.bytes + data.length > NATIVE_MAX_MESSAGE_BYTES) {
      throw new Error('invalid chunk sequence');
    }
    chunks.pieces.push(data);
    chunks.bytes += data.length;
    chunks.next += 1;
    if (chunks.next !== total) return;
    const serialized = chunks.pieces.join('');
    this.chunks = null;
    const complete = JSON.parse(serialized);
    if (!complete || typeof complete !== 'object' || Array.isArray(complete) ||
        complete.type === 'native_chunk' || complete.type === 'host_ready') {
      throw new Error('invalid completed message');
    }
    this.onmessage?.({ data: serialized });
  }
}
