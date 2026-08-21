// Suppress alert/confirm/prompt so page JS can't block automation.
//
// This runs in the MAIN world on every page at document_start, so it MUST NOT
// change behaviour during ordinary human browsing. It used to replace confirm()
// with a function that always returned true — which silently auto-accepted the
// user's own "Delete this repository?" / "Discard unsaved changes?" dialogs.
//
// Now the natives stay in place until automation explicitly turns suppression
// on for this tab. The injected script preamble pushes a scope onto
// window.__btap_dialog_scopes (same MAIN world) carrying a policy and a
// deadline, and activeScope() below is the single reader of that list.
//
// Deadlines rather than a boolean, because a boolean stays stuck on if the
// script throws, the tab navigates mid-command, or the worker is evicted — and
// a stuck flag silently eats the user's own confirm() dialogs, which is the very
// bug this was meant to fix. Note the preamble also maintains a legacy
// window.__btap_suppress_until mirror; nothing reads it, so do not rely on it.
(function() {
  const _log = console.log.bind(console);
  const native = {
    alert: window.alert,
    confirm: window.confirm,
    prompt: window.prompt,
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
      setTimeout(() => { d.style.opacity = '0'; }, 3000);
      setTimeout(() => { d.remove(); }, 3600);
    } catch(e) {}
  }

  function activeScope() {
    const now = Date.now();
    const scopes = Array.isArray(window.__btap_dialog_scopes)
      ? window.__btap_dialog_scopes.filter(scope => scope && now < scope.deadline &&
          (scope.policy === 'dismiss' || scope.policy === 'accept'))
      : [];
    if (Array.isArray(window.__btap_dialog_scopes)) {
      window.__btap_dialog_scopes = scopes;
    }
    if (scopes.length) return scopes[scopes.length - 1];
    return null;
  }

  function recordDialog(scope, type, msg, defaultPrompt) {
    const records = Array.isArray(window.__btap_dialog_records)
      ? window.__btap_dialog_records : [];
    records.push({
      token: scope.token,
      policy: scope.policy,
      type,
      message: String(msg ?? ''),
      defaultPrompt: defaultPrompt === undefined ? '' : String(defaultPrompt),
      openedAt: Date.now(),
    });
    window.__btap_dialog_records = records.slice(-50);
  }

  // Note the asymmetry in the fallbacks: when NOT automating we defer to the
  // native dialog, so the user's own confirmations behave exactly as the page
  // intended. Only under automation do we answer on their behalf.
  window.alert = function(msg) {
    const scope = activeScope();
    if (!scope) return native.alert.call(window, msg);
    recordDialog(scope, 'alert', msg);
    toast('alert', msg);
  };
  window.confirm = function(msg) {
    const scope = activeScope();
    if (!scope) return native.confirm.call(window, msg);
    recordDialog(scope, 'confirm', msg);
    toast('confirm', msg);
    return scope.policy === 'accept';
  };
  window.prompt = function(msg, def) {
    const scope = activeScope();
    if (!scope) return native.prompt.call(window, msg, def);
    recordDialog(scope, 'prompt', msg, def);
    toast('prompt', msg);
    return scope.policy === 'accept' ? (def ?? '') : null;
  };
})();
