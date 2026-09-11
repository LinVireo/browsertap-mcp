"""Exercise permission recovery and durable tab lifetimes in real worker code."""

from __future__ import annotations

import json

import pytest

from tests.test_phase0_recovery import (
    BACKGROUND,
    _create_operation_source,
    _run_generation_harness,
    _run_node_script,
)


def _permission_scenario(scenario: str) -> dict:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("const PERMISSION_LEASES_KEY")
    end = source.index("// --- Dialog policy and runtime execution contexts", start)
    return _run_node_script(
        r"""
const saved = { btapPermissionLeases: [] };
const alarms = new Map();
const attempts = [];
const logs = [];
console.log = (...args) => logs.push(args.join(' '));
let now = 1000000;
Date.now = () => now;
let storageUnavailable = false;
let permissionFailure = '';
let debuggerFailure = '';
let attachFailure = '';
const effective = new Map();
const chrome = {
  storage: { local: {
    get: async keys => Object.fromEntries(keys.map(key => [key, saved[key]])),
    set: async value => {
      if (storageUnavailable) throw new Error('storage temporarily unavailable');
      Object.assign(saved, structuredClone(value));
    },
  } },
  alarms: {
    create: async (name, details) => alarms.set(name, details),
    clear: async name => alarms.delete(name),
  },
  contentSettings: { camera: {
    get: async ({ primaryUrl }) => ({ setting: effective.get(primaryUrl) || 'ask' }),
    set: async ({ primaryPattern, setting }) => {
      attempts.push({ origin: primaryPattern.slice(0, -2), setting });
      if (permissionFailure) throw new Error(permissionFailure);
      effective.set(primaryPattern.slice(0, -2), setting);
    },
  } },
};
const DEFAULT_CDP_TIMEOUT_MS = 20000;
async function attachBtapDebugger() {
  if (attachFailure) throw new Error(attachFailure);
  return {};
}
async function detachBtapDebugger() {}
async function sendDebuggerCommandWithTimeout() {
  attempts.push({ debugger: true });
  if (debuggerFailure) throw new Error(debuggerFailure);
}
__SOURCE__
function expiredLease(kind = 'content', origin = 'https://lease.test') {
  const permission = kind === 'clipboard' ? 'clipboard' : 'camera';
  return {
    id: permissionLeaseId(origin, permission), origin, permission, kind,
    contentSetting: kind === 'content' ? 'camera' : undefined,
    previousSetting: 'ask', tabId: 42, expiresAt: now - 1,
    alarmName: `btap-permission:${origin}:test`, state: 'active',
  };
}
(async () => {
  __SCENARIO__
})().catch(error => { console.error(error); process.exit(1); });
""".replace("__SOURCE__", source[start:end]).replace("__SCENARIO__", scenario)
    )


@pytest.mark.parametrize("kind", ["content", "content_unsupported", "clipboard"])
def test_unsupported_permission_recovery_stops_automatic_retries(kind):
    result = _permission_scenario(
        r"""
const kind = __KIND__;
const lease = expiredLease(kind === 'clipboard' ? kind : 'content');
saved.btapPermissionLeases = [lease];
effective.set(lease.origin, 'allow');
if (kind === 'content') delete chrome.contentSettings.camera;
else if (kind === 'content_unsupported') permissionFailure = 'This content setting is not supported';
else debuggerFailure = "'Browser.setPermission' wasn't found";
const first = await restoreExpiredPermissionLeases();
const attemptsAfterFirst = attempts.length;
for (let index = 0; index < 30; index += 1) {
  now += 60000;
  await restoreExpiredPermissionLeases();
}
process.stdout.write(JSON.stringify({
  first, attemptsAfterFirst, attempts: attempts.length,
  leases: saved.btapPermissionLeases, alarms: [...alarms.keys()],
  effective: effective.get(lease.origin),
}));
""".replace("__KIND__", json.dumps(kind))
    )
    assert result["first"]["ok"] is False
    assert result["first"]["manual_recovery_required"] is True
    assert result["first"]["recovery_pending"] is False
    assert result["attempts"] == result["attemptsAfterFirst"]
    assert result["alarms"] == []
    assert result["effective"] == "allow"
    assert len(result["leases"]) == 1
    lease = result["leases"][0]
    assert lease["state"] == "manual_recovery"
    assert lease["previousSetting"] == "ask"
    failure = result["first"]["failures"][0]
    assert failure["origin"] == lease["origin"]
    assert failure["previous_setting"] == "ask"
    assert failure["recovery_instruction"]


