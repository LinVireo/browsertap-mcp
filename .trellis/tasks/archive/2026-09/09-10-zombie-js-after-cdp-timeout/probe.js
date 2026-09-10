const ZOMBIE_PROBE_BUDGET_MS = 6000;
const ZOMBIE_SENTINEL_TIMEOUT_MS = 1500;

// Runs on the attachment whose evaluate just timed out, *before* that
// attachment is invalidated. Measured 2026-09-10: a fresh attach to a renderer
// pinned by `while(true){}` succeeds browser-side, but its renderer session is
// created on the pinned main thread, so even Runtime.terminateExecution --
// normally handled off the main thread -- never got a reply (1500 ms, verdict
// still_running). The session that is already set up is the only one that can
// deliver the interrupt.
//
// Every command here is a raw sendCommand raced against a timer. Going through
// sendDebuggerCommandWithTimeout would invalidate this very attachment on the
// sentinel's expected timeout and take the kill path down with it.
async function settleZombieAfterTimeout(tabId, attachment) {
  const startedAt = Date.now();
  const remaining = () => ZOMBIE_PROBE_BUDGET_MS - (Date.now() - startedAt);
  const verdict = (zombie, zombie_detail) => ({ zombie, zombie_detail });
  if (!attachment?.target) {
    return verdict('unknown', 'no live attachment was available for the probe');
  }

  // 'responsive' | 'pinned' | 'failed'. A pinned command is left dangling on
  // purpose: the attachment is about to be torn down by the caller anyway.
  const raw = async (method, params) => {
    const budget = Math.min(ZOMBIE_SENTINEL_TIMEOUT_MS, Math.max(100, remaining()));
    let timer = null;
    const clock = new Promise(resolve => {
      timer = setTimeout(() => resolve({ state: 'pinned' }), budget);
    });
    const send = Promise.resolve()
      .then(() => chrome.debugger.sendCommand(attachment.target, method, params || {}))
      .then(() => ({ state: 'responsive' }), error => ({ state: 'failed', error }));
    try { return await Promise.race([send, clock]); } finally { clearTimeout(timer); }
  };
  const sentinel = () => raw('Runtime.evaluate', { expression: '1', returnByValue: true });

  const before = await sentinel();
  if (before.state === 'responsive') {
    return verdict('not_blocking',
      'the page answered a sentinel evaluate after the timeout; the script is finished'
      + ' or idle awaiting something asynchronous, and nothing was terminated');
  }
  if (before.state !== 'pinned') {
    return verdict('unknown', 'could not probe the page after the timeout'
      + ` (${before.error?.message || before.error})`);
  }

  // The sentinel could not run: the main thread is pinned by a script, which is
  // exactly the precondition terminateExecution needs.
  //
  // One pinned-thread cause is not a zombie: a native dialog. alert() blocks the
  // renderer inside whichever script called it -- possibly the page's own,
  // opened before the caller's evaluate ever started -- and terminating that
  // script is not what the caller asked for. The dialog tools own that case.
  const dialog = currentProtocolDialog(tabId);
  if (dialog) {
    return verdict('blocked_by_dialog', `a native ${dialog.type || 'dialog'} is open on the tab`
      + ' and is what pins it; nothing was terminated -- settle it with handle_dialog first');
  }
  if (remaining() < ZOMBIE_SENTINEL_TIMEOUT_MS) {
    return verdict('still_running', 'the page is pinned by the script and the probe budget'
      + ' ran out before it could be terminated');
  }

  const kill = await raw('Runtime.terminateExecution', {});
  if (kill.state !== 'responsive') {
    return verdict('still_running', 'the page is pinned by the script and'
      + ' Runtime.terminateExecution '
      + (kill.state === 'pinned'
        ? 'did not answer in time'
        : `failed (${kill.error?.message || kill.error})`));
  }

  const after = await sentinel();
  if (after.state === 'responsive') {
    return verdict('killed', 'the page was pinned by the script; Runtime.terminateExecution'
      + ' freed it and a sentinel evaluate now answers');
  }
  if (after.state === 'pinned') {
    return verdict('still_running', 'Runtime.terminateExecution was acknowledged but a sentinel'
      + ' evaluate still cannot run; the page remains pinned');
  }
  return verdict('unknown', 'Runtime.terminateExecution was acknowledged but the page could'
    + ` not be re-probed (${after.error?.message || after.error})`);
}
