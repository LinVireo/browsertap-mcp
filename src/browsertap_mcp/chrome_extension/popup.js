document.addEventListener('DOMContentLoaded', () => {
  localizeDocument();
  const btn = document.getElementById('refresh');
  btn.addEventListener('click', fetchCookies);
  document.getElementById('indicator-visible').addEventListener('change', saveIndicatorVisibility);
  loadIndicatorVisibility();
  fetchCookies();
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

function localizeDocument() {
  document.documentElement.lang = chrome.i18n.getUILanguage();
  document.querySelectorAll('[data-i18n]').forEach((element) => {
    element.textContent = message(element.dataset.i18n);
  });
}

async function loadIndicatorVisibility() {
  const stored = await chrome.storage.local.get('btap_indicator_visible');
  let visible = stored.btap_indicator_visible;
  if (visible === undefined) {
    // Carry the pre-BTAP preference over once. The popup is the only place with
    // a healthy extension context guaranteed, so the migration lives here;
    // content.js just reads both keys.
    const legacy = await chrome.storage.local.get('tmwd_indicator_visible');
    if (legacy.tmwd_indicator_visible !== undefined) {
      visible = legacy.tmwd_indicator_visible;
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
    if (!resp.data.length) { out.textContent = message('noCookies'); return; }
    // Display the flags that affect how a cookie can be reused.
    out.textContent = resp.data.map(c =>
      `${c.name}=${c.value}` + (c.httpOnly ? ' [H]' : '') + (c.secure ? ' [S]' : '') + (c.partitionKey ? ' [P]' : '')
    ).join('\n');
    // Preserve the existing one-click name=value clipboard workflow.
    const str = resp.data.map(c => `${c.name}=${c.value}`).join('; ');
    await navigator.clipboard.writeText(str);
  } catch (e) { out.textContent = message('errorPrefix') + e.message; }
}