@pytest.mark.parametrize("phase", ["attach", "command", "content"])
def test_transient_permission_failures_still_retry_and_restore(phase):
    result = _permission_scenario(
        r"""
const phase = __PHASE__;
const lease = expiredLease(phase === 'content' ? 'content' : 'clipboard');
saved.btapPermissionLeases = [lease];
if (phase === 'attach') attachFailure = 'debugger temporarily unavailable';
if (phase === 'command') debuggerFailure = 'CDP command timed out';
if (phase === 'content') permissionFailure = 'temporarily unavailable';
const first = await restoreExpiredPermissionLeases();
const retryScheduled = alarms.has(permissionRetryAlarmName(lease));
attachFailure = debuggerFailure = permissionFailure = '';
const second = await restoreExpiredPermissionLeases();
process.stdout.write(JSON.stringify({
  first, second, retryScheduled, leases: saved.btapPermissionLeases,
  alarms: [...alarms.keys()],
}));
""".replace("__PHASE__", json.dumps(phase))
    )
    assert result["first"]["recovery_pending"] is True
    assert result["first"].get("manual_recovery_required", False) is False
    assert result["retryScheduled"] is True
    assert result["second"]["ok"] is True
    assert result["leases"] == []
    assert result["alarms"] == []


def test_explicit_permission_reset_can_retry_manual_recovery_after_support_returns():
    result = _permission_scenario(
        """
const lease = expiredLease();
saved.btapPermissionLeases = [lease];
const camera = chrome.contentSettings.camera;
delete chrome.contentSettings.camera;
await restoreExpiredPermissionLeases();
chrome.contentSettings.camera = camera;
const automatic = await restoreExpiredPermissionLeases();
const retained = structuredClone(saved.btapPermissionLeases);
const reset = await resetSitePermissionLeases({ origin: lease.origin, permission: 'camera' });
process.stdout.write(JSON.stringify({ automatic, retained, reset,
  effective: effective.get(lease.origin), leases: saved.btapPermissionLeases }));
"""
    )
    assert result["automatic"]["manual_recovery_required"] is True
    assert len(result["retained"]) == 1
    assert result["reset"]["ok"] is True
    assert result["effective"] == "ask"
    assert result["leases"] == []


def test_manual_permission_recovery_cannot_replace_or_lose_prior_state():
    result = _permission_scenario(
        """
const lease = expiredLease();
saved.btapPermissionLeases = [lease];
const camera = chrome.contentSettings.camera;
delete chrome.contentSettings.camera;
storageUnavailable = true;
const failedSave = await restoreExpiredPermissionLeases();
const retainedAfterFailure = structuredClone(saved.btapPermissionLeases);
storageUnavailable = false;
const recoveredSave = await restoreExpiredPermissionLeases();
chrome.contentSettings.camera = camera;
const replace = await setSitePermission({
  origin: 'https://new.test', permission: 'camera', setting: 'allow', durationSeconds: 60,
});
process.stdout.write(JSON.stringify({ failedSave, retainedAfterFailure, recoveredSave,
  replace, leases: saved.btapPermissionLeases, alarms: [...alarms.keys()] }));
"""
    )
    assert result["failedSave"]["recovery_pending"] is True
    assert result["retainedAfterFailure"][0]["previousSetting"] == "ask"
    assert result["recoveredSave"]["manual_recovery_required"] is True
    assert result["replace"]["ok"] is False
    assert result["replace"]["manual_recovery_required"] is True
    assert result["replace"]["recovery_pending"] is False
    assert len(result["leases"]) == 1
    assert result["leases"][0]["origin"] == "https://lease.test"
    assert result["alarms"] == []


