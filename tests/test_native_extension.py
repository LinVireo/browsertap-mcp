"""Native frames must drive the complete worker's existing command lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_dialog_scope_installation import run_node

HARNESS = Path(__file__).parent / "node" / "native_transport_harness.cjs"


def run_native(source: str) -> None:
    run_node(
        "const { nativeWorker, config, chunks, flush } = require("
        + json.dumps(str(HARNESS)) + ");\n" + source
    )


def test_native_bootstrap_routes_commands_code_heartbeats_and_tab_snapshots():
    run_native("""
const runtime = await nativeWorker({ config: (_url, index) => index ? config('native') : null });
const port = runtime.ports[0];
assert.equal(runtime.sockets.length, 0);
assert.equal(port.sent.length, 0, 'no extension handshake before host_ready');
await runtime.ready();
const ready = port.sent.find(message => message.type === 'ext_ready');
assert.equal(ready.clientId, 'chrome_synthetic');
assert.equal(ready.tabs[0].id, 73);
assert.ok(ready.tabs[0].generation);
assert.ok(ready.tabs[0].tab_identity);
assert.equal(runtime.run('bridgeStatusMessage().mode'), 'native');
assert.equal(runtime.run('bridgeStatusMessage().ws'), true);
assert.equal(runtime.pendingTimerDelays().includes(5000), false);
assert.deepEqual(runtime.pendingIntervalDelays(), [20000]);
for (const call of runtime.fetches) {
  assert.equal(call.url, 'http://127.0.0.1:18766/api/extension/config');
  assert.equal(call.request.method, 'GET');
  assert.equal(call.request.credentials, 'omit');
  assert.equal(call.request.headers, undefined, 'extension must never read or send the bridge token');
}
await port.receive({ id: 'native-command', cmd: { cmd: 'tabs' } });
assert.equal(port.sent.find(message => message.id === 'native-command').type, 'result');
await port.receive({ id: 'native-code', tabId: 73,
  code: 'window.nativeRuns = (window.nativeRuns || 0) + 1; window.nativeRuns' });
assert.equal(runtime.main.window.nativeRuns, 1);
assert.deepEqual(port.sent.filter(message => message.id === 'native-code').map(message => message.type),
  ['ack', 'result']);
assert.equal(port.sent.find(message => message.id === 'native-code' && message.type === 'result').result, 1);
runtime.tickIntervals(20000);
await flush();
assert.ok(port.sent.some(message => message.type === 'ping'));
assert.ok(port.sent.some(message => message.type === 'tabs_update'));
runtime.advance(1000, false);
await port.receive({ type: 'pong' });
assert.equal(runtime.run('lastPongAt'), 1001000);
assert.equal(runtime.sockets.length, 0);
""")


def test_existing_bridge_websocket_preference_skips_native():
    run_native("""
const runtime = await nativeWorker({ config: () => config('websocket') });
assert.equal(runtime.nativeCalls(), 0);
assert.equal(runtime.sockets.length, 1);
assert.equal(runtime.sockets[0].url, 'ws://127.0.0.1:18765');
await runtime.sockets[0].open();
assert.equal(runtime.sockets[0].sent.filter(message => message.type === 'ext_ready').length, 1);
assert.equal(runtime.run('bridgeStatusMessage().mode'), 'websocket');
""")


def test_host_ready_rechecks_the_actual_bridge_port_before_registration():
    run_native("""
const runtime = await nativeWorker({ config: (_url, index) => index ? config('websocket') : null });
const port = runtime.ports[0];
await runtime.ready(port, 23450);
assert.equal(runtime.fetches[1].url, 'http://127.0.0.1:23451/api/extension/config');
assert.equal(port.closed, true);
assert.equal(port.sent.length, 0, 'an unselected transport must never register tabs');
assert.equal(runtime.sockets.length, 1);
assert.equal(runtime.sockets[0].url, 'ws://127.0.0.1:23450');
assert.ok(runtime.events.findIndex(event => event[0] === 'native-close') <
  runtime.events.findIndex(event => event[0] === 'websocket-create'));
await runtime.sockets[0].open();
assert.equal(runtime.sockets[0].sent.filter(message => message.type === 'ext_ready').length, 1);
""")


@pytest.mark.parametrize("invalid", [
    {}, {"transport": "websocket"},
    {"transport": "websocket", "nativeHost": "other.host", "protocol": 1},
    {"transport": "websocket", "nativeHost": "io.browsertap.host", "protocol": "1"},
    {"transport": "unknown", "nativeHost": "io.browsertap.host", "protocol": 1},
])
def test_invalid_configuration_cannot_select_a_transport_or_another_host(invalid):
    run_native(f"""
