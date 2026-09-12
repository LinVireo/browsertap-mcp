// Loaded by the worker, not by ordinary pages. The complete function is sent
// only to the target tab for explicit accept/dismiss commands. Keep it
// self-contained: both scripting injection and the CDP builder serialize it.
function manageDialogScope(action, scope) {
  const key = '__btap_dialog_controller';
  const slot = Object.getOwnPropertyDescriptor(window, key);
  const current = slot?.value;
  const isController = current?.version === 1 &&
    typeof current.enter === 'function' && typeof current.releaseToken === 'function';
  if (action === 'release') {
    if (isController) current.releaseToken(String(scope?.token));
    return null;
  }
  const installing = action === 'install';
  if ((!installing && action !== 'enter') || !scope?.token ||
      (scope.policy !== 'accept' && scope.policy !== 'dismiss') ||
      !Number.isFinite(scope.deadline) || scope.deadline <= Date.now() ||
      (Number.isFinite(scope.startDeadline) && scope.startDeadline <= Date.now())) return null;
  // InjectionResult must contain plain data, not the lease's closure functions.
  // The top builder adopts its prepared entry; subframes keep theirs until the
  // worker releases the token or the original absolute deadline expires.
  const resultFor = lease => installing ? {
    __btap_dialog_scope_installed: true,
    token: String(scope.token),
    policy: scope.policy,
    deadline: scope.deadline,
    top: window.top === window,
  } : lease;
  if (slot) {
    if (!isController) throw new Error('dialog scope controller conflicts with a page property');
    return resultFor(current.enter(scope, installing));
  }

  // This slot exists only while a scope is active. It coordinates overlapping
  // executions in one frame; records and original descriptors stay in closures.
  // MAIN world is page-controlled, so this is not an anti-tampering boundary.
  const names = ['alert', 'confirm', 'prompt'];
  const originals = Object.fromEntries(names.map(name => [name, {
    descriptor: Object.getOwnPropertyDescriptor(window, name),
    value: window[name],
  }]));
  const wrappers = {};
  let scopes = [];
  let expiryTimer = null;
  let restored = false;
  const armTimer = setTimeout.bind(window);
  const cancelTimer = clearTimeout.bind(window);
  const _log = console.log.bind(console);

  function restore() {
    if (restored) return;
    restored = true;
    if (expiryTimer !== null) cancelTimer(expiryTimer);
    expiryTimer = null;
    for (const name of names) {
      // A page may replace a function during automation. Restore only our own
      // wrapper, never overwrite that later page change with an old snapshot.
      if (Object.getOwnPropertyDescriptor(window, name)?.value !== wrappers[name]) continue;
      try {
        const descriptor = originals[name].descriptor;
        if (descriptor) Object.defineProperty(window, name, descriptor);
        else delete window[name];
      } catch (_) { /* a page can make its own property non-configurable */ }
    }
    if (Object.getOwnPropertyDescriptor(window, key)?.value === controller) {
      try { delete window[key]; } catch (_) {}
    }
  }

  function refresh() {
    const now = Date.now();
    scopes = scopes.filter(entry => now < entry.deadline);
    if (expiryTimer !== null) cancelTimer(expiryTimer);
    expiryTimer = null;
    if (!scopes.length) {
      restore();
      return;
    }
    expiryTimer = armTimer(refresh, Math.max(1, Math.min(...scopes.map(entry => entry.deadline)) - now));
  }

  const controller = {
    version: 1,
    enter(value, preparing = false) {
      const prepared = !preparing && scopes.find(entry => entry.prepared &&
        entry.token === String(value.token) && entry.policy === value.policy &&
        entry.deadline === value.deadline);
      const entry = prepared || {
        token: String(value.token), policy: value.policy, deadline: value.deadline,
        records: [], prepared: preparing,
      };
      if (prepared) entry.prepared = false;
      else scopes.push(entry);
      refresh();
      return {
        records: () => entry.records.slice(),
        release() {
          scopes = scopes.filter(candidate => candidate !== entry);
          refresh();
        },
      };
    },
    releaseToken(token) {
      scopes = scopes.filter(entry => entry.token !== token);
      refresh();
    },
  };

  function toast(type, msg) {
    _log('[BTAP] ' + type + ' suppressed:', msg);
    try {
      const d = document.createElement('div');
      d.textContent = '[' + type + '] ' + msg;
      Object.assign(d.style, {
        position:'fixed', top:'12px', right:'12px', zIndex:'2147483647',
        background:'#222', color:'#fff', padding:'10px 18px', borderRadius:'8px',
        fontSize:'14px', maxWidth:'420px', wordBreak:'break-all',
        boxShadow:'0 4px 16px rgba(0,0,0,.3)', opacity:'1',
        transition:'opacity .5s', pointerEvents:'none'
      });
      (document.body || document.documentElement).appendChild(d);
      armTimer(() => { d.style.opacity = '0'; }, 3000);
      armTimer(() => { d.remove(); }, 3600);
    } catch (_) {}
  }

  function activeScope() {
    // Background tabs can throttle timers. Never answer a dialog after its
    // deadline merely because the cleanup timer has not had a turn yet.
    refresh();
    return scopes[scopes.length - 1] || null;
  }

  function recordDialog(entry, type, msg, defaultPrompt) {
    entry.records.push({
      token: entry.token,
      policy: entry.policy,
      type,
      message: String(msg ?? ''),
      defaultPrompt: defaultPrompt === undefined ? '' : String(defaultPrompt),
      openedAt: Date.now(),
    });
    entry.records = entry.records.slice(-50);
  }

  wrappers.alert = function(msg) {
    const scope = activeScope();
    if (!scope) return originals.alert.value.call(window, msg);
    recordDialog(scope, 'alert', msg);
    toast('alert', msg);
  };
  wrappers.confirm = function(msg) {
    const scope = activeScope();
    if (!scope) return originals.confirm.value.call(window, msg);
    recordDialog(scope, 'confirm', msg);
    toast('confirm', msg);
    return scope.policy === 'accept';
  };
  wrappers.prompt = function(msg, def) {
    const scope = activeScope();
    if (!scope) return originals.prompt.value.call(window, msg, def);
    recordDialog(scope, 'prompt', msg, def);
    toast('prompt', msg);
    return scope.policy === 'accept' ? (def ?? '') : null;
  };
  try {
    Object.defineProperty(window, key, { value: controller, configurable: true });
    for (const name of names) {
      const descriptor = originals[name].descriptor;
      Object.defineProperty(window, name, descriptor && 'value' in descriptor
        ? { ...descriptor, value: wrappers[name] }
        : { value: wrappers[name], writable: true, configurable: true,
            enumerable: descriptor?.enumerable ?? true });
    }
    return resultFor(controller.enter(scope, installing));
  } catch (error) {
    restore();
    throw error;
  }
}

manageDialogScope;