def _create_scenario(scenario: str) -> dict:
    return _run_node_script(
        r"""
const logs = [];
console.log = (...args) => logs.push(args.join(' '));
let now = 2000000000;
Date.now = () => now;
let createCalls = 0;
let writeCalls = 0;
let storageUnavailable = false;
let loseNextWriteAck = false;
const stored = { btapCreateOperationsV1: {} };
const chrome = {
  storage: { session: {
    get: async keys => {
      const names = Array.isArray(keys) ? keys : [keys];
      return structuredClone(Object.fromEntries(names.map(key => [key, stored[key]])));
    },
    set: async value => {
      writeCalls += 1;
      if (storageUnavailable) throw new Error('session write unavailable');
      Object.assign(stored, structuredClone(value));
      if (loseNextWriteAck) {
        loseNextWriteAck = false;
        throw new Error('storage reply was lost after committing');
      }
    },
  } },
  tabs: { create: async ({ url }) => {
    createCalls += 1;
    return { id: createCalls + 1000, url, status: 'complete', title: '', windowId: 1 };
  } },
};
function worker() {
  return new Function('chrome', 'getClientId', 'tabGenerationFor',
    'scheduleNewTabGeneration', 'sendTabsUpdate',
    __SOURCE__ + '\nreturn { createTabAck, createTabStatus, loadCreateOperations, '
      + 'recordCount: () => createOperations.size };'
  )(chrome, async () => 'chrome:test', async id => `generation-${id}`,
    async id => `generation-${id}`, async () => {});
}
function orphan(id, age = 0) {
  return { operation_id: id, status: 'pending', url: 'https://same.test',
    client_id: 'chrome:test', created_at: now - age, tab_status: 'pending' };
}
(async () => {
  __SCENARIO__
})().catch(error => { console.error(error); process.exit(1); });
""".replace("__SOURCE__", json.dumps(_create_operation_source())).replace(
            "__SCENARIO__", scenario
        )
    )


def test_orphan_create_record_is_terminal_after_worker_eviction():
    result = _create_scenario(
        """
stored.btapCreateOperationsV1.orphan = orphan('orphan');
const firstWorker = worker();
const status = await firstWorker.createTabStatus({ operation_id: 'orphan' });
const duplicate = await firstWorker.createTabAck({ operation_id: 'orphan', url: 'https://same.test' });
const restarted = await worker().createTabStatus({ operation_id: 'orphan' });
process.stdout.write(JSON.stringify({ status, duplicate, restarted, createCalls, stored }));
"""
    )
    for key in ["status", "duplicate", "restarted"]:
        info = result[key]["data"]
        assert info["operation_status"] == "unknown"
        assert info["may_have_created"] is True
        assert info["retry_safe"] is False
        assert info["resume_required"] is False
        assert info["error"]
    assert result["createCalls"] == 0


@pytest.mark.parametrize("reason", ["ttl", "capacity"])
def test_orphan_create_records_are_bounded_without_replaying_evicted_ids(reason):
    result = _create_scenario(
        r"""
const reason = __REASON__;
const count = reason === 'ttl' ? 1 : 300;
for (let index = 0; index < count; index += 1) {
  const id = `orphan-${index}`;
  stored.btapCreateOperationsV1[id] = orphan(id, reason === 'ttl' ? 7 * 86400000 : index);
}
const firstWorker = worker();
const status = await firstWorker.createTabStatus({ operation_id: 'orphan-0' });
const countAfterLoad = firstWorker.recordCount();
const retained = new Set(Object.keys(stored.btapCreateOperationsV1));
const retired = Array.from({ length: count }, (_, index) => `orphan-${index}`)
  .filter(id => !retained.has(id));
const restarted = worker();
const replies = [];
for (const id of retired) replies.push(await restarted.createTabAck({ operation_id: id }));
const fresh = await restarted.createTabAck({ operation_id: 'fresh-operation' });
process.stdout.write(JSON.stringify({ status, countAfterLoad, retired, replies, fresh,
  createCalls, finalCount: restarted.recordCount(), storedBytes: JSON.stringify(stored).length }));
""".replace("__REASON__", json.dumps(reason))
    )
    assert result["countAfterLoad"] <= 256
    assert result["retired"]
    if reason == "ttl":
        assert result["countAfterLoad"] == 0
    for response in result["replies"]:
        info = response["data"]
        assert info["operation_status"] == "unknown"
        assert info["retry_safe"] is False
        assert info["may_have_created"] is True
        assert info["resume_required"] is False
        assert info["replay_guard"]["match"] is True
    assert result["fresh"]["data"]["operation_status"] == "completed"
    assert result["createCalls"] == 1
    assert result["finalCount"] <= 256
    assert result["storedBytes"] < 500000


