;(function(){

// NOTE: this script used to strip <meta http-equiv="Content-Security-Policy">
// from every page it loaded into, which weakened the page's own protections
// during ordinary browsing, not just under automation. CSP is now relaxed
// per-tab and only for the duration of a command (see withCspOff in
// background.js), and eval-blocking pages fall back to the CDP path, so the
// unconditional meta removal is no longer needed.

// Indicator badge reflects real WS status. The background owns reconnect
// scheduling and pushes status here, so this page context needs no timers.
(function(){
  if(window.self!==window.top)return;
  // Drop any badge left by an orphaned older injection so we don't double up.
  const stale = document.getElementById('btap-indicator');
  if (stale) stale.remove();
  const d=document.createElement('div');
  d.id='btap-indicator';
  const message = (name, fallback) => {
    try { return chrome.i18n.getMessage(name) || fallback; } catch (_) { return fallback; }
  };
  // Resolve i18n strings once at injection time. paint() runs later from
  // async callbacks, where a reloaded extension leaves this orphaned context
  // without a usable chrome.i18n — so it must not touch chrome APIs at all.
  const TEXT_CONNECTED = message('indicatorConnected', 'BTAP: connected');
  const TEXT_DISCONNECTED = message('indicatorDisconnected', 'BTAP: disconnected');
  d.innerText=message('indicatorChecking', 'BTAP: checking');
  d.style.cssText='position:fixed;bottom:8px;right:8px;background:#888;color:white;padding:4px 7px;border-radius:4px;font-size:11px;font-weight:bold;z-index:99999;box-shadow:0 2px 4px rgba(0,0,0,0.2);opacity:0.85;';
  function paint(ok){
    try {
      if (document.getElementById('btap-indicator') !== d) return;
      d.innerText = ok ? TEXT_CONNECTED : TEXT_DISCONNECTED;
      d.style.background = ok ? '#4CAF50' : '#e67e22';
    } catch (_) {}
  }
  (document.body||document.documentElement).appendChild(d);
  function applyVisibility(value){ d.hidden = value === false; }
  try {
    // This callback can run after an extension reload has orphaned this
    // context, so it must not touch chrome.* at all — any access there throws
    // "Extension context invalidated". That rules out reading
    // chrome.runtime.lastError, whose only cost is a possible unchecked-error
    // warning in the rare case storage itself fails; a failed read yields
    // undefined, which leaves the badge at its default visible state.
    const applyStored = (stored) => {
      if (document.getElementById('btap-indicator') !== d) return;
      // Read both key names: the popup migrates the pre-BTAP preference, but a
      // page can load before that ever happens. `undefined` from either key
      // leaves the badge visible, which is the default either way.
      if (!stored) return applyVisibility(undefined);
      applyVisibility(stored.btap_indicator_visible !== undefined
        ? stored.btap_indicator_visible
        : stored.tmwd_indicator_visible);
    };
    // Chrome answers through the callback; a promise-returning host is also
    // handled so neither calling convention is left unread.
    const pendingRead = chrome.storage.local.get(
      ['btap_indicator_visible', 'tmwd_indicator_visible'], applyStored,
    );
    if (pendingRead && typeof pendingRead.then === 'function') {
      pendingRead.then(applyStored, () => {});
    }
    chrome.storage.onChanged.addListener((changes, areaName)=>{
      if (document.getElementById('btap-indicator') !== d) return;
      if (areaName !== 'local') return;
      const change = changes.btap_indicator_visible || changes.tmwd_indicator_visible;
      if (change) applyVisibility(change.newValue);
    });
  } catch(_) { /* orphaned contexts keep the default visible state */ }
  const receiveStatus = (status)=>{
    if (status && status.type === 'btap_status') paint(Boolean(status.ws));
  };
  try {
    chrome.runtime.onMessage.addListener((msg)=>{ receiveStatus(msg); });
    const port = chrome.runtime.connect({ name: 'btap_keepalive' });
    port.onMessage.addListener(receiveStatus);
    port.postMessage({ type: 'btap_status_request' });
  } catch(_) { paint(false); }
})();

})();