const runtime = await nativeWorker({{ config: () => ({json.dumps(invalid)}) }});
assert.equal(runtime.nativeCalls(), 1);
assert.equal(runtime.sockets.length, 0);
await runtime.ready();
assert.equal(runtime.run('bridgeStatusMessage().mode'), 'native');
""")


@pytest.mark.parametrize("failure", ["constructor", "connecting", "open", "timeout"])
def test_native_failure_falls_back_once_and_late_callbacks_cannot_replace_it(failure):
    run_native(f"""
const failure = {json.dumps(failure)};
const runtime = await nativeWorker({{ nativeThrows: failure === 'constructor' }});
let retired;
if (failure !== 'constructor') {{
  retired = runtime.run('ws');
  if (failure === 'open') await runtime.ready();
  if (failure === 'timeout') runtime.expireTimers(5000);
  else await runtime.ports[0].drop();
}}
await flush();
assert.equal(runtime.nativeCalls(), 1);
assert.equal(runtime.sockets.length, 1);
const fallback = runtime.sockets[0];
retired?.onclose();
await runtime.chrome.runtime.onStartup.emit();
await runtime.chrome.alarms.onAlarm.emit({{ name: 'btap-ws-probe' }});
await flush();
assert.equal(runtime.run('ws'), fallback);
assert.equal(runtime.sockets.length, 1);
assert.equal(runtime.nativeCalls(), 1);
fallback.close();
await runtime.chrome.runtime.onStartup.emit();
await flush();
assert.equal(runtime.sockets.length, 2, 'the existing WebSocket retry loop remains active');
assert.equal(runtime.nativeCalls(), 1, 'failed Native is not retried on every wake-up event');
""")


@pytest.mark.parametrize("message", [
    None, [], "host_ready", {}, {"type": "pong"},
    {"type": "host_ready", "protocol": 2, "bridgePort": 18765},
    {"type": "host_ready", "protocol": True, "bridgePort": 18765},
    {"type": "host_ready", "protocol": 1, "bridgePort": "18765"},
    {"type": "host_ready", "protocol": 1, "bridgePort": 1023},
    {"type": "host_ready", "protocol": 1, "bridgePort": 65534},
    {"type": "host_ready", "protocol": 1, "bridgePort": 18765.5},
])
def test_invalid_host_readiness_closes_without_registration(message):
    run_native(f"""
const runtime = await nativeWorker();
const port = runtime.ports[0];
await port.receive({json.dumps(message)});
assert.equal(port.closed, true);
assert.deepEqual(port.sent, []);
assert.equal(runtime.fetches.length, 1, 'an invalid host cannot redirect the HTTP config read');
assert.equal(runtime.sockets.length, 1);
""")


@pytest.mark.parametrize("large", [False, True])
def test_ordered_chunks_deliver_one_complete_command_with_unicode(large):
    run_native(f"""
const runtime = await nativeWorker();
await runtime.ready();
const port = runtime.ports[0];
const padding = {str(large).lower()} ? '/*' + 'x'.repeat(1200000) + '*/' : '';
const frames = chunks({{ id: 'chunk-command', tabId: 73,
  code: padding + 'window.nativeRuns = (window.nativeRuns || 0) + 1; ({{ text: "中文😀", runs: window.nativeRuns }})',
}}, {256 * 1024 if large else 11});
assert.ok(frames.length > 1);
for (const frame of frames.slice(0, -1)) {{
  await port.receive(frame);
  assert.equal(runtime.main.window.nativeRuns, undefined);
}}
await port.receive(frames.at(-1));
assert.equal(runtime.main.window.nativeRuns, 1);
const replies = port.sent.filter(message => message.id === 'chunk-command');
assert.deepEqual(replies.map(message => message.type), ['ack', 'result']);
assert.deepEqual(replies[1].result, {{ text: '中文😀', runs: 1 }});
assert.equal(runtime.sockets.length, 0);
""")


@pytest.mark.parametrize("corruption", [
    "first-index", "duplicate", "gap", "changed-id", "changed-total", "interleaved",
    "unicode", "not-json", "null", "array", "primitive", "nested", "host-ready",
    "too-many", "zero-total", "boolean-index", "fraction-index", "empty-id",
    "long-id", "nonstr-data", "empty-data", "oversized-frame",
])
def test_invalid_chunks_fail_closed_before_any_browser_command(corruption):
    run_native(f"""