def test_failed_orphan_compaction_keeps_durable_evidence_and_blocks_new_creates():
    result = _create_scenario(
        """
for (let index = 0; index < 300; index += 1) {
  const id = `orphan-${index}`;
  stored.btapCreateOperationsV1[id] = orphan(id, 7 * 86400000);
}
const before = JSON.stringify(stored);
storageUnavailable = true;
const current = worker();
const failed = [];
for (let index = 0; index < 10; index += 1) {
  failed.push(await current.createTabAck({ operation_id: `new-${index}` }));
}
const unchanged = JSON.stringify(stored) === before;
const duringCount = current.recordCount();
storageUnavailable = false;
const recovered = await current.createTabAck({ operation_id: 'new-after-recovery' });
const duplicate = await worker().createTabAck({ operation_id: 'orphan-0' });
process.stdout.write(JSON.stringify({ failed, unchanged, duringCount, recovered, duplicate,
  createCalls, finalCount: current.recordCount() }));
"""
    )
    assert result["unchanged"] is True
    assert result["duringCount"] <= 300
    assert all(reply["data"]["retry_safe"] is False for reply in result["failed"])
    assert result["createCalls"] == 1
    assert result["recovered"]["data"]["operation_status"] == "completed"
    assert result["duplicate"]["data"]["operation_status"] == "unknown"
    assert result["finalCount"] <= 256


@pytest.mark.parametrize("saturated", [False, True])
def test_replay_filter_false_positives_and_saturation_are_explicit_and_fail_closed(saturated):
    result = _create_scenario(
        r"""
const saturated = __SATURATED__;
stored.btapCreateReplayFilterV1 = { version: 1, bitmap: (saturated ? 'ff' : 'fe').repeat(65536) };
stored.btapCreateOperationsV1.completed = {
  operation_id: 'completed', status: 'completed', created_at: now,
  id: 4242, generation: 'original-generation', tab_status: 'complete',
};
const current = worker();
let match = null;
for (let index = 0; index < 100; index += 1) {
  const id = `fresh-${index}`;
  const status = await current.createTabStatus({ operation_id: id });
  if (status.data.replay_guard?.match) { match = status; break; }
}
if (!match) throw new Error('fixture did not produce a replay-filter false positive');
const refused = await current.createTabAck({ operation_id: match.data.operation_id });
const existing = await current.createTabAck({ operation_id: 'completed' });
process.stdout.write(JSON.stringify({ match, refused, existing, createCalls }));
""".replace("__SATURATED__", json.dumps(saturated))
    )
    for reply in [result["match"], result["refused"]]:
        info = reply["data"]
        assert info["operation_status"] == "unknown"
        assert info["retry_safe"] is False
        assert info["resume_required"] is False
        assert info["replay_guard"]["match"] is True
        assert info["replay_guard"]["saturated"] is saturated
        assert 0 < info["replay_guard"]["bits_set"] <= info["replay_guard"]["total_bits"]
        assert info["error"]
    assert result["existing"]["data"]["operation_status"] == "completed"
    assert result["existing"]["data"]["generation"] == "original-generation"
    assert result["createCalls"] == 0


def test_corrupt_replay_filter_is_not_treated_as_an_empty_history():
    result = _create_scenario(
        """
stored.btapCreateReplayFilterV1 = { version: 1, bitmap: 'invalid' };
const before = JSON.stringify(stored);
const current = worker();
const status = await current.createTabStatus({ operation_id: 'fresh' });
const create = await current.createTabAck({ operation_id: 'fresh' });
process.stdout.write(JSON.stringify({ status, create, createCalls, unchanged: before === JSON.stringify(stored) }));
"""
    )
    for reply in [result["status"], result["create"]]:
        assert reply["data"]["operation_status"] == "unknown"
        assert reply["data"]["retry_safe"] is False
        assert "replay protection is unreadable" in reply["data"]["error"]
    assert result["createCalls"] == 0


