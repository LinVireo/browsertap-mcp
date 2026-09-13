document.addEventListener('DOMContentLoaded', () => {
  localizeDocument();
  document.getElementById('refresh').addEventListener('click', fetchCookies);
  document.getElementById('copy').addEventListener('click', copyCookies);
  document.getElementById('indicator-visible').addEventListener('change', saveIndicatorVisibility);
  loadIndicatorVisibility();
  // Nothing reads cookies on open. This used to call fetchCookies(), whose tail
  // wrote every cookie of the active tab to the clipboard -- so opening the popup
  // for the indicator checkbox above silently replaced the clipboard with session
  // credentials, HttpOnly ones included. Both halves are gestures now.
});

// The bridge port is deliberately NOT editable here. The Python side owns it
// (BROWSERTAP_BRIDGE_PORT), and a second source of truth in a popup any user
// can open silently breaks the bridge for the whole profile — the value would
// persist in chrome.storage and survive reinstalling the Python package.
// background.js still reads the stored port key, so the rare non-default-port
// setup stays possible as a one-liner in the extension's service-worker
// console; docs/TROUBLESHOOTING.md carries the exact command.

function message(name, substitutions) {
  return chrome.i18n.getMessage(name, substitutions) || name;
}

// What the last Refresh put on screen. The copy button works off this instead of
// re-reading, so what lands in the clipboard is exactly what the user is looking
// at -- a second read could return a different jar after a background refresh.
let renderedCookies = null;

function localizeDocument() {
  document.documentElement.lang = chrome.i18n.getUILanguage();
  document.querySelectorAll('[data-i18n]').forEach((element) => {
    element.textContent = message(element.dataset.i18n);
  });
}

async function loadIndicatorVisibility() {
  // Read the current and legacy keys together. The popup opens on the user's
  // click, so the extra storage round trip is visible on slower profiles; the
  // combined read preserves the same precedence and migration retry semantics.
  const stored = await chrome.storage.local.get([
    'btap_indicator_visible',
    'tmwd_indicator_visible',
  ]);
  let visible = stored.btap_indicator_visible;
  if (visible === undefined) {
    // Carry the pre-BTAP preference over once. The popup is the only place with
    // a healthy extension context guaranteed, so the migration lives here;
    // content.js just reads both keys.
    if (stored.tmwd_indicator_visible !== undefined) {
      visible = stored.tmwd_indicator_visible;
      try {
        await chrome.storage.local.set({ btap_indicator_visible: visible });
        await chrome.storage.local.remove?.('tmwd_indicator_visible');
      } catch (_) { /* retried the next time the popup opens */ }
    }
  }
  document.getElementById('indicator-visible').checked = visible !== false;
}

async function saveIndicatorVisibility() {
  const visible = document.getElementById('indicator-visible').checked;
  await chrome.storage.local.set({ btap_indicator_visible: visible });
}

async function fetchCookies() {
  const out = document.getElementById('out');
  // A refresh replaces the previous jar. Invalidate it before any query so a
  // failed read can never leave stale credentials available to Copy.
  renderedCookies = null;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url) { out.textContent = message('noActiveTab'); return; }
    // chrome://, edge://, file:// and the new-tab page have no cookie jar we can
    // read. Say so here rather than sending a request that can only come back
    // as an error the user cannot act on.
    if (!/^https?:\/\//i.test(tab.url)) {
      out.textContent = message('cookiesUnavailable');
      return;
    }
    const resp = await chrome.runtime.sendMessage({ cmd: 'cookies', url: tab.url });
    if (!resp?.ok) { out.textContent = message('errorPrefix') + (resp?.error || message('unknownError')); return; }
    // A successful envelope with a non-array payload means the background sent
    // something we cannot render; reading .length off it would throw.
    if (!Array.isArray(resp.data)) {
      out.textContent = message('errorPrefix') + message('unknownError');
      return;
    }
    if (!resp.data.length) { renderedCookies = null; out.textContent = message('noCookies'); return; }
    // Display the flags that affect how a cookie can be reused.
    out.textContent = resp.data.map(c =>
      `${c.name}=${c.value}` + (c.httpOnly ? ' [H]' : '') + (c.secure ? ' [S]' : '') + (c.partitionKey ? ' [P]' : '')
    ).join('\n');
    renderedCookies = resp.data;
  } catch (e) { renderedCookies = null; out.textContent = message('errorPrefix') + e.message; }
}

async function copyCookies() {
  const btn = document.getElementById('copy');
  if (!renderedCookies?.length) { flashButton(btn, 'copyNothing'); return; }
  try {
    await navigator.clipboard.writeText(
      renderedCookies.map(c => `${c.name}=${c.value}`).join('; ')
    );
    flashButton(btn, 'copyDone');
  } catch (_) {
    // Report on the button, never in #out. A clipboard failure is unrelated to
    // the cookie list, and overwriting it would erase what the user asked for --
    // which is what the old shared try/catch did.
    flashButton(btn, 'copyFailed');
  }
}

// Confirm on the button for 1.5s, then restore the label from i18n rather than
// from whatever text was there: restoring captured text would make a temporary
// message permanent when a second click lands inside the window.
function flashButton(btn, key) {
  btn.textContent = message(key);
  clearTimeout(btn._btapFlashTimer);
  btn._btapFlashTimer = setTimeout(() => {
    btn.textContent = message('copyButton');
  }, 1500);
}