const corruption = {json.dumps(corruption)};
const runtime = await nativeWorker();
await runtime.ready();
const port = runtime.ports[0];
const first = {{ type: 'native_chunk', id: 'synthetic', index: 0, total: 3, data: '{{' }};
const second = {{ ...first, index: 1, data: '"type":' }};
let frames = [first];
switch (corruption) {{
  case 'first-index': first.index = 1; break;
  case 'duplicate': frames.push(first); break;
  case 'gap': frames.push({{ ...second, index: 2 }}); break;
  case 'changed-id': frames.push({{ ...second, id: 'other' }}); break;
  case 'changed-total': frames.push({{ ...second, total: 4 }}); break;
  case 'interleaved': frames.push({{ type: 'pong' }}); break;
  case 'unicode': first.data = '中文'; break;
  case 'not-json': first.total = 1; break;
  case 'null': first.total = 1; first.data = 'null'; break;
  case 'array': first.total = 1; first.data = '[]'; break;
  case 'primitive': first.total = 1; first.data = '42'; break;
  case 'nested': first.total = 1; first.data = '{{"type":"native_chunk"}}'; break;
  case 'host-ready': first.total = 1; first.data = '{{"type":"host_ready"}}'; break;
  case 'too-many': first.total = 257; break;
  case 'zero-total': first.total = 0; break;
  case 'boolean-index': first.index = false; break;
  case 'fraction-index': first.index = 0.5; break;
  case 'empty-id': first.id = ''; break;
  case 'long-id': first.id = 'x'.repeat(129); break;
  case 'nonstr-data': first.data = {{}}; break;
  case 'empty-data': first.data = ''; break;
  case 'oversized-frame': first.data = 'x'.repeat(1024 * 1024); break;
}}
for (const frame of frames) await port.receive(frame);
assert.equal(port.closed, true);
assert.equal(runtime.sockets.length, 1);
assert.equal(runtime.main.window.nativeRuns, undefined);
assert.equal(port.sent.some(message => message.type === 'ack'), false);
""")


def test_chunk_accumulation_stops_at_the_64_mib_boundary():
    run_native("""
const runtime = await nativeWorker();
await runtime.ready();
const port = runtime.ports[0];
const data = ' '.repeat(512 * 1024);
for (let index = 0; index < 128; index++) {
  await port.receive({ type: 'native_chunk', id: 'bounded', index, total: 130, data });
}
assert.equal(port.closed, false, 'exactly 64 MiB is within the logical limit');
await port.receive({ type: 'native_chunk', id: 'bounded', index: 128, total: 130, data });
assert.equal(port.closed, true);
assert.equal(runtime.sockets.length, 1);
""")


def test_reset_invalidates_a_pending_configuration_read():
    run_native("""
let release;
const runtime = await nativeWorker({ config: (_url, index) => index === 0
  ? new Promise(resolve => { release = resolve; }) : config('native') });
assert.equal(runtime.ports.length, 0);
runtime.run('resetConnection()');
await flush();
await runtime.ready();
release(config('websocket'));
await flush();
assert.equal(runtime.ports.length, 1);
assert.equal(runtime.sockets.length, 0);
assert.equal(runtime.run('bridgeStatusMessage().mode'), 'native');
""")


def test_reset_during_host_ready_cannot_register_the_retired_native_port():
    run_native("""
let release;
const runtime = await nativeWorker({ config: (_url, index) => index === 1
  ? new Promise(resolve => { release = resolve; }) : config('native') });
const retired = runtime.ports[0];
await runtime.ready(retired, 23450);
runtime.run('resetConnection()');
await flush();
assert.equal(retired.closed, true);
assert.equal(runtime.ports.length, 2);
await runtime.ready(runtime.ports[1], 23460);
release(config('websocket'));
await flush();
assert.equal(retired.sent.length, 0);
assert.equal(runtime.sockets.length, 0);
assert.equal(runtime.run('bridgePort'), 23460);
assert.equal(runtime.run('bridgeStatusMessage().mode'), 'native');
""")


def test_disconnect_during_execution_never_replays_or_answers_on_the_new_socket():
    run_native("""
let release;
const gate = new Promise(resolve => { release = resolve; });
const runtime = await nativeWorker({ beforeInjection: () => gate });
await runtime.ready();
const port = runtime.ports[0];
await port.receive({ id: 'uncertain', tabId: 73,
  code: 'window.nativeRuns = (window.nativeRuns || 0) + 1; window.nativeRuns' });