@pytest.mark.parametrize("corrupt", [
    None, False, 7, "unreadable", [], [{}], {"orphan": None},
    {"orphan": {"status": "unexpected"}},
    {"orphan": {"status": "pending", "created_at": "unknown"}},
])
def test_corrupt_create_registry_cannot_be_read_as_no_prior_operation(corrupt):
    result = _create_scenario(
        """
stored.btapCreateOperationsV1 = __CORRUPT__;
const before = JSON.stringify(stored);
const current = worker();
const status = await current.createTabStatus({ operation_id: 'orphan' });
const create = await current.createTabAck({ operation_id: 'orphan' });
process.stdout.write(JSON.stringify({status, create, createCalls,
  unchanged:before === JSON.stringify(stored)}));
""".replace("__CORRUPT__", json.dumps(corrupt))
    )
    for reply in (result["status"], result["create"]):
        assert reply["data"]["operation_status"] == "unknown"
        assert reply["data"]["retry_safe"] is False
        assert "unreadable" in reply["data"]["error"]
    assert result["createCalls"] == 0
    assert result["unchanged"] is True
    assert result["unchanged"] is True


def test_completed_and_not_created_receipts_keep_their_existing_expiry_policy():
    result = _create_scenario(
        """
for (const status of ['completed', 'not_found']) {
  stored.btapCreateOperationsV1[status] = {
    operation_id: status, status, created_at: now - 7 * 86400000,
    id: status === 'completed' ? 99 : null, generation: 'old-generation',
  };
}
const current = worker();
const statuses = await Promise.all(['completed', 'not_found'].map(operation_id =>
  current.createTabStatus({ operation_id })));
const fresh = await current.createTabAck({ operation_id: 'fresh' });
process.stdout.write(JSON.stringify({ statuses, fresh, createCalls,
  replayFilterCreated: Object.hasOwn(stored, 'btapCreateReplayFilterV1') }));
"""
    )
    for reply in result["statuses"]:
        assert reply["data"]["operation_status"] == "not_found"
        assert reply["data"]["retry_safe"] is True
    assert result["fresh"]["data"]["operation_status"] == "completed"
    assert result["createCalls"] == 1
    assert result["replayFilterCreated"] is False


def test_lost_compaction_write_ack_recovers_without_replaying_the_original_create():
    result = _create_scenario(
        """
stored.btapCreateOperationsV1.orphan = orphan('orphan', 7 * 86400000);
loseNextWriteAck = true;
const current = worker();
const first = await current.createTabStatus({ operation_id: 'orphan' });
const retry = await current.createTabAck({ operation_id: 'orphan' });
const restarted = await worker().createTabAck({ operation_id: 'orphan' });
const fresh = await current.createTabAck({ operation_id: 'fresh' });
process.stdout.write(JSON.stringify({ first, retry, restarted, fresh, createCalls }));
"""
    )
    assert result["first"]["data"]["operation_status"] == "unknown"
    for key in ["retry", "restarted"]:
        assert result[key]["data"]["operation_status"] == "unknown"
        assert result[key]["data"]["replay_guard"]["match"] is True
        assert result[key]["data"]["retry_safe"] is False
    assert result["fresh"]["data"]["operation_status"] == "completed"
    assert result["createCalls"] == 1


def test_create_capacity_refuses_new_work_while_all_retained_operations_are_active():
    result = _create_scenario(
        """
let release;
const gate = new Promise(resolve => { release = resolve; });
chrome.tabs.create = async ({ url }) => {
  const id = ++createCalls;
  await gate;
  return { id, url, status: 'complete', windowId: 1 };
};
const current = worker();
const settled = [];
const pending = Array.from({ length: 300 }, (_, index) =>
  current.createTabAck({ operation_id: `active-${index}` }).then(reply => {
    settled.push(reply); return reply;
  }));
for (let spin = 0; settled.length < 44 && spin < 2000; spin += 1) {
  await new Promise(resolve => setTimeout(resolve, 1));
}
const beforeRelease = {
  createCalls, recordCount: current.recordCount(),
  savedCount: Object.keys(stored.btapCreateOperationsV1).length, refused: settled.length,
};
release();
const replies = await Promise.all(pending);
process.stdout.write(JSON.stringify({ beforeRelease, createCalls,
  finalCount: current.recordCount(),
  completed: replies.filter(reply => reply.data.operation_status === 'completed').length,
  refused: replies.filter(reply => reply.data.capacity_exhausted === true).length,
}));
"""
    )
    assert result["beforeRelease"] == {
        "createCalls": 256, "recordCount": 256, "savedCount": 256, "refused": 44,
    }
    assert result["completed"] == result["createCalls"] == 256
    assert result["refused"] == 44
    assert result["finalCount"] <= 256


@pytest.mark.parametrize("outage_half", ["storage", "tabs"])
def test_removed_and_reused_native_id_keeps_its_new_generation_after_outage(outage_half):
    result = _run_generation_harness(
        r"""
const stored = { btapTabGenerationsV1: { '42': 'generation-old', '7': 'generation-other' } };
let outage = true;
const outageHalf = __HALF__;
let live = [{ id: 42 }, { id: 7 }];
const writes = [];
const chrome = {
  storage: { session: {
    get: async key => {
      if (outage && outageHalf === 'storage') throw new Error('storage unavailable');
      return structuredClone({ [key]: stored[key] });
    },
    set: async value => { writes.push(structuredClone(value)); Object.assign(stored, value); },
  } },
  tabs: { query: async () => {
    if (outage && outageHalf === 'tabs') throw new Error('tab query unavailable');
    return live;
  } },
};
__SOURCE__
(async () => {
  live = [{ id: 7 }];
  await forgetTabGeneration(42);
  live = [{ id: 42 }, { id: 7 }];
  const newGeneration = await tabGenerationFor(42);
  const beforeRecovery = structuredClone(stored);
  outage = false;
  const recovered = await tabGenerationFor(42);
  const other = await tabGenerationFor(7);
  process.stdout.write(JSON.stringify({ newGeneration, recovered, other, beforeRecovery, stored, writes }));
})().catch(error => { console.error(error); process.exit(1); });
""".replace("__HALF__", json.dumps(outage_half))
    )
    assert result["newGeneration"] != "generation-old"
    assert result["recovered"] == result["newGeneration"]
    assert result["other"] == "generation-other"
    assert result["beforeRecovery"]["btapTabGenerationsV1"]["42"] == "generation-old"
    assert result["stored"]["btapTabGenerationsV1"]["42"] == result["newGeneration"]


def test_scheduled_new_lifetime_is_not_replaced_by_its_retired_durable_generation():
    result = _run_generation_harness(
        """
const stored = { btapTabGenerationsV1: { '42': 'generation-old' } };
let outage = true;
const chrome = {
  storage: { session: {
    get: async key => {
      if (outage) throw new Error('storage unavailable');
      return { [key]: stored[key] };
    },
    set: async value => Object.assign(stored, value),
  } },
  tabs: { query: async () => [{ id: 42 }] },
};
__SOURCE__
(async () => {
  const created = await scheduleNewTabGeneration(42);
  outage = false;
  const recovered = await tabGenerationFor(42);
  process.stdout.write(JSON.stringify({ created, recovered, stored }));
})().catch(error => { console.error(error); process.exit(1); });
"""
    )
    assert result["created"] != "generation-old"
    assert result["recovered"] == result["created"]
    assert result["stored"]["btapTabGenerationsV1"]["42"] == result["created"]