assert.equal(port.sent.filter(message => message.id === 'uncertain').length, 1);
assert.equal(port.sent.find(message => message.id === 'uncertain').type, 'ack');
await port.drop();
const fallback = runtime.sockets[0];
await fallback.open();
release();
await flush();
assert.equal(runtime.main.window.nativeRuns, 1);
assert.equal(runtime.injections.length, 1);
assert.equal(port.sent.filter(message => message.id === 'uncertain').length, 1);
assert.equal(fallback.sent.some(message => message.id === 'uncertain'), false);
""")


def test_keepalive_send_failure_keeps_the_new_fallback_socket():
    run_native("""
const runtime = await nativeWorker();
await runtime.ready();
runtime.ports[0].failSend = true;
runtime.tickIntervals(20000);
await flush();
assert.equal(runtime.ports[0].closed, true);
assert.equal(runtime.sockets.length, 1);
assert.equal(runtime.run('ws'), runtime.sockets[0]);
assert.equal(runtime.run('connectInFlight'), true);
assert.deepEqual(runtime.pendingIntervalDelays(), []);
assert.deepEqual(runtime.errors, []);
""")


def test_failed_native_ack_does_not_start_or_replay_browser_execution():
    run_native("""
const runtime = await nativeWorker();
await runtime.ready();
const port = runtime.ports[0];
port.failSend = true;
await port.receive({ id: 'unconfirmed-ack', tabId: 73,
  code: 'window.nativeRuns = (window.nativeRuns || 0) + 1' });
assert.equal(port.closed, true);
assert.equal(runtime.main.window.nativeRuns, undefined);
assert.equal(runtime.injections.length, 0);
assert.equal(port.sent.filter(message => message.id === 'unconfirmed-ack').length, 1);
assert.equal(runtime.sockets.length, 1);
assert.equal(runtime.sockets[0].sent.some(message => message.id === 'unconfirmed-ack'), false);
""")


def test_config_timeout_releases_the_attempt_and_native_handshake_clears_watchdogs():
    run_native("""
const runtime = await nativeWorker({ fetch: (_url, request, index) => index === 0
  ? new Promise((_resolve, reject) => request.signal.addEventListener('abort',
    () => reject(new Error('aborted')), { once: true }))
  : Promise.resolve({ ok: true, json: async () => config('native') }) });
assert.equal(runtime.nativeCalls(), 0);
runtime.expireTimers(1500);
await flush();
assert.equal(runtime.fetches[0].request.signal.aborted, true);
assert.equal(runtime.nativeCalls(), 1);
await runtime.ready();
assert.equal(runtime.pendingTimerDelays().includes(1500), false);
assert.equal(runtime.pendingTimerDelays().includes(5000), false);
""")


def test_port_change_invalidates_old_selection_and_uses_the_new_config_url():
    run_native("""
let release;
const runtime = await nativeWorker({ config: (_url, index) => index === 0
  ? new Promise(resolve => { release = resolve; }) : config('websocket') });
await runtime.chrome.storage.onChanged.emit({ btap_port: { newValue: 23450 } }, 'local');
await flush();
release(config('native'));
await flush();
assert.equal(runtime.nativeCalls(), 0);
assert.equal(runtime.sockets.length, 1);
assert.equal(runtime.sockets[0].url, 'ws://127.0.0.1:23450');
assert.equal(runtime.fetches[1].url, 'http://127.0.0.1:23451/api/extension/config');
""")


def test_late_generation_read_never_sends_ext_ready_on_the_retired_transport():
    run_native("""
let release;
const gate = new Promise(resolve => { release = resolve; });
const runtime = await nativeWorker({ configureChrome(chrome) {
  chrome.storage.session.get = async key => key === 'btapTabGenerationsV1' ? gate : {};
} });
const retiredPort = runtime.ports[0];
const retiredSocket = runtime.run('ws');
let retiredSends = 0;
const send = retiredSocket.send.bind(retiredSocket);
retiredSocket.send = value => { retiredSends += 1; return send(value); };
await runtime.ready();
runtime.run('resetConnection()');
await flush();
await runtime.ready(runtime.ports[1]);
release({ btapTabGenerationsV1: { '73': 'durable-generation' } });
await flush();
assert.equal(retiredPort.closed, true);
assert.equal(retiredSends, 0);
assert.equal(runtime.ports[1].sent.filter(message => message.type === 'ext_ready').length, 1);
""")


def test_native_pong_timeout_closes_before_websocket_recovery():
    run_native("""
const runtime = await nativeWorker();
await runtime.ready();
runtime.advance(56000, false);
runtime.tickIntervals(20000);
await flush();
assert.equal(runtime.ports[0].closed, true);
assert.equal(runtime.sockets.length, 1);
assert.equal(runtime.run('ws'), runtime.sockets[0]);
assert.equal(runtime.nativeCalls(), 1);
""")