@pytest.mark.parametrize("old_operation", ["lookup", "assignment"])
def test_remove_and_reuse_wait_for_inflight_generation_work_without_resurrecting_old_lifetime(
    old_operation,
):
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("async function validateTabCloseGenerations")
    end = source.index("async function closeTabsWithGenerations", start)
    result = _run_generation_harness(
        r"""
const stored = { btapTabGenerationsV1: { '42': 'generation-old', '7': 'generation-other' } };
let outage = true;
let reads = 0;
let release;
const gate = new Promise(resolve => { release = resolve; });
const chrome = {
  storage: { session: {
    get: async key => {
      if (++reads === 1) await gate;
      if (outage) throw new Error('storage unavailable');
      return structuredClone({ [key]: stored[key] });
    },
    set: async value => Object.assign(stored, structuredClone(value)),
  } },
  tabs: { query: async () => [{ id: 42 }, { id: 7 }] },
};
__SOURCE__
__VALIDATE__
(async () => {
  const original = __MODE__ === 'assignment' ? scheduleNewTabGeneration(42) : tabGenerationFor(42);
  while (!reads) await Promise.resolve();
  const removed = forgetTabGeneration(42);
  const created = scheduleNewTabGeneration(42);
  release();
  const oldGeneration = await original;
  await removed;
  const newGeneration = await created;
  outage = false;
  const recovered = await tabGenerationFor(42);
  const oldRefusal = await validateTabCloseGenerations([42], { '42': oldGeneration });
  const newRefusal = await validateTabCloseGenerations([42], { '42': newGeneration });
  process.stdout.write(JSON.stringify({ oldGeneration, newGeneration, recovered,
    oldRefusal, newRefusal, stored }));
})().catch(error => { console.error(error); process.exit(1); });
""".replace("__MODE__", json.dumps(old_operation)).replace("__VALIDATE__", source[start:end])
    )
    assert result["oldGeneration"] != result["newGeneration"]
    assert result["newGeneration"] == result["recovered"]
    assert "refusing close" in result["oldRefusal"]
    assert result["newRefusal"] is None
    assert result["stored"]["btapTabGenerationsV1"] == {
        "7": "generation-other", "42": result["newGeneration"],
    }


def test_browser_replacement_preserves_the_transferred_generation_during_storage_outage():
    result = _run_generation_harness(
        """
const stored = {
  btapTabGenerationsV1: { '42': 'owned-generation', '43': 'retired-generation' },
  btapTabIdentitiesV1: { '42': 'owned-identity', '43': 'retired-identity' },
};
let outage = true;
const chrome = {
  storage: { session: {
    get: async key => {
      if (outage) throw new Error('storage unavailable');
      return structuredClone({ [key]: stored[key] });
    },
    set: async value => Object.assign(stored, structuredClone(value)),
  } },
  tabs: { query: async () => [{ id: 43 }] },
};
__SOURCE__
(async () => {
  // onReplaced is direct browser evidence of one logical lifetime, even when
  // its destination native handle used to belong to a different tab.
  tabGenerations.set(42, 'owned-generation');
  tabIdentities.set('42', 'owned-identity');
  await transferTabIdentity(43, 42);
  const transferred = tabGenerations.get(43);
  outage = false;
  const recovered = await tabGenerationFor(43);
  process.stdout.write(JSON.stringify({ transferred, recovered, stored }));
})().catch(error => { console.error(error); process.exit(1); });
"""
    )
    assert result["transferred"] == result["recovered"] == "owned-generation"
    assert result["stored"]["btapTabGenerationsV1"] == {"43": "owned-generation"}


def test_generation_retirement_evidence_does_not_accumulate_after_a_successful_load():
    result = _run_generation_harness(
        """
const stored = { btapTabGenerationsV1: { '7': 'generation-seven' } };
const chrome = {
  storage: { session: {
    get: async key => ({ [key]: stored[key] }),
    set: async value => Object.assign(stored, value),
  } },
  tabs: { query: async () => [{ id: 7 }] },
};
__SOURCE__
(async () => {
  await tabGenerationFor(7);
  for (let id = 100; id < 400; id += 1) {
    await scheduleNewTabGeneration(id);
    await forgetTabGeneration(id);
  }
  process.stdout.write(JSON.stringify({ retired: retiredTabGenerations.size,
    pending: tabGenerationAssignments.size + tabGenerationRemovals.size, stored }));
})().catch(error => { console.error(error); process.exit(1); });
"""
    )
    assert result["retired"] == result["pending"] == 0
    assert result["stored"]["btapTabGenerationsV1"] == {"7": "generation-seven"}
